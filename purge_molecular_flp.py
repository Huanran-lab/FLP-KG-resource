"""Delete remaining clearly molecular (non-solid) FLP nodes."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config_utils import neo4j_password  # noqa: E402

load_dotenv(ROOT / ".env")

DELETE_NAMES = [
    "(PhMe)Al(C6F5)3/(o-C6H4Me)3P FLP",
    "bisphosphino naphthalene FLP",
    "phosphido-borane FLP",
    "phosphine/borane FLP",
    "B(C6F5)3/(o-C6H4Me)3P FLP",
    "phosphino-borane 11a",
    "phosphino-borane FLP",
    "phosphinoferrocene/B(C6F5)3 FLP",
]


def main() -> None:
    driver = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("neo4j", neo4j_password())
    )
    with driver.session() as s:
        deleted = s.run(
            """
            MATCH (f:FLP)
            WHERE f.name IN $names
               OR toLower(f.name) CONTAINS 'phosphine/borane'
               OR toLower(f.name) CONTAINS 'phosphido-borane'
               OR toLower(f.name) CONTAINS 'bisphosphino'
            WITH collect(DISTINCT f) AS fs
            FOREACH (f IN fs | DETACH DELETE f)
            RETURN size(fs) AS c
            """,
            names=DELETE_NAMES,
        ).single()["c"]
        print(f"deleted molecular FLP: {deleted}")

        # Unsupported molecular B(C6F5) patterns (no solid location/support)
        deleted2 = s.run(
            """
            MATCH (f:FLP)
            WHERE (toLower(f.name) CONTAINS 'b(c6f5)' OR toLower(f.name) CONTAINS 'tbu3p')
              AND NOT EXISTS { (f)-[:LOCATED_ON|SUPPORTED_ON]->() }
              AND NOT EXISTS { (:Defect)-[:ENABLES]->(f) }
            WITH collect(f) AS fs
            FOREACH (f IN fs | DETACH DELETE f)
            RETURN size(fs) AS c
            """
        ).single()["c"]
        print(f"deleted unsupported B(C6F5)/tBu3P FLP: {deleted2}")

        orphan = s.run(
            """
            MATCH (f:FLP {name: 'chiral frustrated Lewis pair'})
            OPTIONAL MATCH (f)-[r]-()
            WITH f, count(r) AS rc
            WHERE rc = 0
            DETACH DELETE f
            RETURN count(*) AS c
            """
        ).single()["c"]
        print(f"deleted orphan chiral frustrated Lewis pair: {orphan}")

        nodes = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        flps = s.run("MATCH (:FLP) RETURN count(*) AS c").single()["c"]
        print(f"before merge: nodes={nodes} rels={rels} flp={flps}")

    driver.close()


if __name__ == "__main__":
    main()
