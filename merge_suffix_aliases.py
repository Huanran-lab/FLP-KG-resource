"""Merge near-identical FLP / entity names that differ only by trivial suffixes."""
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
from src.config_utils import neo4j_password, project_root  # noqa: E402
from src.validate import soft_name_key  # noqa: E402

load_dotenv(ROOT / ".env")

# soft_name_key removes spaces, so suffixes are compacted
SUFFIX_RE = re.compile(
    r"(basedporousflpcatalyst|porousflpcatalyst|flpcatalyst|"
    r"activesite|pairflp|flp)$",
    re.I,
)


def canon_key(label: str, name: str) -> str:
    s = soft_name_key(name)
    if label == "FLP":
        s2 = SUFFIX_RE.sub("", s)
        if s2:
            s = s2
    return s


def merge_pair(session, surv: str, dup: str) -> None:
    outs = list(
        session.run(
            """
            MATCH (d)-[r]->(t)
            WHERE elementId(d)=$dup AND elementId(t)<>$surv
            RETURN type(r) AS type, elementId(t) AS tid, properties(r) AS props
            """,
            dup=dup,
            surv=surv,
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
            surv=surv,
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
            dup=dup,
            surv=surv,
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
            surv=surv,
            props=rel["props"] or {},
        )
    session.run("MATCH (d) WHERE elementId(d)=$dup DETACH DELETE d", dup=dup)


def main() -> None:
    driver = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", neo4j_password()),
    )
    examples: list[dict[str, Any]] = []
    merged = 0
    with driver.session() as s:
        rows = [
            dict(r)
            for r in s.run(
                """
                MATCH (n) WHERE n.name IS NOT NULL
                OPTIONAL MATCH (n)-[r]-()
                WITH n, count(r) AS degree
                RETURN elementId(n) AS id, labels(n)[0] AS label, n.name AS name, degree
                """
            )
        ]
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for n in rows:
            groups[(n["label"], canon_key(n["label"], n["name"]))].append(n)

        for (label, key), nodes in groups.items():
            if len(nodes) < 2:
                continue
            # Prefer name that already ends with FLP for FLP label; else highest degree
            def score(n: dict[str, Any]) -> tuple:
                name = n["name"] or ""
                prefer = 1 if label == "FLP" and re.search(r"flp$", name, re.I) else 0
                return (prefer, int(n["degree"]), -len(name))

            nodes_sorted = sorted(nodes, key=score, reverse=True)
            surv = nodes_sorted[0]
            dups = nodes_sorted[1:]
            aliases = [surv["name"]]
            for d in dups:
                aliases.append(d["name"])
                merge_pair(s, surv["id"], d["id"])
                merged += 1
            uniq = []
            for a in aliases:
                if a not in uniq:
                    uniq.append(a)
            s.run(
                "MATCH (n) WHERE elementId(n)=$id SET n.aliases=$a, n.name_key=$k",
                id=surv["id"],
                a=uniq,
                k=key,
            )
            if len(examples) < 20:
                examples.append(
                    {
                        "label": label,
                        "key": key,
                        "survivor": surv["name"],
                        "merged": [d["name"] for d in dups],
                    }
                )

        after = dict(
            s.run(
                "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                "RETURN nodes, count(r) AS relationships"
            ).single()
        )
        flp_n = s.run("MATCH (:FLP) RETURN count(*) AS c").single()["c"]

    report = {
        "merged_nodes": merged,
        "examples": examples,
        "after": after,
        "flp_nodes": flp_n,
    }
    path = project_root() / "out" / "suffix_merge_report.json"
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print("wrote", path)
    driver.close()


if __name__ == "__main__":
    main()
