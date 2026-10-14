"""Remove homogeneous (molecular) FLP subgraph; keep solid/heterogeneous FLP.

Then merge equivalent nodes (soft name key + schema/extra synonyms).
"""
from __future__ import annotations

import json
import os
import re
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

load_dotenv(ROOT / ".env")

SOLID_NAME = re.compile(
    r"(surface|solid|hetero|oxide|ceo2|tio2|zro2|al2o3|in2o3|zno|mof|cof|"
    r"zeolite|fau|porous|vacanc|defect|support|interface|lattice|"
    r"carbon nitride|pcn|g-c3n4|wo3|nio|cuo|doped|cluster|"
    r"\b[a-z]{0,3}-?o\b|/o\b|o\(h\)|oh\.\.\.|sflp|cflp|"
    r"mil-|uio-|sion-|heos?|hap|hydroxyapatite)",
    re.I,
)

# Strong homogeneous molecular markers
HOMO_STRONG = re.compile(
    r"("
    r"b\(c6f5\)3|b\(p-c6f4h\)3|bph3|\bbcf\b|"
    r"p\(t-?bu\)3|ptbu3|tbu3p|pmes3|"
    r"\(c6f5\)3[pb]|\(c6h2me3\)3p|"
    r"1-tmp-2-bh2|tmp-h|lutidine/|"
    r"phosphino-?borane|phosphine-?borane|phosphido-?borane|"
    r"bisphosphino|phosphinoferrocene|carbene/b\(|"
    r"open tbu3p/|zirconocene amine|"
    r"intramolecular (?:p-b )?flp|"
    r"compound\s+\d+|substituted flp\s+\d+|"
    r"alkenylene-linked|macromolecular flp|"
    r"\b\d{2,3}[a-z]?(?:/\d{2,3}[a-z]?)?\s*/\s*b\("
    r")",
    re.I,
)

HOMO_GENERIC = re.compile(
    r"^(frustrated lewis pair|phosphine-borane adduct|dynamic flp|"
    r"modified flp(?: with chiral centers)?|rotation-activated flp|"
    r"flp network|flp-mediated bifunctional catalyst)$",
    re.I,
)


def load_extra_synonyms() -> dict[str, list[str]]:
    path = project_root() / "synonyms_extra.yaml"
    if not path.exists():
        return {}
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    return {str(k): list(v or []) for k, v in data.items()}


def classify_flp(row: dict[str, Any]) -> str:
    name = row["name"] or ""
    mats = [m for m in (row.get("mats") or []) if m]
    defect_links = int(row.get("defect_links") or 0)
    has_solid_link = bool(mats) or defect_links > 0

    if has_solid_link or SOLID_NAME.search(name):
        return "solid"
    if HOMO_STRONG.search(name) or HOMO_GENERIC.search(name.strip()):
        return "homo"
    # numbered intramolecular / compound-style without solid evidence
    if re.search(r"\b\d{2,3}[a-z]\b", name) and not SOLID_NAME.search(name):
        return "homo"
    # remaining unclear without solid evidence -> treat as homo noise
    return "homo"


def fetch_flps(session) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in session.run(
            """
            MATCH (f:FLP)
            OPTIONAL MATCH (f)-[:LOCATED_ON|SUPPORTED_ON]->(m)
            OPTIONAL MATCH (def:Defect)-[:ENABLES]->(f)
            OPTIONAL MATCH (mat:Material)-[:HAS_DEFECT]->(def)
            WITH f,
                 collect(DISTINCT coalesce(m.name, mat.name)) AS mats,
                 count(def) AS defect_links
            RETURN elementId(f) AS id,
                   f.name AS name,
                   mats,
                   defect_links
            """
        )
    ]


def delete_homo_flps(session, homo_ids: list[str]) -> int:
    if not homo_ids:
        return 0
    # Also drop exclusive acid/base partners that only touch these FLPs
    result = session.run(
        """
        UNWIND $ids AS fid
        MATCH (f:FLP) WHERE elementId(f) = fid
        OPTIONAL MATCH (f)-[:HAS_ACID_SITE|HAS_BASE_SITE|FORMS_PAIR_WITH]-(ab)
        WITH collect(DISTINCT f) AS flps, collect(DISTINCT ab) AS partners
        FOREACH (f IN flps | DETACH DELETE f)
        WITH partners
        UNWIND partners AS p
        WITH p WHERE p IS NOT NULL
          AND NOT EXISTS { MATCH (p)--(:FLP) }
          AND NOT EXISTS {
            MATCH (p)--(x)
            WHERE x:Material OR x:Defect OR x:Catalyst OR x:Support OR x:ActiveSite
          }
        DETACH DELETE p
        RETURN count(p) AS deleted_partners
        """,
        ids=homo_ids,
    ).single()
    return int(result["deleted_partners"] if result else 0)


def prune_non_flp_orphans(session) -> int:
    """Delete nodes with no FLP within 2 hops (after homo removal)."""
    result = session.run(
        """
        MATCH (n)
        WHERE NOT n:FLP
          AND NOT EXISTS { MATCH (n)-[*1..2]-(:FLP) }
        WITH collect(n) AS ns
        FOREACH (n IN ns | DETACH DELETE n)
        RETURN size(ns) AS deleted
        """
    ).single()
    return int(result["deleted"] if result else 0)


def choose_survivor(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return sorted(
        nodes,
        key=lambda n: (-int(n.get("degree", 0)), len(n.get("name") or ""), n["id"]),
    )[0]


def merge_equivalents(session, synonyms: dict[str, list[str]]) -> dict[str, Any]:
    rows = [
        dict(r)
        for r in session.run(
            """
            MATCH (n)
            WHERE n.name IS NOT NULL
            OPTIONAL MATCH (n)-[r]-()
            WITH n, count(r) AS degree
            RETURN elementId(n) AS id,
                   labels(n)[0] AS label,
                   n.name AS name,
                   degree
            """
        )
    ]
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for n in rows:
        key = apply_synonyms(str(n["name"]), synonyms)
        groups[(n["label"], key)].append(n)

    merged_nodes = 0
    groups_merged = 0
    examples: list[dict[str, Any]] = []

    for (label, key), nodes in groups.items():
        if len(nodes) < 2:
            continue
        survivor = choose_survivor(nodes)
        dups = [n for n in nodes if n["id"] != survivor["id"]]
        groups_merged += 1
        alias_names = [survivor["name"]]
        for dup in dups:
            alias_names.append(dup["name"])
            # move outs
            outs = list(
                session.run(
                    """
                    MATCH (d)-[r]->(t)
                    WHERE elementId(d) = $dup AND elementId(t) <> $surv
                    RETURN type(r) AS type, elementId(t) AS tid, properties(r) AS props
                    """,
                    dup=dup["id"],
                    surv=survivor["id"],
                )
            )
            for rel in outs:
                session.run(
                    f"""
                    MATCH (s) WHERE elementId(s) = $surv
                    MATCH (t) WHERE elementId(t) = $tid
                    MERGE (s)-[nr:{rel['type']}]->(t)
                    SET nr += $props
                    """,
                    surv=survivor["id"],
                    tid=rel["tid"],
                    props=rel["props"] or {},
                )
            ins = list(
                session.run(
                    """
                    MATCH (f)-[r]->(d)
                    WHERE elementId(d) = $dup AND elementId(f) <> $surv
                    RETURN type(r) AS type, elementId(f) AS fid, properties(r) AS props
                    """,
                    dup=dup["id"],
                    surv=survivor["id"],
                )
            )
            for rel in ins:
                session.run(
                    f"""
                    MATCH (f) WHERE elementId(f) = $fid
                    MATCH (s) WHERE elementId(s) = $surv
                    MERGE (f)-[nr:{rel['type']}]->(s)
                    SET nr += $props
                    """,
                    fid=rel["fid"],
                    surv=survivor["id"],
                    props=rel["props"] or {},
                )
            session.run(
                "MATCH (d) WHERE elementId(d) = $dup DETACH DELETE d",
                dup=dup["id"],
            )
            merged_nodes += 1

        # refresh survivor aliases / name_key
        uniq_aliases = []
        for a in alias_names:
            if a and a not in uniq_aliases:
                uniq_aliases.append(a)
        session.run(
            """
            MATCH (s) WHERE elementId(s) = $sid
            SET s.name_key = $nk,
                s.aliases = $aliases
            """,
            sid=survivor["id"],
            nk=key,
            aliases=uniq_aliases,
        )
        if len(examples) < 15:
            examples.append(
                {
                    "label": label,
                    "key": key,
                    "survivor": survivor["name"],
                    "merged": [d["name"] for d in dups],
                }
            )

    after = session.run(
        """
        MATCH (n) WITH count(n) AS nodes
        MATCH ()-[r]->() RETURN nodes, count(r) AS relationships
        """
    ).single()
    return {
        "groups_merged": groups_merged,
        "merged_nodes": merged_nodes,
        "after": dict(after) if after else {},
        "examples": examples,
    }


def main() -> None:
    schema = load_schema()
    synonyms = dict(schema.get("synonyms") or {})
    synonyms.update(load_extra_synonyms())

    uri = schema.get("neo4j", {}).get("uri", "bolt://localhost:7687")
    user = schema.get("neo4j", {}).get("user", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, neo4j_password()))

    report: dict[str, Any] = {}
    with driver.session() as s:
        before = s.run(
            "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
            "RETURN nodes, count(r) AS relationships"
        ).single()
        report["before"] = dict(before)

        flps = fetch_flps(s)
        solid, homo = [], []
        for f in flps:
            bucket = classify_flp(f)
            (solid if bucket == "solid" else homo).append(f)

        report["flp_solid"] = [f["name"] for f in solid]
        report["flp_homo_removed"] = [f["name"] for f in homo]

        partners = delete_homo_flps(s, [f["id"] for f in homo])
        report["deleted_homo_flp"] = len(homo)
        report["deleted_exclusive_partners"] = partners

        orphans = prune_non_flp_orphans(s)
        report["deleted_orphans"] = orphans

        merge_stats = merge_equivalents(s, synonyms)
        report["merge"] = merge_stats

        after = s.run(
            "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
            "RETURN nodes, count(r) AS relationships"
        ).single()
        report["after"] = dict(after)

        flp_left = s.run("MATCH (f:FLP) RETURN count(f) AS c").single()["c"]
        report["flp_remaining"] = flp_left

        # smoke
        paths = s.run(
            """
            MATCH (m:Material)-[:HAS_DEFECT]->(d:Defect)-[:ENABLES]->(f:FLP)
                  -[:HAS_ACID_SITE]->(a)
            RETURN m.name AS material, d.name AS defect, f.name AS flp, a.name AS acid
            LIMIT 8
            """
        ).data()
        report["canonical_sample"] = paths

    out = project_root() / "out" / "prune_homo_merge_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: report[k] for k in ("before", "deleted_homo_flp", "deleted_exclusive_partners", "deleted_orphans", "merge", "after", "flp_remaining")}, ensure_ascii=False, indent=2))
    print("homo removed:")
    for n in report["flp_homo_removed"]:
        print(" -", n)
    print("report:", out)
    driver.close()


if __name__ == "__main__":
    main()
