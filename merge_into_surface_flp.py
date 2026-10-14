"""Merge generic FLP aliases into FLP:surface FLP; fix mislabeled Defect nodes."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config_utils import neo4j_password  # noqa: E402

load_dotenv(ROOT / ".env")

SURVIVOR_LABEL = "FLP"
SURVIVOR_NAME = "surface FLP"
SURVIVOR_KEY = "surface flp"

# (label, name) to fold into surface FLP
MERGE_NAMES = [
    ("FLP", "frustrated Lewis pair"),
    ("FLP", "Frustrated Lewis pair"),
    ("FLP", "frustrated lewis pair"),
    ("FLP", "dynamic frustrated Lewis pair"),
    ("FLP", "FLP"),
    ("Mechanism", "FLP mechanism"),
    ("Defect", "surface FLP"),
    ("Defect", "frustrated Lewis pair"),
    ("Defect", "FLP mechanism"),
]


def ensure_survivor(session) -> str:
    """Return elementId of survivor FLP:surface FLP."""
    # Prefer name_key surface flp; also fold surfaceflp duplicate
    row = session.run(
        """
        MATCH (n:FLP)
        WHERE n.name_key IN ['surface flp', 'surfaceflp'] OR n.name = $name
        RETURN elementId(n) AS id, n.name_key AS nk, n.name AS name
        ORDER BY CASE n.name_key WHEN 'surface flp' THEN 0 ELSE 1 END
        """,
        name=SURVIVOR_NAME,
    ).data()
    if not row:
        session.run(
            """
            CREATE (n:FLP {
              name: $name,
              name_key: $nk,
              aliases: [$nk]
            })
            """,
            name=SURVIVOR_NAME,
            nk=SURVIVOR_KEY,
        )
        return session.run(
            "MATCH (n:FLP {name_key:$nk}) RETURN elementId(n) AS id",
            nk=SURVIVOR_KEY,
        ).single()["id"]

    survivor_id = row[0]["id"]
    # Fix survivor props
    session.run(
        """
        MATCH (n) WHERE elementId(n)=$id
        SET n:FLP,
            n.name=$name,
            n.name_key=$nk
        REMOVE n:Defect
        """,
        id=survivor_id,
        name=SURVIVOR_NAME,
        nk=SURVIVOR_KEY,
    )
    # Merge other surfaceflp duplicates into survivor
    for r in row[1:]:
        merge_pair(session, survivor_id, r["id"])
    return survivor_id


def merge_pair(session, surv_id: str, dup_id: str) -> None:
    if surv_id == dup_id:
        return
    outs = list(
        session.run(
            """
            MATCH (d)-[r]->(t)
            WHERE elementId(d)=$dup AND elementId(t)<>$surv
            RETURN type(r) AS type, elementId(t) AS tid, properties(r) AS props
            """,
            dup=dup_id,
            surv=surv_id,
        )
    )
    for rel in outs:
        session.run(
            f"""
            MATCH (s) WHERE elementId(s)=$surv
            MATCH (t) WHERE elementId(t)=$tid
            MERGE (s)-[nr:{rel['type']}]->(t)
            SET nr += $props
            """,
            surv=surv_id,
            tid=rel["tid"],
            props=rel["props"] or {},
        )
    ins = list(
        session.run(
            """
            MATCH (f)-[r]->(d)
            WHERE elementId(d)=$dup AND elementId(f)<>$surv
            RETURN type(r) AS type, elementId(f) AS fid, properties(r) AS props
            """,
            dup=dup_id,
            surv=surv_id,
        )
    )
    for rel in ins:
        session.run(
            f"""
            MATCH (f) WHERE elementId(f)=$fid
            MATCH (s) WHERE elementId(s)=$surv
            MERGE (f)-[nr:{rel['type']}]->(s)
            SET nr += $props
            """,
            fid=rel["fid"],
            surv=surv_id,
            props=rel["props"] or {},
        )
    # Collect alias
    dup_name = session.run(
        "MATCH (d) WHERE elementId(d)=$id RETURN d.name AS name", id=dup_id
    ).single()
    if dup_name and dup_name["name"]:
        session.run(
            """
            MATCH (s) WHERE elementId(s)=$surv
            SET s.aliases = coalesce(s.aliases, []) + [$alias]
            """,
            surv=surv_id,
            alias=dup_name["name"],
        )
    session.run("MATCH (d) WHERE elementId(d)=$dup DETACH DELETE d", dup=dup_id)


def main() -> None:
    driver = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", neo4j_password()),
    )
    merged = []
    with driver.session() as s:
        surv = ensure_survivor(s)
        # Find candidates by soft match
        cands = s.run(
            """
            MATCH (n)
            WHERE n.name IS NOT NULL AND elementId(n) <> $surv
              AND (
                toLower(n.name) IN [
                  'frustrated lewis pair', 'flp mechanism',
                  'dynamic frustrated lewis pair', 'flp',
                  'heterogeneous flp', 'solid flp', 'surface flp'
                ]
                OR n.name_key IN [
                  'frustrated lewis pair', 'flp mechanism',
                  'dynamic frustrated lewis pair', 'surfaceflp',
                  'frustratedlewispair', 'flpmechanism', 'surface flp'
                ]
              )
              AND NOT (n:FLP AND n.name_key = 'surface flp')
            RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name, n.name_key AS nk
            """,
            surv=surv,
        ).data()

        for c in cands:
            merge_pair(s, surv, c["id"])
            merged.append({"from_label": c["label"], "from_name": c["name"], "nk": c["nk"]})

        # Final hygiene: no Defect label on survivor; unique aliases
        s.run(
            """
            MATCH (n) WHERE elementId(n)=$surv
            SET n:FLP, n.name=$name, n.name_key=$nk
            REMOVE n:Defect
            """,
            surv=surv,
            name=SURVIVOR_NAME,
            nk=SURVIVOR_KEY,
        )
        s.run(
            """
            MATCH (n) WHERE elementId(n)=$surv
            WITH n, coalesce(n.aliases, []) AS a
            UNWIND (CASE WHEN size(a)=0 THEN [null] ELSE a END) AS x
            WITH n, [x IN collect(DISTINCT x) WHERE x IS NOT NULL] AS uniq
            SET n.aliases = uniq + CASE WHEN NOT $name IN uniq THEN [$name] ELSE [] END
            """,
            surv=surv,
            name=SURVIVOR_NAME,
        )

        # Verify no Defect named surface FLP
        bad = s.run(
            """
            MATCH (n:Defect)
            WHERE toLower(n.name) = 'surface flp'
               OR n.name_key = 'surface flp'
            RETURN count(n) AS c
            """
        ).single()["c"]
        left = s.run(
            """
            MATCH (n:FLP {name_key:'surface flp'})
            OPTIONAL MATCH (n)-[r]-()
            RETURN n.name AS name, labels(n) AS labels, count(r) AS degree,
                   n.aliases AS aliases
            """
        ).single()
        after = s.run(
            "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
            "RETURN nodes, count(r) AS rels"
        ).single()

    report = {
        "merged": merged,
        "survivor": dict(left) if left else None,
        "defect_surface_flp_remaining": bad,
        "after": dict(after),
    }
    out = ROOT / "out" / "merge_surface_flp_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    driver.close()


if __name__ == "__main__":
    main()
