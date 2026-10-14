"""Merge facet / surface / plane variants into their base material nodes.

Examples:
  ZnO (100) surface, ZnO (100) -> ZnO
  MgO(111) -> MgO
  γ-Al2O3(110) -> γ-Al2O3
  P-doped ZnO (100) -> P-doped ZnO
  Au(111), Au surface -> Au (if Au exists or is created)

Does NOT merge FLP / Reaction / Defect / Lewis sites.
"""
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

# base + optional (hkl) + optional surface/facet/plane
VARIANT_RE = re.compile(
    r"^(?P<base>.+?)\s*"
    r"(?:"
    r"[\(\（]\s*(?P<hkl>\d{3})\s*[\)\）]"
    r")\s*"
    r"(?P<suf>surface|facet|plane|termination)?"
    r"\s*$"
    r"|"
    r"^(?P<base2>.+?)\s+(?P<suf2>surface|facet|plane|termination)\s*$",
    re.I,
)

SKIP_LABELS = {
    "FLP",
    "Reaction",
    "Defect",
    "LewisAcid",
    "LewisBase",
    "Mechanism",
    "Molecule",
    "Condition",
}


def parse_variant(name: str) -> tuple[str, str] | None:
    """Return (base, original) if name is a facet/surface variant."""
    name = name.strip()
    m = VARIANT_RE.match(name)
    if not m:
        return None
    if m.group("base") is not None:
        base = m.group("base").strip()
        hkl = m.group("hkl")
        suf = m.group("suf")
        if not hkl and not suf:
            return None
        return base, name
    base = (m.group("base2") or "").strip()
    if not base:
        return None
    # avoid folding generic "surface" words that aren't materials
    if len(base) < 2 or len(base) > 60:
        return None
    return base, name


def merge_pair(session, sid: str, did: str) -> str:
    if sid == did:
        return ""
    outs = list(
        session.run(
            """
            MATCH (d)-[r]->(t)
            WHERE elementId(d)=$did AND elementId(t)<>$sid
            RETURN type(r) AS type, elementId(t) AS tid, properties(r) AS props
            """,
            did=did,
            sid=sid,
        )
    )
    for rel in outs:
        session.run(
            f"""
            MATCH (s) WHERE elementId(s)=$sid
            MATCH (t) WHERE elementId(t)=$tid
            MERGE (s)-[nr:{rel['type']}]->(t)
            SET nr += $props
            """,
            sid=sid,
            tid=rel["tid"],
            props=rel["props"] or {},
        )
    ins = list(
        session.run(
            """
            MATCH (f)-[r]->(d)
            WHERE elementId(d)=$did AND elementId(f)<>$sid
            RETURN type(r) AS type, elementId(f) AS fid, properties(r) AS props
            """,
            did=did,
            sid=sid,
        )
    )
    for rel in ins:
        session.run(
            f"""
            MATCH (f) WHERE elementId(f)=$fid
            MATCH (s) WHERE elementId(s)=$sid
            MERGE (f)-[nr:{rel['type']}]->(s)
            SET nr += $props
            """,
            fid=rel["fid"],
            sid=sid,
            props=rel["props"] or {},
        )

    dup = session.run(
        "MATCH (d) WHERE elementId(d)=$did RETURN d.name AS name, labels(d) AS labs",
        did=did,
    ).single()
    dname = (dup["name"] if dup else "") or ""
    labs = set(dup["labs"] or []) if dup else set()

    # Clear unique keys on duplicate first to avoid constraint clashes
    session.run(
        """
        MATCH (d) WHERE elementId(d)=$did
        SET d.name_key = null
        """,
        did=did,
    )

    for lab in ("Material", "Catalyst", "Support"):
        if lab in labs:
            try:
                session.run(
                    f"MATCH (s) WHERE elementId(s)=$sid SET s:{lab}",
                    sid=sid,
                )
            except Exception:
                # uniqueness on (:Support|:Catalyst {name_key}) etc.
                pass

    session.run(
        """
        MATCH (s) WHERE elementId(s)=$sid
        MATCH (d) WHERE elementId(d)=$did
        WITH s, d, coalesce(s.aliases, []) AS a, d.name AS dname
        SET s.aliases = CASE
              WHEN dname IS NULL THEN a
              WHEN dname IN a THEN a
              ELSE a + [dname]
            END,
            d.name_key = null
        WITH s, d
        DETACH DELETE d
        """,
        sid=sid,
        did=did,
    )
    return dname


def find_nodes_by_name(session, name: str) -> list[dict]:
    return [
        dict(r)
        for r in session.run(
            """
            MATCH (n)
            WHERE n.name = $name
            RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                   size([(n)--() | 1]) AS deg
            ORDER BY deg DESC
            """,
            name=name,
        )
    ]


def find_nodes_by_name_ci(session, name: str) -> list[dict]:
    return [
        dict(r)
        for r in session.run(
            """
            MATCH (n)
            WHERE toLower(n.name) = toLower($name)
            RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                   size([(n)--() | 1]) AS deg
            ORDER BY deg DESC
            """,
            name=name,
        )
    ]


def ensure_base(session, base: str, seed_labs: list[str]) -> str:
    rows = find_nodes_by_name(session, base)
    if not rows:
        rows = find_nodes_by_name_ci(session, base)
    # Also find by name_key uniqueness (Material/Catalyst/Support)
    if not rows:
        nk = soft_name_key(base)
        rows = [
            dict(r)
            for r in session.run(
                """
                MATCH (n)
                WHERE n.name_key = $nk
                  AND (n:Material OR n:Catalyst OR n:Support)
                RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                       size([(n)--() | 1]) AS deg
                ORDER BY deg DESC
                """,
                nk=nk,
            )
        ]
    if rows:
        # Prefer Material/Catalyst/Support over Reactant/Product
        def rank(r: dict) -> tuple:
            labs = set(r["labs"] or [])
            prefer = 0 if labs & {"Material", "Catalyst", "Support"} else 1
            return (prefer, -r["deg"])

        rows.sort(key=rank)
        sid = rows[0]["id"]
        # merge same-name duplicates first
        for r in rows[1:]:
            if not (set(r["labs"] or []) & SKIP_LABELS):
                merge_pair(session, sid, r["id"])
        return sid

    primary = "Material"
    for lab in ("Material", "Catalyst", "Support"):
        if lab in seed_labs:
            primary = lab
            break
    nk = soft_name_key(base)
    try:
        session.run(
            f"""
            CREATE (n:{primary} {{
              name: $name,
              name_key: $nk,
              aliases: [$name]
            }})
            """,
            name=base,
            nk=nk,
        )
        print(f"  CREATED {primary}:{base}")
    except Exception:
        # race / unique constraint — fetch existing
        hit = session.run(
            """
            MATCH (n)
            WHERE n.name_key = $nk OR n.name = $name
            RETURN elementId(n) AS id
            ORDER BY size([(n)--()|1]) DESC
            LIMIT 1
            """,
            nk=nk,
            name=base,
        ).single()
        if hit:
            return hit["id"]
        raise
    return find_nodes_by_name(session, base)[0]["id"]


def main() -> None:
    load_dotenv(ROOT / ".env")
    driver = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("neo4j", neo4j_password())
    )
    report: dict = {"merged": [], "created_bases": [], "skipped": []}

    with driver.session() as s:
        candidates = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL
                RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                       size([(n)--() | 1]) AS deg
                ORDER BY name
                """
            )
        ]

        # Collect (base -> list of variant node ids)
        groups: dict[str, list[dict]] = {}
        for c in candidates:
            labs = set(c["labs"] or [])
            if labs & SKIP_LABELS:
                continue
            parsed = parse_variant(c["name"])
            if not parsed:
                continue
            base, _ = parsed
            # skip absurd bases
            if base.lower() in {"the", "a", "an", "on", "of"}:
                continue
            groups.setdefault(base, []).append(c)

        for base, variants in sorted(groups.items()):
            # seed labels from highest-degree variant
            variants.sort(key=lambda x: -x["deg"])
            seed_labs = list(variants[0]["labs"] or [])
            sid = ensure_base(s, base, seed_labs)
            # normalize survivor name/key
            s.run(
                """
                MATCH (n) WHERE elementId(n)=$id
                SET n.name = $name, n.name_key = $nk
                """,
                id=sid,
                name=base,
                nk=soft_name_key(base),
            )
            for v in variants:
                if v["id"] == sid:
                    continue
                # re-check node still exists
                still = s.run(
                    "MATCH (n) WHERE elementId(n)=$id RETURN n.name AS name",
                    id=v["id"],
                ).single()
                if not still:
                    continue
                dname = merge_pair(s, sid, v["id"])
                if dname:
                    report["merged"].append({"from": dname, "to": base})
                    print(f"  MERGED {dname} -> {base}")

        # Also fold spaced facet forms already covered; second pass for
        # near-duplicates like "P-doped ZnO(100)" vs "P-doped ZnO (100)"
        # (both map to same base via parse)

        # Clean aliases lists (drop stringified junk)
        s.run(
            """
            MATCH (n)
            WHERE n.aliases IS NOT NULL
            WITH n, [x IN coalesce(n.aliases, []) WHERE x IS NOT NULL AND NOT x STARTS WITH '['] AS a
            SET n.aliases = a
            """
        )

        remaining = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  n.name =~ '.*[(\\\\uff08]\\\\s*\\\\d{3}\\\\s*[)\\\\uff09].*'
                  OR toLower(n.name) ENDS WITH ' surface'
                  OR toLower(n.name) ENDS WITH ' facet'
                )
                AND NOT n:FLP AND NOT n:Reaction AND NOT n:Defect
                AND NOT n:LewisAcid AND NOT n:LewisBase
                RETURN labels(n)[0] AS lab, n.name AS name,
                       size([(n)--() | 1]) AS deg
                ORDER BY name
                """
            )
        ]
        report["remaining_facet_like"] = remaining
        after = s.run(
            "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
            "RETURN nodes, count(r) AS rels"
        ).single()
        report["after"] = dict(after)

        # spotlight ZnO / MgO / Al2O3
        for name in ("ZnO", "MgO", "γ-Al2O3", "Au", "gold"):
            row = s.run(
                """
                MATCH (n) WHERE n.name = $name
                RETURN n.name AS name, labels(n) AS labs, n.aliases AS aliases,
                       size([(n)--() | 1]) AS deg
                """,
                name=name,
            ).single()
            if row:
                report.setdefault("spotlight", {})[name] = dict(row)

    out = ROOT / "out" / "merge_facet_surface_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    driver.close()


if __name__ == "__main__":
    main()
