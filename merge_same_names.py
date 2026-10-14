"""Merge Neo4j nodes that share the same label and equivalent names.

Safe equivalence (same chemical identity after normalization):
- Unicode dash / slash / whitespace variants
- Schema synonyms from flp_schema.yaml
- Adsorbate star placement (*CO vs CO*)

Does NOT merge charge-distinct species (H+ vs H-, OH vs OH-, Ru vs Ru+).
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config_utils import load_schema, neo4j_password, project_root  # noqa: E402
from src.validate import apply_synonyms, soft_name_key  # noqa: E402


def choose_survivor(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    # Prefer higher degree, then shorter display name, then stable id.
    return sorted(
        nodes,
        key=lambda n: (-int(n.get("degree", 0)), len(n.get("name") or ""), n["id"]),
    )[0]


def fetch_nodes(session) -> list[dict[str, Any]]:
    rows = list(
        session.run(
            """
            MATCH (n)
            WHERE n.name IS NOT NULL
            OPTIONAL MATCH (n)-[r]-()
            WITH n, count(r) AS degree
            RETURN elementId(n) AS id,
                   labels(n)[0] AS label,
                   n.name AS name,
                   n.name_key AS name_key,
                   degree
            """
        )
    )
    return [dict(r) for r in rows]


def merge_pair(session, survivor_id: str, dup_id: str, survivor_label: str) -> dict[str, int]:
    """Move relationships from dup to survivor, then delete dup."""
    moved_out = 0
    moved_in = 0

    out_rels = list(
        session.run(
            """
            MATCH (d)-[r]->(t)
            WHERE elementId(d) = $dup AND elementId(t) <> $surv
            RETURN elementId(r) AS rid, type(r) AS type, elementId(t) AS tid,
                   properties(r) AS props
            """,
            dup=dup_id,
            surv=survivor_id,
        )
    )
    for rel in out_rels:
        session.run(
            f"""
            MATCH (s) WHERE elementId(s) = $surv
            MATCH (t) WHERE elementId(t) = $tid
            MERGE (s)-[nr:{rel['type']}]->(t)
            SET nr += $props
            """,
            surv=survivor_id,
            tid=rel["tid"],
            props=rel["props"] or {},
        )
        moved_out += 1

    in_rels = list(
        session.run(
            """
            MATCH (f)-[r]->(d)
            WHERE elementId(d) = $dup AND elementId(f) <> $surv
            RETURN type(r) AS type, elementId(f) AS fid, properties(r) AS props
            """,
            dup=dup_id,
            surv=survivor_id,
        )
    )
    for rel in in_rels:
        session.run(
            f"""
            MATCH (f) WHERE elementId(f) = $fid
            MATCH (s) WHERE elementId(s) = $surv
            MERGE (f)-[nr:{rel['type']}]->(s)
            SET nr += $props
            """,
            fid=rel["fid"],
            surv=survivor_id,
            props=rel["props"] or {},
        )
        moved_in += 1

    # Copy useful properties / aliases onto survivor, then delete duplicate.
    session.run(
        """
        MATCH (s) WHERE elementId(s) = $surv
        MATCH (d) WHERE elementId(d) = $dup
        WITH s, d,
             coalesce(s.aliases, '') AS old_aliases,
             d.name AS dname
        SET s.aliases = CASE
              WHEN old_aliases = '' THEN dname
              WHEN old_aliases CONTAINS dname THEN old_aliases
              ELSE old_aliases + ' | ' + dname
            END
        WITH s, d
        DETACH DELETE d
        """,
        surv=survivor_id,
        dup=dup_id,
    )
    return {"moved_out": moved_out, "moved_in": moved_in}


def main() -> None:
    load_dotenv(project_root() / ".env")
    schema = load_schema()
    synonyms = schema.get("synonyms") or {}
    password = neo4j_password()
    uri = schema.get("neo4j", {}).get("uri", "bolt://localhost:7687")
    user = schema.get("neo4j", {}).get("user", "neo4j")

    driver = GraphDatabase.driver(uri, auth=(user, password))
    report: dict[str, Any] = {"groups": [], "merged_nodes": 0, "groups_merged": 0}

    try:
        with driver.session() as session:
            nodes = fetch_nodes(session)
            groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
            for n in nodes:
                key = apply_synonyms(n["name"], synonyms)
                groups[(n["label"], key)].append(n)

            dup_groups = {k: v for k, v in groups.items() if len(v) > 1}
            for (label, key), members in sorted(
                dup_groups.items(), key=lambda kv: -len(kv[1])
            ):
                survivor = choose_survivor(members)
                dups = [m for m in members if m["id"] != survivor["id"]]
                group_info = {
                    "label": label,
                    "key": key,
                    "survivor": {
                        "id": survivor["id"],
                        "name": survivor["name"],
                        "name_key": survivor["name_key"],
                    },
                    "merged": [],
                }
                for dup in dups:
                    stats = merge_pair(session, survivor["id"], dup["id"], label)
                    # Keep survivor name_key stable; record alias already set.
                    group_info["merged"].append(
                        {
                            "id": dup["id"],
                            "name": dup["name"],
                            "name_key": dup["name_key"],
                            **stats,
                        }
                    )
                    report["merged_nodes"] += 1
                report["groups_merged"] += 1
                report["groups"].append(group_info)

            # Refresh name_key on survivors to soft key for future MERGE stability
            for g in report["groups"]:
                session.run(
                    """
                    MATCH (n) WHERE elementId(n) = $id
                    SET n.name_key = $nk
                    """,
                    id=g["survivor"]["id"],
                    nk=g["key"],
                )

            stats = session.run(
                "MATCH (n) WITH count(n) AS nodes "
                "MATCH ()-[r]->() RETURN nodes, count(r) AS rels"
            ).single()
            report["after"] = {"nodes": int(stats["nodes"]), "relationships": int(stats["rels"])}
    finally:
        driver.close()

    out = project_root() / "out" / "merge_same_names_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "groups_merged": report["groups_merged"],
                "merged_nodes": report["merged_nodes"],
                "after": report["after"],
                "examples": [
                    {
                        "label": g["label"],
                        "survivor": g["survivor"]["name"],
                        "merged": [m["name"] for m in g["merged"]],
                    }
                    for g in report["groups"][:20]
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
