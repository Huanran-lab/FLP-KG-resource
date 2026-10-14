"""Merge Al2O3 / γ-Al2O3 / gamma-Al2O3 into canonical Al2O3."""
from __future__ import annotations

import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config_utils import neo4j_password  # noqa: E402
from src.validate import soft_name_key  # noqa: E402
from scripts.dedupe_graph import merge_pair  # noqa: E402

SURVIVOR = "Al2O3"
ALIASES = [
    "γ-Al2O3",
    "gamma-Al2O3",
    "γ‑Al2O3",
    "Gamma-Al2O3",
    "γ-Al₂O₃",
    "Al₂O₃",
]


def main() -> None:
    load_dotenv(ROOT / ".env")
    d = GraphDatabase.driver("bolt://localhost:7687", auth=("neo4j", neo4j_password()))
    with d.session() as s:
        related = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(replace(n.name,'₂','2')) CONTAINS 'al2o3'
                  OR toLower(n.name) CONTAINS 'alumina'
                )
                AND NOT n:FLP AND NOT n:Reaction AND NOT n:Defect
                AND NOT n:LewisAcid AND NOT n:LewisBase
                RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                       size([(n)--()|1]) AS deg
                ORDER BY deg DESC
                """
            )
        ]
        print("related before:")
        for r in related:
            print(f"  {r['labs']} deg={r['deg']}  {r['name']}")

        # Pure alumina / gamma-alumina only (not doped composites)
        pure = []
        for r in related:
            n = r["name"].strip()
            low = n.lower().replace("₂", "2").replace("₃", "3")
            # strip greek / gamma prefix
            core = low
            for p in ("γ-", "𝛾-", "gamma-", "α-", "a-", "beta-", "β-"):
                if core.startswith(p):
                    core = core[len(p) :]
            if core in {"al2o3", "alumina"} or n in ALIASES or n == SURVIVOR:
                pure.append(r)

        if not pure:
            print("nothing to merge")
            return

        pure.sort(key=lambda x: -x["deg"])
        # Prefer node already named Al2O3
        surv = next((p for p in pure if p["name"] == SURVIVOR), pure[0])
        sid = surv["id"]
        s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            SET n.name=$name, n.name_key=$nk, n:Material
            """,
            id=sid,
            name=SURVIVOR,
            nk=soft_name_key(SURVIVOR),
        )
        print(f"survivor: {surv['name']} -> {SURVIVOR}")

        for r in pure:
            if r["id"] == sid:
                continue
            still = s.run(
                "MATCH (n) WHERE elementId(n)=$id RETURN n.name AS name", id=r["id"]
            ).single()
            if not still:
                continue
            merge_pair(s, sid, r["id"])
            print(f"  MERGED {r['name']} -> {SURVIVOR}")

        # Also merge any remaining exact alias names
        for alias in ALIASES:
            rows = list(
                s.run(
                    """
                    MATCH (n) WHERE n.name = $name
                    RETURN elementId(n) AS id, n.name AS name
                    """,
                    name=alias,
                )
            )
            for r in rows:
                if r["id"] == sid:
                    continue
                merge_pair(s, sid, r["id"])
                print(f"  MERGED {r['name']} -> {SURVIVOR}")

        s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            WITH n, coalesce(n.aliases, []) AS a
            WITH n, a + $extra AS raw
            UNWIND raw AS x
            WITH n, collect(DISTINCT x) AS uniq
            SET n.aliases = [x IN uniq WHERE x IS NOT NULL],
                n.name = $name, n.name_key = $nk
            SET n:Material:Support
            """,
            id=sid,
            extra=[SURVIVOR, "γ-Al2O3", "gamma-Al2O3", "alumina"],
            name=SURVIVOR,
            nk=soft_name_key(SURVIVOR),
        )

        left = list(
            s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(replace(n.name,'₂','2')) =~ '^(γ-|gamma-)?al2o3$'
                  OR toLower(n.name) = 'alumina'
                )
                RETURN n.name AS name, labels(n) AS labs, size([(n)--()|1]) AS deg
                """
            )
        )
        print("left:", [dict(x) for x in left])
        print(
            dict(
                s.run(
                    "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                    "RETURN nodes, count(r) AS rels"
                ).single()
            )
        )
    d.close()


if __name__ == "__main__":
    main()
