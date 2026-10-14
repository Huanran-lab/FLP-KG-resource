"""Aggressive but safe graph dedupe for solid-FLP Neo4j KG.

Merges:
1) Exact same display name (any labels) -> one node, union labels
2) Soft-key / synonym equivalents within label families
3) Known cross-label families (Material/Catalyst/Support, FLP/ActiveSite, etc.)
"""
from __future__ import annotations

import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.config_utils import load_schema, neo4j_password  # noqa: E402
from src.validate import apply_synonyms, soft_name_key  # noqa: E402

# Label families that may be merged when soft/syn key matches
FAMILIES: list[set[str]] = [
    {"Material", "Catalyst", "Support"},
    {"FLP", "ActiveSite"},
    {"LewisAcid", "ActiveSite", "Defect"},
    {"LewisBase", "ActiveSite", "Defect", "Product", "Molecule", "Intermediate"},
    {"Product", "Molecule", "Intermediate", "Reactant"},
    {"Reaction", "ReactionStep", "Activity"},
    {"Descriptor", "EnergeticDescriptor", "Mechanism"},
]

# Prefer these labels when choosing survivor display label (first wins)
LABEL_PRIORITY = [
    "FLP",
    "Material",
    "Catalyst",
    "Support",
    "LewisAcid",
    "LewisBase",
    "Defect",
    "ActiveSite",
    "Reaction",
    "ReactionStep",
    "Activity",
    "Product",
    "Molecule",
    "Intermediate",
    "Reactant",
    "Mechanism",
    "Descriptor",
    "EnergeticDescriptor",
]


def load_synonyms() -> dict[str, list[str]]:
    syn = dict(load_schema().get("synonyms") or {})
    path = ROOT / "synonyms_extra.yaml"
    if path.exists():
        syn.update(yaml.safe_load(path.read_text(encoding="utf-8")) or {})
    return syn


def family_id(lab: str) -> str | None:
    for i, fam in enumerate(FAMILIES):
        if lab in fam:
            return f"F{i}"
    return f"L:{lab}"  # singleton family = same label only


def merge_key(name: str, lab: str, synonyms: dict) -> tuple[str, str]:
    key = soft_name_key(apply_synonyms(name, synonyms))
    # adsorbate star normalization already in soft_name_key
    return family_id(lab) or f"L:{lab}", key


def choose_survivor(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    def rank(n: dict) -> tuple:
        labs = n.get("labs") or [n.get("lab")]
        pri = 999
        for i, p in enumerate(LABEL_PRIORITY):
            if p in labs:
                pri = i
                break
        return (pri, -int(n.get("deg") or 0), len(n.get("name") or ""), n["id"])

    return sorted(nodes, key=rank)[0]


def merge_pair(session, sid: str, did: str) -> bool:
    if sid == did:
        return False
    # clear unique keys on dup
    session.run(
        "MATCH (d) WHERE elementId(d)=$did SET d.name_key = null",
        did=did,
    )
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
    if not dup:
        return False
    dname = dup["name"] or ""
    dlabs = [x for x in (dup["labs"] or []) if x]

    # Union labels onto survivor (skip conflicting unique-key labels carefully)
    for lab in dlabs:
        try:
            session.run(
                f"MATCH (s) WHERE elementId(s)=$sid SET s:{lab}",
                sid=sid,
            )
        except Exception:
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
    return True


def fetch_nodes(session) -> list[dict[str, Any]]:
    return [
        dict(r)
        for r in session.run(
            """
            MATCH (n) WHERE n.name IS NOT NULL
            RETURN elementId(n) AS id, n.name AS name,
                   labels(n) AS labs, labels(n)[0] AS lab,
                   size([(n)--()|1]) AS deg
            """
        )
    ]


def main() -> None:
    load_dotenv(ROOT / ".env")
    synonyms = load_synonyms()
    driver = GraphDatabase.driver(
        "bolt://localhost:7687", auth=("neo4j", neo4j_password())
    )
    report: dict[str, Any] = {"merged": 0, "groups": 0, "examples": []}

    with driver.session() as s:
        before = dict(
            s.run(
                "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                "RETURN nodes, count(r) AS rels"
            ).single()
        )
        report["before"] = before

        # Pass A: exact same name (case-sensitive) across any labels
        nodes = fetch_nodes(s)
        by_exact: dict[str, list] = defaultdict(list)
        for n in nodes:
            by_exact[n["name"]].append(n)

        for name, members in sorted(by_exact.items(), key=lambda kv: -len(kv[1])):
            if len(members) < 2:
                continue
            # skip charge-confusable single letters? keep merging
            surv = choose_survivor(members)
            for m in members:
                if m["id"] == surv["id"]:
                    continue
                still = s.run(
                    "MATCH (n) WHERE elementId(n)=$id RETURN n.name AS name",
                    id=m["id"],
                ).single()
                if not still:
                    continue
                if merge_pair(s, surv["id"], m["id"]):
                    report["merged"] += 1
            report["groups"] += 1
            if len(report["examples"]) < 40:
                report["examples"].append(
                    {"kind": "exact", "name": name, "n": len(members)}
                )

        # Pass B: soft/syn within families
        nodes = fetch_nodes(s)
        by_fam: dict[tuple[str, str], list] = defaultdict(list)
        for n in nodes:
            lab = n["lab"]
            # for multi-label nodes, pick best family among labels
            fams = []
            for lab2 in n["labs"] or [lab]:
                fams.append(merge_key(n["name"], lab2, synonyms))
            # use highest-priority family's key
            key = None
            for p in LABEL_PRIORITY:
                if p in (n["labs"] or []):
                    key = merge_key(n["name"], p, synonyms)
                    break
            if key is None:
                key = merge_key(n["name"], lab, synonyms)
            by_fam[key].append(n)

        for (fam, key), members in sorted(by_fam.items(), key=lambda kv: -len(kv[1])):
            # unique by id
            uniq = {}
            for m in members:
                uniq[m["id"]] = m
            members = list(uniq.values())
            if len(members) < 2:
                continue
            # Don't merge if names differ only by charge-like trailing signs differently
            # soft_name_key already keeps + -
            surv = choose_survivor(members)
            # Canonical display: prefer shortest non-empty among high-degree
            canon_name = surv["name"]
            for m in members:
                if m["id"] == surv["id"]:
                    continue
                still = s.run(
                    "MATCH (n) WHERE elementId(n)=$id RETURN n.name AS name",
                    id=m["id"],
                ).single()
                if not still:
                    continue
                if merge_pair(s, surv["id"], m["id"]):
                    report["merged"] += 1
            # refresh name_key
            s.run(
                """
                MATCH (n) WHERE elementId(n)=$id
                SET n.name = $name, n.name_key = $nk
                """,
                id=surv["id"],
                name=canon_name,
                nk=soft_name_key(apply_synonyms(canon_name, synonyms)),
            )
            report["groups"] += 1
            if len(report["examples"]) < 80:
                names = sorted({m["name"] for m in members})
                report["examples"].append(
                    {"kind": "family", "fam": fam, "key": key, "names": names[:8]}
                )

        # Pass C: facet/surface leftover materials (best-effort)
        from scripts.merge_facet_surface_aliases import parse_variant, ensure_base

        nodes = fetch_nodes(s)
        for n in nodes:
            labs = set(n["labs"] or [])
            if not labs & {"Material", "Catalyst", "Support", "Product", "Reactant"}:
                continue
            parsed = parse_variant(n["name"])
            if not parsed:
                continue
            base, _ = parsed
            if soft_name_key(base) == soft_name_key(n["name"]):
                continue
            try:
                sid = ensure_base(s, base, list(n["labs"] or []))
            except Exception as exc:
                report.setdefault("facet_errors", []).append(
                    {"name": n["name"], "err": str(exc)}
                )
                continue
            if sid == n["id"]:
                continue
            still = s.run(
                "MATCH (x) WHERE elementId(x)=$id RETURN x.name AS name", id=n["id"]
            ).single()
            if still and merge_pair(s, sid, n["id"]):
                report["merged"] += 1
                report["examples"].append(
                    {"kind": "facet", "from": n["name"], "to": base}
                )

        # Pass D: any remaining identical soft keys across ALL labels
        # (except keep Material vs LewisAcid when names are BC/BN/NC-like diatomics)
        KEEP_SEPARATE = re.compile(r"^(bc|bn|nc|bo|no|cn)$", re.I)
        nodes = fetch_nodes(s)
        by_soft: dict[str, list] = defaultdict(list)
        for n in nodes:
            by_soft[soft_name_key(apply_synonyms(n["name"], synonyms))].append(n)
        for key, members in by_soft.items():
            uniq = {m["id"]: m for m in members}
            members = list(uniq.values())
            if len(members) < 2:
                continue
            if KEEP_SEPARATE.match(key):
                continue
            labs = {lab for m in members for lab in (m["labs"] or [])}
            # don't fold Material into acid/base for elemental names length<=2
            if "Material" in labs and labs & {"LewisAcid", "LewisBase"} and len(key) <= 2:
                continue
            surv = choose_survivor(members)
            for m in members:
                if m["id"] == surv["id"]:
                    continue
                still = s.run(
                    "MATCH (n) WHERE elementId(n)=$id RETURN 1 AS ok", id=m["id"]
                ).single()
                if still and merge_pair(s, surv["id"], m["id"]):
                    report["merged"] += 1

        # Clean aliases junk
        s.run(
            """
            MATCH (n) WHERE n.aliases IS NOT NULL
            WITH n, [x IN coalesce(n.aliases,[])
                     WHERE x IS NOT NULL AND NOT toString(x) STARTS WITH '['] AS a
            SET n.aliases = a
            """
        )

        after = dict(
            s.run(
                "MATCH (n) WITH count(n) AS nodes MATCH ()-[r]->() "
                "RETURN nodes, count(r) AS rels"
            ).single()
        )
        report["after"] = after
        flp = s.run("MATCH (:FLP) RETURN count(*) AS c").single()["c"]
        report["flp"] = flp

        # residual exact dups
        residual = list(
            s.run(
                """
                MATCH (n) WHERE n.name IS NOT NULL
                WITH n.name AS name, count(*) AS c
                WHERE c > 1
                RETURN name, c ORDER BY c DESC LIMIT 30
                """
            )
        )
        report["residual_exact"] = [dict(r) for r in residual]

    out = ROOT / "out" / "dedupe_graph_report.json"
    out.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(
        f"before={report['before']} after={report['after']} "
        f"merged={report['merged']} groups={report['groups']} flp={report['flp']}"
    )
    print("residual exact:", report["residual_exact"][:15])
    driver.close()


if __name__ == "__main__":
    main()
