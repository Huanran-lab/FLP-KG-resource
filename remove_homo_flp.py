"""Find and remove homogeneous (non-solid) FLP content from Neo4j."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.config_utils import neo4j_password  # noqa: E402

load_dotenv(ROOT / ".env")

SOLID_NAME = re.compile(
    r"(surface|solid|hetero|oxide|ceo2|tio2|zro2|al2o3|in2o3|zno|mof|cof|"
    r"zeolite|fau|porous|vacanc|defect|support|interface|lattice|"
    r"carbon nitride|pcn|g-c3n4|wo3|nio|cuo|doped|cluster|"
    r"\b[a-z]{0,3}-?o\b|/o\b|o\(h\)|oh\.\.\.|sflp|cflp|"
    r"mil-|uio-|sion-|heos?|hap|hydroxyapatite|phi|polyheptazine|"
    r"perovskite|graphene|alooh|bn|tungsten|zn-o|ti-o|ce-o|"
    r"on\s+\w+|@)",
    re.I,
)

HOMO_STRONG = re.compile(
    r"("
    # BCF alone is NOT enough — often immobilized on MOF/COF/frameworks
    r"b\(c6f5\)3|b\(p-c6f4h\)3|bph3|"
    r"p\(t-?bu\)3|ptbu3|tbu3p|pmes3|"
    r"\(c6f5\)3[pb]|\(c6h2me3\)3p|"
    r"1-tmp-2-bh2|tmp-h|lutidine/|"
    r"phosphino-?borane|phosphine/?borane|phosphido-?borane|"
    r"bisphosphino|phosphinoferrocene|carbene/b\(|"
    r"open tbu3p/|zirconocene amine|"
    r"intramolecular (?:p-b )?flp|"
    r"compound\s+\d+|substituted flp\s+\d+|"
    r"alkenylene-linked|macromolecular flp|"
    r"\b\d{2,3}[a-z]?(?:/\d{2,3}[a-z]?)?\s*/\s*b\("
    r")",
    re.I,
)

HOMO_GENERIC = re.compile(
    r"^(frustrated lewis pair|phosphine-borane adduct|dynamic flp|"
    r"modified flp(?: with chiral centers)?|rotation-activated flp|"
    r"flp network|flp-mediated bifunctional catalyst|"
    r"homogeneous flp|molecular flp|solution[- ]phase flp)$",
    re.I,
)


def classify(row: dict) -> str:
    """Only delete when strong molecular markers are present.

    Missing LOCATED_ON/ENABLES alone is NOT enough — many solid FLPs were
    extracted without those edges and must not be purged.
    """
    name = row["name"] or ""
    mats = [m for m in (row.get("mats") or []) if m]
    defect_links = int(row.get("defect_links") or 0)
    has_solid_link = bool(mats) or defect_links > 0
    if has_solid_link or SOLID_NAME.search(name):
        return "solid"
    if HOMO_STRONG.search(name) or HOMO_GENERIC.search(name.strip()):
        return "homo"
    # compound codes like 25a / 136a without solid context
    if re.search(r"\b\d{2,3}[a-z]\b", name) and HOMO_STRONG.search(name):
        return "homo"
    # ambiguous / unknown → keep (do not treat as homo)
    return "solid"


def main() -> None:
    driver = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("neo4j", neo4j_password())
    )
    report: dict = {"homo_flp": [], "solid_flp": [], "deleted": {}}
    with driver.session() as s:
        before = dict(
            s.run(
                "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                "RETURN nodes, count(r) AS relationships"
            ).single()
        )
        flps = [
            dict(r)
            for r in s.run(
                """
                MATCH (f:FLP)
                OPTIONAL MATCH (f)-[:LOCATED_ON|SUPPORTED_ON|HAS_COMPONENT]->(m)
                OPTIONAL MATCH (def:Defect)-[:ENABLES]->(f)
                OPTIONAL MATCH (mat:Material)-[:HAS_DEFECT]->(def)
                WITH f,
                     collect(DISTINCT coalesce(m.name, mat.name)) AS mats,
                     count(def) AS defect_links
                RETURN elementId(f) AS id, f.name AS name, mats, defect_links
                ORDER BY name
                """
            )
        ]
        homo_ids = []
        for f in flps:
            bucket = classify(f)
            item = {"name": f["name"], "mats": f["mats"], "defect_links": f["defect_links"]}
            if bucket == "solid":
                report["solid_flp"].append(item)
            else:
                report["homo_flp"].append(item)
                homo_ids.append(f["id"])

        # Also catch classic molecular acid/base only used by homo FLPs later via orphan prune
        # Delete homo FLPs and exclusive partners
        deleted_partners = 0
        if homo_ids:
            deleted_partners = s.run(
                """
                UNWIND $ids AS fid
                MATCH (f:FLP) WHERE elementId(f) = fid
                OPTIONAL MATCH (f)-[:HAS_ACID_SITE|HAS_BASE_SITE|FORMS_PAIR_WITH]-(ab)
                WITH collect(DISTINCT f) AS flps, collect(DISTINCT ab) AS partners
                FOREACH (f IN flps | DETACH DELETE f)
                WITH partners
                UNWIND partners AS p
                WITH p WHERE p IS NOT NULL
                  AND NOT EXISTS { MATCH (p)--(:FLP) }
                  AND NOT EXISTS {
                    MATCH (p)--(x)
                    WHERE x:Material OR x:Defect OR x:Catalyst OR x:Support OR x:ActiveSite
                  }
                DETACH DELETE p
                RETURN count(p) AS deleted_partners
                """,
                ids=homo_ids,
            ).single()["deleted_partners"]

        # Molecular-only nodes by name (no solid FLP link)
        mol_deleted = s.run(
            """
            MATCH (n)
            WHERE n.name IS NOT NULL
              AND NOT n:FLP
              AND (
                toLower(n.name) CONTAINS 'b(c6f5)3'
                OR toLower(n.name) CONTAINS 'tbu3p'
                OR toLower(n.name) CONTAINS 'p(tbu)3'
                OR toLower(n.name) CONTAINS 'phosphino-borane'
                OR toLower(n.name) CONTAINS 'phosphine-borane'
              )
              AND NOT EXISTS { MATCH (n)-[*1..2]-(:FLP) }
            WITH collect(n) AS ns
            FOREACH (n IN ns | DETACH DELETE n)
            RETURN size(ns) AS c
            """
        ).single()["c"]

        # Do NOT mass-delete all non-FLP-adjacent nodes (would wipe solid context).
        # Only remove nodes that look molecular AND have no FLP path.
        orphans = 0

        after = dict(
            s.run(
                "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                "RETURN nodes, count(r) AS relationships"
            ).single()
        )
        flp_left = s.run("MATCH (:FLP) RETURN count(*) AS c").single()["c"]

    report["before"] = before
    report["deleted"] = {
        "homo_flp_count": len(homo_ids),
        "exclusive_partners": deleted_partners,
        "molecular_orphans": mol_deleted,
        "non_flp_orphans_2hop": orphans,
    }
    report["after"] = after
    report["flp_remaining"] = flp_left
    out = ROOT / "out" / "remove_homo_flp_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({
        "homo_found": len(report["homo_flp"]),
        "homo_names": [x["name"] for x in report["homo_flp"]],
        "solid_count": len(report["solid_flp"]),
        "deleted": report["deleted"],
        "before": before,
        "after": after,
        "flp_remaining": flp_left,
    }, ensure_ascii=False, indent=2))
    print("report:", out)
    driver.close()


if __name__ == "__main__":
    main()
