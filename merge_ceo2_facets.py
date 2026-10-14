"""Merge CeO2 crystal-facet / surface / defect aliases into canonical CeO2 nodes.

Rules:
- Pure CeO2 + facet/defect/surface → CeO2
- Composite with only facet suffix (e.g. Ni-CeO2(110)) → base (Ni-CeO2) if present
- Do NOT fold doped/supported catalysts into bare CeO2
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

from src.config_utils import neo4j_password, project_root  # noqa: E402
from src.validate import soft_name_key  # noqa: E402

# Facet / miller-index suffix: (110), (111), (100), etc.
FACET_RE = re.compile(
    r"^(?P<base>.+?)\s*[\(\（]\s*\d{3}\s*[\)\）]\s*$",
    re.I,
)

# Pure CeO2 family to fold into "CeO2"
PURE_ALIASES = {
    "ceo2",
    "ceria",
    "cerium oxide",
    "cerium dioxide",
    "ceo2 surface",
    "ceria surface",
    "defective ceo2",
    "defect ceo2",
    "reduced ceo2",
    "partially reduced ceo2",
}


def normalize(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def strip_facet(name: str) -> str | None:
    m = FACET_RE.match(name.strip())
    if not m:
        return None
    return m.group("base").strip()


def is_pure_ceo2_family(name: str) -> bool:
    n = normalize(name)
    # strip facet first
    base = strip_facet(name)
    if base:
        n = normalize(base)
    # defect/defective/reduced prefixes
    n2 = re.sub(
        r"^(defect(?:ive)?|partially\s+reduced|reduced|pristine)\s+",
        "",
        n,
    )
    n2 = re.sub(r"\s+surface$", "", n2)
    return n2 in {"ceo2", "ceria", "cerium oxide", "cerium dioxide"} or n in PURE_ALIASES


def merge_pair(session, sid: str, did: str) -> None:
    if sid == did:
        return
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
    dname = dup["name"] if dup else ""
    # Ensure survivor keeps Material/Catalyst usefulness: add Material if dup had it
    if dup and "Material" in (dup["labs"] or []):
        session.run(
            "MATCH (s) WHERE elementId(s)=$sid SET s:Material",
            sid=sid,
        )
    if dup and "Catalyst" in (dup["labs"] or []):
        session.run(
            "MATCH (s) WHERE elementId(s)=$sid SET s:Catalyst",
            sid=sid,
        )
    if dup and "Support" in (dup["labs"] or []):
        session.run(
            "MATCH (s) WHERE elementId(s)=$sid SET s:Support",
            sid=sid,
        )

    session.run(
        """
        MATCH (s) WHERE elementId(s)=$sid
        MATCH (d) WHERE elementId(d)=$did
        WITH s, d, coalesce(s.aliases, []) AS a, d.name AS dname
        SET s.aliases = CASE
              WHEN dname IS NULL THEN a
              WHEN dname IN a THEN a
              ELSE a + [dname]
            END
        WITH s, d
        DETACH DELETE d
        """,
        sid=sid,
        did=did,
    )
    print(f"  MERGED {dname} -> survivor")


def ensure_ceo2(session) -> str:
    row = session.run(
        """
        MATCH (n)
        WHERE n.name = 'CeO2' OR n.name_key = 'ceo2'
        RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
               size([(n)--() | 1]) AS deg
        ORDER BY deg DESC
        LIMIT 1
        """
    ).single()
    if row:
        session.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            SET n.name = 'CeO2', n.name_key = $nk
            SET n:Material:Catalyst
            """,
            id=row["id"],
            nk=soft_name_key("CeO2"),
        )
        return row["id"]
    session.run(
        """
        CREATE (n:Material:Catalyst {
          name: 'CeO2',
          name_key: $nk,
          aliases: ['CeO2']
        })
        """,
        nk=soft_name_key("CeO2"),
    )
    return session.run(
        "MATCH (n:Material {name:'CeO2'}) RETURN elementId(n) AS id"
    ).single()["id"]


def find_by_name(session, name: str) -> list[dict]:
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


def find_base_survivor(session, base_name: str) -> str | None:
    """Prefer existing node named exactly base_name; else create Material."""
    rows = find_by_name(session, base_name)
    if rows:
        return rows[0]["id"]
    # try without spaces around /
    alt = base_name.replace(" ", "")
    if alt != base_name:
        rows = find_by_name(session, alt)
        if rows:
            return rows[0]["id"]
    return None


def main() -> None:
    load_dotenv(project_root() / ".env")
    driver = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("neo4j", neo4j_password())
    )
    report: dict = {"pure_into_ceo2": [], "facet_into_base": [], "skipped": []}

    with driver.session() as s:
        ceo2_id = ensure_ceo2(s)

        candidates = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(n.name) CONTAINS 'ceo2'
                  OR toLower(n.name) CONTAINS 'ceria'
                  OR toLower(n.name) CONTAINS 'cerium oxide'
                )
                // do not merge FLP / reaction / product entity names into material CeO2
                AND NOT n:FLP
                AND NOT n:Reaction
                AND NOT n:Product
                AND NOT n:Reactant
                AND NOT n:LewisAcid
                AND NOT n:LewisBase
                AND NOT n:Defect
                AND NOT n:Mechanism
                RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                       size([(n)--() | 1]) AS deg
                ORDER BY name
                """
            )
        ]

        # Pass 1: pure CeO2 family (incl. facets) → CeO2
        for c in candidates:
            if c["id"] == ceo2_id:
                continue
            if is_pure_ceo2_family(c["name"]):
                merge_pair(s, ceo2_id, c["id"])
                report["pure_into_ceo2"].append(c["name"])

        # Refresh candidate list after deletions
        left = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(n.name) CONTAINS 'ceo2'
                  OR toLower(n.name) CONTAINS 'ceria'
                )
                AND NOT n:FLP AND NOT n:Reaction AND NOT n:Product
                AND NOT n:Reactant AND NOT n:LewisAcid AND NOT n:LewisBase
                AND NOT n:Defect AND NOT n:Mechanism
                AND elementId(n) <> $ceo2
                RETURN elementId(n) AS id, n.name AS name, labels(n) AS labs,
                       size([(n)--() | 1]) AS deg
                ORDER BY name
                """,
                ceo2=ceo2_id,
            )
        ]

        # Pass 2: strip facet suffix onto existing base (Ni-CeO2(110) → Ni-CeO2)
        # Also: Ni-CeO2(110)-Ov kept separate (extra Ov qualifier)
        for c in left:
            name = c["name"]
            # skip if has extra qualifier after facet: Ni-CeO2(111)-Ov
            if re.search(r"\)\s*[-–].+$", name):
                report["skipped"].append({"name": name, "reason": "qualifier after facet"})
                continue
            base = strip_facet(name)
            if not base:
                report["skipped"].append({"name": name, "reason": "no facet / not pure"})
                continue
            # Don't fold pure into CeO2 again (already done)
            if is_pure_ceo2_family(name):
                continue
            surv = find_base_survivor(s, base)
            if not surv:
                # create base Material from facet name
                s.run(
                    """
                    CREATE (n:Material {
                      name: $name,
                      name_key: $nk,
                      aliases: [$name]
                    })
                    """,
                    name=base,
                    nk=soft_name_key(base),
                )
                surv = find_by_name(s, base)[0]["id"]
                print(f"  CREATED base {base}")
            if surv == c["id"]:
                continue
            merge_pair(s, surv, c["id"])
            report["facet_into_base"].append({"from": name, "to": base})

        # Finalize CeO2 props
        s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            SET n.name = 'CeO2', n.name_key = $nk
            SET n:Material:Catalyst
            WITH n, coalesce(n.aliases, []) AS a
            WITH n, [x IN a WHERE x IS NOT NULL] +
                 CASE WHEN NOT 'CeO2' IN a THEN ['CeO2'] ELSE [] END AS raw
            UNWIND raw AS x
            WITH n, collect(DISTINCT x) AS uniq
            SET n.aliases = uniq
            """,
            id=ceo2_id,
            nk=soft_name_key("CeO2"),
        )

        remaining = [
            dict(r)
            for r in s.run(
                """
                MATCH (n)
                WHERE n.name IS NOT NULL AND (
                  toLower(n.name) CONTAINS 'ceo2'
                  OR toLower(n.name) CONTAINS 'ceria'
                )
                RETURN labels(n)[0] AS label, n.name AS name,
                       size([(n)--() | 1]) AS deg
                ORDER BY name
                """
            )
        ]
        ceo2 = s.run(
            """
            MATCH (n) WHERE elementId(n)=$id
            RETURN n.name AS name, labels(n) AS labs, n.aliases AS aliases,
                   size([(n)--() | 1]) AS deg
            """,
            id=ceo2_id,
        ).single()
        report["ceo2"] = dict(ceo2) if ceo2 else None
        report["remaining_ceo2_named"] = remaining
        after = s.run(
            "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
            "RETURN nodes, count(r) AS rels"
        ).single()
        report["after"] = dict(after)

    out = ROOT / "out" / "merge_ceo2_facets_report.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
    driver.close()


if __name__ == "__main__":
    main()
