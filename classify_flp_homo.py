"""Classify FLP nodes as solid/hetero vs homogeneous molecular."""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env")

SOLID_NAME = re.compile(
    r"(surface|solid|hetero|oxide|ceo2|tio2|zro2|al2o3|ino3|zno|mof|cof|"
    r"zeolite|fau|porous|vacanc|defect|support|interface|lattice|"
    r"carbon nitride|pcn|g-c3n4|wo3|nio|cuo|bi-o|ce-o|cu-o|"
    r"on\s+\w|@|doped|cluster)",
    re.I,
)
# Classic molecular FLP markers (homogeneous organo P/B/N pairs)
HOMO_NAME = re.compile(
    r"("
    r"b\(c6f5\)3|b\(p-c6f4h\)3|bph3|bcf|"
    r"p\(t-?bu\)3|pmes3|ptbu3|pr3|"
    r"\(c6f5\)3[pb]|\(c6h2me3\)3p|"
    r"tmp|tmp-h|1-tmp|"
    r"\b\d{2,3}[a-z]?(?:/\d{2,3}[a-z]?)?(?:\s*/\s*b\()"  # compound numbers like 136a/B(
    r"|metal[- ]?free"
    r")",
    re.I,
)


def main() -> None:
    d = GraphDatabase.driver(
        "bolt://localhost:7687",
        auth=("neo4j", os.getenv("NEO4J_PASSWORD")),
    )
    with d.session() as s:
        rows = list(
            s.run(
                """
                MATCH (f:FLP)
                OPTIONAL MATCH (f)-[:LOCATED_ON|SUPPORTED_ON]->(m)
                OPTIONAL MATCH (def:Defect)-[:ENABLES]->(f)
                OPTIONAL MATCH (mat:Material)-[:HAS_DEFECT]->(def)
                WITH f,
                     collect(DISTINCT coalesce(m.name, mat.name)) AS mats,
                     count(def) AS defect_links
                RETURN f.name AS name,
                       elementId(f) AS id,
                       mats,
                       defect_links,
                       [(f)-[:HAS_ACID_SITE]->(a) | a.name][0..5] AS acids,
                       [(f)-[:HAS_BASE_SITE]->(b) | b.name][0..5] AS bases
                ORDER BY name
                """
            )
        )
        solid, homo, unclear = [], [], []
        for r in rows:
            name = r["name"] or ""
            mats = [m for m in (r["mats"] or []) if m]
            has_solid_link = bool(mats) or int(r["defect_links"] or 0) > 0
            solidish_name = bool(SOLID_NAME.search(name))
            homish_name = bool(HOMO_NAME.search(name))

            if has_solid_link or solidish_name:
                # if strongly molecular name but solid-linked, keep as solid
                solid.append(r)
            elif homish_name:
                homo.append(r)
            else:
                # no solid evidence: treat short numbered / slash molecular names as homo
                if re.fullmatch(r"[\w()/\-\s]+", name) and (
                    "/" in name or re.search(r"\b\d{2}[a-z]?\b", name)
                ):
                    homo.append(r)
                else:
                    unclear.append(r)

        print(f"solid={len(solid)} homo={len(homo)} unclear={len(unclear)}")
        print("\n--- HOMO ---")
        for r in homo:
            print(r["name"])
        print("\n--- UNCLEAR ---")
        for r in unclear:
            print(r["name"], "| mats=", r["mats"], "def=", r["defect_links"])
        print("\n--- SOLID sample ---")
        for r in solid[:40]:
            print(r["name"], "| mats=", r["mats"][:3])
    d.close()


if __name__ == "__main__":
    main()
