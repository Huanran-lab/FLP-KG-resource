"""One-off / reusable merge for VCe-O ↔ VO-Ce-O style aliases."""
from __future__ import annotations

import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config_utils import load_schema, neo4j_password, project_root  # noqa: E402
from src.validate import soft_name_key  # noqa: E402


# survivor_name -> duplicate names to fold in
PAIRS = {
    "VO-Ce-O": ["VCe-O", "Vo-Ce-O"],
    "VO-Ce-O cluster": ["VCe-O cluster", "Vo-Ce-O cluster"],
}


def merge_pair(session, survivor_name: str, dup_name: str, label: str = "Defect") -> bool:
    rows = list(
        session.run(
            """
            MATCH (s), (d)
            WHERE labels(s)[0] = $label AND labels(d)[0] = $label
              AND s.name = $s AND d.name = $d
            RETURN elementId(s) AS sid, elementId(d) AS did
            """,
            label=label,
            s=survivor_name,
            d=dup_name,
        )
    )
    if not rows:
        print(f"SKIP missing: {dup_name} -> {survivor_name}")
        return False

    sid, did = rows[0]["sid"], rows[0]["did"]
    if sid == did:
        return False

    outs = list(
        session.run(
            """
            MATCH (d)-[r]->(t)
            WHERE elementId(d) = $did AND elementId(t) <> $sid
            RETURN type(r) AS type, elementId(t) AS tid, properties(r) AS props
            """,
            did=did,
            sid=sid,
        )
    )
    for rel in outs:
        session.run(
            f"""
            MATCH (s) WHERE elementId(s) = $sid
            MATCH (t) WHERE elementId(t) = $tid
            MERGE (s)-[nr:{rel['type']}]->(t)
            SET nr += $props
            """,
            sid=sid,
            tid=rel["tid"],
            props=rel["props"] or {},
        )

    ins = list(
        session.run(
            """
            MATCH (f)-[r]->(d)
            WHERE elementId(d) = $did AND elementId(f) <> $sid
            RETURN type(r) AS type, elementId(f) AS fid, properties(r) AS props
            """,
            did=did,
            sid=sid,
        )
    )
    for rel in ins:
        session.run(
            f"""
            MATCH (f) WHERE elementId(f) = $fid
            MATCH (s) WHERE elementId(s) = $sid
            MERGE (f)-[nr:{rel['type']}]->(s)
            SET nr += $props
            """,
            fid=rel["fid"],
            sid=sid,
            props=rel["props"] or {},
        )

    session.run(
        """
        MATCH (s) WHERE elementId(s) = $sid
        MATCH (d) WHERE elementId(d) = $did
        WITH s, d, coalesce(s.aliases, '') AS old_a, d.name AS dname
        SET s.aliases = CASE
              WHEN old_a = '' THEN dname
              WHEN old_a CONTAINS dname THEN old_a
              ELSE old_a + ' | ' + dname
            END,
            s.name_key = $nk
        WITH s, d
        DETACH DELETE d
        """,
        sid=sid,
        did=did,
        nk=soft_name_key(survivor_name),
    )
    print(f"MERGED {dup_name} -> {survivor_name}")
    return True


def main() -> None:
    load_dotenv(project_root() / ".env")
    schema = load_schema()
    uri = schema.get("neo4j", {}).get("uri", "bolt://localhost:7687")
    user = schema.get("neo4j", {}).get("user", "neo4j")
    driver = GraphDatabase.driver(uri, auth=(user, neo4j_password()))
    try:
        with driver.session() as session:
            for survivor, dups in PAIRS.items():
                for dup in dups:
                    merge_pair(session, survivor, dup)
            left = list(
                session.run(
                    """
                    MATCH (n:Defect)
                    WHERE toLower(n.name) CONTAINS 'vce'
                       OR toLower(n.name) CONTAINS 'vo-ce'
                       OR toLower(n.name) CONTAINS 'vo–ce'
                    RETURN n.name AS name, n.aliases AS aliases, n.name_key AS nk
                    ORDER BY name
                    """
                )
            )
            print(json.dumps([dict(r) for r in left], ensure_ascii=False, indent=2))
    finally:
        driver.close()


if __name__ == "__main__":
    main()
