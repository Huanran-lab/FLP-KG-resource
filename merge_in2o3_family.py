"""Merge In(OH)/hydroxylated-indium variants into In2O3; dedupe InOH···In SFLP."""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config_utils import neo4j_password  # noqa: E402
from src.validate import soft_name_key  # noqa: E402
from scripts.merge_facet_surface_aliases import merge_pair, find_nodes_by_name  # noqa: E402

# Exact names to fold into In2O3 (bulk / hydroxylated oxide family)
INTO_IN2O3 = [
    "In(OH)3-x",
    "In(OH)3",
    "In(OH)₃",
    "In(OH)₃-x",
    "In2O3-x(OH)y",
    "In₂O₃-x(OH)y",
    "indium hydroxide",
    "InOH",
    "In(OH)",
    "In-OH",
]

# Keep distinct (functionalized / doped / ligand-modified)
KEEP_OUT = {
    "amine-functionalized in2o3",
    "in2o3-x(eda)y",
    "n-in2o3",
    "in2o3 nanocrystals",  # morphology — fold? user didn't ask; keep unless exact hydroxide
}


def is_hydroxide_variant(name: str) -> bool:
    n = name.strip()
    low = n.lower().replace("₃", "3").replace("₂", "2")
    if low in {x.lower().replace("₃", "3") for x in INTO_IN2O3}:
        return True
    # In2O3-x(OH)y style
    if re.match(r"^in2o3[-–]?x?\s*\(oh\)\s*y?$", low):
        return True
    if re.match(r"^in\s*\(oh\)\s*3?[-–]?x?$", low):
        return True
    if low in {"indium hydroxide", "inoh", "in(oh)", "in-oh"}:
        return True
    return False


def main() -> None:
    load_dotenv(ROOT / ".env")
    driver = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("neo4j", neo4j_password())
    )
    report: dict = {"merged_into_in2o3": [], "flp_merged": [], "skipped": []}

    with driver.session() as s:
        # Ensure In2O3 survivor
        rows = find_nodes_by_name(s, "In2O3")
        if not rows:
            s.run(
                """
                CREATE (n:Material {
                  name: 'In2O3',
                  name_key: $nk,
                  aliases: ['In2O3']
                })
                """,
                nk=soft_name_key("In2O3"),
            )
            rows = find_nodes_by_name(s, "In2O3")
        sid = rows[0]["id"]
        s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            SET n.name='In2O3', n.name_key=$nk, n:Material
            """,
            id=sid,
            nk=soft_name_key("In2O3"),
        )

        cands = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(n.name) CONTAINS 'in2o3'
                  OR toLower(n.name) CONTAINS 'in(oh)'
                  OR toLower(n.name) CONTAINS 'indium hydroxide'
                  OR toLower(n.name) = 'inoh'
                  OR n.name CONTAINS 'In-OH'
                  OR n.name CONTAINS 'InOH'
                )
                AND NOT n:FLP AND NOT n:Reaction AND NOT n:Defect
                AND NOT n:Mechanism
                RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                       size([(n)--() | 1]) AS deg
                ORDER BY name
                """
            )
        ]

        for c in cands:
            if c["id"] == sid:
                continue
            low = c["name"].lower()
            if low in KEEP_OUT or any(k in low for k in ("eda", "amine-functionalized", "n-in2o3")):
                if "n-in2o3" == low.replace(" ", "") or low.startswith("n-in2o3"):
                    report["skipped"].append({"name": c["name"], "reason": "doped"})
                    continue
                if "amine" in low or "eda" in low:
                    report["skipped"].append({"name": c["name"], "reason": "functionalized"})
                    continue
            if "nanocrystal" in low:
                report["skipped"].append({"name": c["name"], "reason": "morphology"})
                continue
            if not is_hydroxide_variant(c["name"]) and "in2o3-x(oh)" not in low.replace(" ", ""):
                # only hydroxide / InOH family + explicit list
                if c["name"] not in INTO_IN2O3:
                    report["skipped"].append({"name": c["name"], "reason": "not hydroxide alias"})
                    continue
            still = s.run(
                "MATCH (n) WHERE elementId(n)=$id RETURN n.name AS name", id=c["id"]
            ).single()
            if not still:
                continue
            dname = merge_pair(s, sid, c["id"])
            if dname:
                report["merged_into_in2o3"].append(dname)
                print(f"  MERGED {dname} -> In2O3")

        # Also catch any exact INTO_IN2O3 missed
        for name in INTO_IN2O3:
            for r in find_nodes_by_name(s, name):
                if r["id"] == sid:
                    continue
                dname = merge_pair(s, sid, r["id"])
                if dname:
                    report["merged_into_in2o3"].append(dname)
                    print(f"  MERGED {dname} -> In2O3")

        # Dedupe InOH...In SFLP (ASCII dots vs unicode ellipsis)
        flp_canon = "InOH···In SFLP"
        flp_aliases = [
            "InOH...In SFLP",
            "InOH···In SFLP",
            "InOH…In SFLP",
            "InOH-In SFLP",
        ]
        frows = []
        for name in flp_aliases:
            frows.extend(
                [
                    {**dict(r), "src_name": name}
                    for r in s.run(
                        """
                        MATCH (f:FLP {name:$n})
                        RETURN elementId(f) AS id, f.name AS name,
                               size([(f)--()|1]) AS deg
                        """,
                        n=name,
                    )
                ]
            )
        if frows:
            frows.sort(key=lambda x: -x["deg"])
            # prefer unicode ellipsis name as survivor display
            surv = frows[0]
            s.run(
                """
                MATCH (f) WHERE elementId(f)=$id
                SET f.name=$n, f.name_key=$nk
                """,
                id=surv["id"],
                n=flp_canon,
                nk=soft_name_key(flp_canon),
            )
            for r in frows[1:]:
                if r["id"] == surv["id"]:
                    continue
                merge_pair(s, surv["id"], r["id"])
                report["flp_merged"].append(r["name"])
                print(f"  MERGED FLP {r['name']} -> {flp_canon}")

        # Clean In2O3 aliases
        s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            SET n:Material, n.name='In2O3', n.name_key=$nk
            WITH n, coalesce(n.aliases, []) AS a
            WITH n, [x IN a + $extra WHERE x IS NOT NULL AND NOT x STARTS WITH '['] AS raw
            UNWIND raw AS x
            WITH n, collect(DISTINCT x) AS uniq
            SET n.aliases = uniq
            """,
            id=sid,
            nk=soft_name_key("In2O3"),
            extra=[
                "In2O3",
                "In(OH)3-x",
                "In2O3-x(OH)y",
                "indium hydroxide",
                "InOH",
                "In(OH)",
            ],
        )

        left = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(n.name) CONTAINS 'in2o3'
                  OR toLower(n.name) CONTAINS 'in(oh)'
                  OR toLower(n.name) CONTAINS 'indium hydroxide'
                  OR toLower(n.name) = 'inoh'
                  OR n.name CONTAINS 'InOH'
                )
                RETURN labels(n)[0] AS lab, n.name AS name,
                       size([(n)--()|1]) AS deg
                ORDER BY name
                """
            )
        ]
        in2o3 = s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            RETURN n.name AS name, labels(n) AS labs, n.aliases AS aliases,
                   size([(n)--()|1]) AS deg
            """,
            id=sid,
        ).single()
        report["in2o3"] = dict(in2o3) if in2o3 else None
        report["remaining"] = left
        report["after"] = dict(
            s.run(
                "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                "RETURN nodes, count(r) AS rels"
            ).single()
        )

    out = ROOT / "out" / "merge_in2o3_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    driver.close()


if __name__ == "__main__":
    main()
