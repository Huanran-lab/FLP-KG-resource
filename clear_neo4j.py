"""Clear all data from the local FLP Neo4j instance."""
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
    uri = "bolt://localhost:7687"
    user = "neo4j"
    password = os.getenv("NEO4J_PASSWORD", "FLPFLPFLP")
    driver = GraphDatabase.driver(uri, auth=(user, password))
    driver.verify_connectivity()
    with driver.session() as s:
        before_n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        before_r = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"before nodes={before_n} rels={before_r}")

        # Batch delete for large graphs
        while True:
            summary = s.run(
                "MATCH (n) WITH n LIMIT 10000 DETACH DELETE n RETURN count(*) AS c"
            ).single()
            deleted = int(summary["c"])
            if deleted == 0:
                break
            print(f"deleted batch={deleted}")

        after_n = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        after_r = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        print(f"after nodes={after_n} rels={after_r}")

        for row in s.run("SHOW CONSTRAINTS YIELD name RETURN name"):
            name = row["name"]
            print(f"drop constraint {name}")
            s.run(f"DROP CONSTRAINT `{name}` IF EXISTS").consume()

        for row in s.run(
            "SHOW INDEXES YIELD name, type WHERE type <> 'LOOKUP' RETURN name, type"
        ):
            name = row["name"]
            print(f"drop index {name} ({row['type']})")
            s.run(f"DROP INDEX `{name}` IF EXISTS").consume()

        print("FLP instance cleared")
    driver.close()


if __name__ == "__main__":
    main()
