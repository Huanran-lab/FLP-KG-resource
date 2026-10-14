"""Audit how FLP-centric the current Neo4j graph is."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")


def main() -> None:
    d = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", os.getenv("NEO4J_PASSWORD")),
    )
    with d.session() as s:
        print("=== label counts ===")
        for r in s.run(
            "MATCH (n) RETURN labels(n)[0] AS label, count(*) AS c ORDER BY c DESC"
        ):
            print(dict(r))

        print("=== FLP-centric edge counts ===")
        checks = {
            "FLP_HAS_ACID": "MATCH (:FLP)-[:HAS_ACID_SITE]->() RETURN count(*) AS c",
            "FLP_HAS_BASE": "MATCH (:FLP)-[:HAS_BASE_SITE]->() RETURN count(*) AS c",
            "DEFECT_ENABLES_FLP": "MATCH (:Defect)-[:ENABLES]->(:FLP) RETURN count(*) AS c",
            "FLP_LOCATED_ON": "MATCH (:FLP)-[:LOCATED_ON]->() RETURN count(*) AS c",
            "FLP_CATALYZES": "MATCH (:FLP)-[:CATALYZES]->() RETURN count(*) AS c",
            "FLP_ACTIVATES": "MATCH (:FLP)-[:ACTIVATES]->() RETURN count(*) AS c",
            "ACID_PAIR_BASE": "MATCH (:LewisAcid)-[:FORMS_PAIR_WITH]->(:LewisBase) RETURN count(*) AS c",
        }
        for name, cy in checks.items():
            print(name, s.run(cy).single()["c"])

        flp_n = s.run("MATCH (f:FLP) RETURN count(f) AS c").single()["c"]
        print("FLP nodes:", flp_n)

        print("=== sample FLP names ===")
        for r in s.run("MATCH (f:FLP) RETURN f.name AS name ORDER BY name LIMIT 30"):
            print("-", r["name"])

        orphan = s.run(
            """
            MATCH (n)
            WHERE NOT n:FLP
              AND NOT EXISTS { MATCH (n)-[*1..2]-(:FLP) }
            RETURN labels(n)[0] AS label, count(*) AS c
            ORDER BY c DESC
            """
        ).data()
        total_orphan = sum(r["c"] for r in orphan)
        total = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        print("=== nodes with no FLP within 2 hops ===")
        for r in orphan:
            print(r)
        print(f"orphan {total_orphan}/{total} ({100*total_orphan/total:.1f}%)")

        # Wrong over-merge signal: same FLP linked to many unrelated materials via shared vacancy
        print("=== overshare: materials sharing identical surface FLP + Ce3+ path ===")
        row = s.run(
            """
            MATCH (m:Material)-[:HAS_DEFECT]->(d:Defect {name:'oxygen vacancy'})
                  -[:ENABLES]->(f:FLP {name:'surface FLP'})
                  -[:HAS_ACID_SITE]->(a {name:'Ce3+'})
            RETURN count(DISTINCT m) AS materials
            """
        ).single()
        print(dict(row))
    d.close()


if __name__ == "__main__":
    main()
