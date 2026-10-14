"""Rename VCe-O nodes to canonical VO-Ce-O when survivor is missing."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

RENAMES = [
    ("VCe-O", "VO-Ce-O"),
    ("VCe-O cluster", "VO-Ce-O cluster"),
]


def main() -> None:
    d = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", os.getenv("NEO4J_PASSWORD")),
    )
    with d.session() as s:
        for old, new in RENAMES:
            has_new = s.run("MATCH (n {name:$n}) RETURN count(n) AS c", n=new).single()["c"]
            has_old = s.run("MATCH (n {name:$n}) RETURN count(n) AS c", n=old).single()["c"]
            print(f"{old} -> {new}: old={has_old} new={has_new}")
            if has_old and not has_new:
                s.run(
                    "MATCH (n {name:$old}) "
                    "SET n.name=$new, n.aliases = coalesce(n.aliases, []) + [$old]",
                    old=old,
                    new=new,
                )
                print("  renamed")
        rows = list(
            s.run(
                """
                MATCH (m:Material)-[:HAS_DEFECT]->(d:Defect)-[:ENABLES]->(f:FLP)
                      -[:HAS_ACID_SITE]->(a)
                RETURN m.name AS material, d.name AS defect, f.name AS flp, a.name AS acid
                LIMIT 8
                """
            )
        )
        print("canonical paths:", len(rows))
        for r in rows:
            print(dict(r))
        n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        e = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"final nodes={n} rels={e}")
    d.close()


if __name__ == "__main__":
    main()
