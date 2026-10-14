"""Restore wrongly deleted solid FLPs from beifen backup; remove residual molecular FLPs."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

from dotenv import load_dotenv
from neo4j import GraphDatabase

ROOT = Path(__file__).resolve().parents[1]
BEIFEN = Path(r"C:\Users\huanr\Desktop\beifen\graph.json")

# Known molecular / homogeneous FLPs — do not restore
WRONG_HOMO = {
    "(C6F5)3P/B(C6F5)3 FLP",
    "(C6H2Me3)2PCH2CH2B(C6F5)2",
    "(C6H2Me3)3P/BPh3 FLP",
    "(PhMe)Al(C6F5)3/tBu3P FLP",
    "1-TMP-2-BH2-C6H4 FLP",
    "136a/B(C6F5)3 FLP",
    "136b/B(C6F5)3 FLP",
    "136c/B(C6F5)3 FLP",
    "136d/B(C6F5)3 FLP",
    "25a",
    "25a/27a",
    "31/B(C6F5)3",
    "B(C6F5)3/tBu3P FLP",
    "B(p-C6F4H)3/PR3 FLP",
    "carbene/B(C6F5)3 FLP",
    "intramolecular FLP 25a",
    "intramolecular P-B FLP 25a",
    "lutidine/B(C6F5)3 FLP",
    "open tBu3P/B(C6F5)3 pair",
    "substituted FLP 26b",
    "substituted FLP 26c",
    "substituted FLP 26d",
    "tBu3P/B(C6F5)3",
    "tBu3P/B(C6H2Me3)3 FLP",
    "tBu3P/BPh3 FLP",
    "zirconocene amine FLP",
    "zirconocene-based FLP",
    "alkenylene-linked FLP",
    "amine-borane FLP",
    "macromolecular FLP",
    "phosphino-borane 11a",
    "phosphino-borane FLP",
    "phosphinoferrocene/B(C6F5)3 FLP",
    "B(C6F5)3/(o-C6H4Me)3P FLP",
    "rotation-activated FLP",
    "FLP network",
    "FLP-mediated bifunctional catalyst",
}

MOL_NAME_RE = re.compile(
    r"b\(c6f5\)|tbu3p|phosphino-borane|phosphinoferrocene|phosphine-borane|"
    r"zirconocene|lutidine/|carbene/b|\btmp\b|alkenylene-linked|"
    r"macromolecular flp|amine-borane flp|rotation-activated",
    re.I,
)


# Generic aliases that should be merged into surface FLP, not restored as nodes
SKIP_RESTORE = {
    "frustrated Lewis pair",
    "Frustrated Lewis pair",
    "frustrated lewis pair",
    "dynamic frustrated Lewis pair",
    "FLP",
    "FLP mechanism",
}


def is_molecular_name(name: str) -> bool:
    low = name.lower()
    if name in WRONG_HOMO or name in SKIP_RESTORE:
        return True
    if MOL_NAME_RE.search(low):
        return True
    if re.match(r"^\d{2,3}[a-z]", low):
        return True
    return False


def main() -> None:
    load_dotenv(ROOT / ".env")
    password = os.getenv("NEO4J_PASSWORD", "testtest")
    uri = os.getenv("NEO4J_URI", "bolt://localhost:7687")
    user = os.getenv("NEO4J_USER", "neo4j")

    g = json.loads(BEIFEN.read_text(encoding="utf-8"))
    driver = GraphDatabase.driver(uri, auth=(user, password))

    with driver.session() as s:
        existing = {r["name"] for r in s.run("MATCH (f:FLP) RETURN f.name AS name")}
        print(f"existing FLP: {len(existing)}")

        props_by: dict[str, tuple[list[str], dict]] = {}
        for n in g["nodes"]:
            props = dict(n.get("properties") or {})
            name = props.get("name") or n.get("name")
            if not name:
                continue
            labs = n.get("labels") or ["Entity"]
            props_by[name] = (labs, props)

        restore_nodes = []
        for n in g["nodes"]:
            if "FLP" not in (n.get("labels") or []):
                continue
            props = dict(n.get("properties") or {})
            name = props.get("name") or n.get("name")
            if not name or name in existing:
                continue
            if is_molecular_name(name):
                continue
            restore_nodes.append((name, props))

        print(f"to restore from beifen: {len(restore_nodes)}")
        for name, _ in restore_nodes:
            print(f"  + {name}")

        restore_names = {name for name, _ in restore_nodes}
        rel_restore = [
            e
            for e in g["relationships"]
            if e.get("source_name") in restore_names or e.get("target_name") in restore_names
        ]
        print(f"edges to restore: {len(rel_restore)}")

        needed = set()
        for e in rel_restore:
            needed.add(e["source_name"])
            needed.add(e["target_name"])

        created = 0
        for name in needed:
            if name not in props_by:
                continue
            labs, props = props_by[name]
            primary = labs[0]
            c = s.run(
                f"MATCH (n:{primary} {{name: $name}}) RETURN count(n) AS c",
                name=name,
            ).single()["c"]
            if c == 0:
                s.run(
                    f"MERGE (n:{primary} {{name: $name}}) SET n += $props",
                    name=name,
                    props=props,
                )
                created += 1
        print(f"created missing endpoints: {created}")

        for name, props in restore_nodes:
            s.run(
                "MERGE (n:FLP {name: $name}) SET n += $props",
                name=name,
                props=props,
            )

        er = 0
        for e in rel_restore:
            rtype = e["type"]
            sn, tn = e["source_name"], e["target_name"]
            sl = (e.get("source_labels") or ["Entity"])[0]
            tl = (e.get("target_labels") or ["Entity"])[0]
            props = dict(e.get("properties") or {})
            try:
                s.run(
                    f"""
                    MATCH (a:{sl} {{name: $s}})
                    MATCH (b:{tl} {{name: $t}})
                    MERGE (a)-[r:{rtype}]->(b)
                    SET r += $props
                    """,
                    s=sn,
                    t=tn,
                    props=props,
                )
                er += 1
            except Exception as ex:
                print(f"edge fail {sn} -[{rtype}]-> {tn}: {ex}")

        # Also restore solid FLPs mentioned in ref_expand extractions if missing
        ext_dir = ROOT / "out" / "ref_expand" / "extractions"
        restored_ext = 0
        if ext_dir.exists():
            for path in sorted(ext_dir.glob("*.json")):
                data = json.loads(path.read_text(encoding="utf-8"))
                for node in data.get("nodes") or []:
                    if node.get("label") != "FLP":
                        continue
                    name = (node.get("properties") or {}).get("name") or node.get("name")
                    if not name or is_molecular_name(name):
                        continue
                    c = s.run(
                        "MATCH (f:FLP {name: $name}) RETURN count(f) AS c", name=name
                    ).single()["c"]
                    if c == 0:
                        props = dict(node.get("properties") or {})
                        props["name"] = name
                        s.run(
                            "MERGE (n:FLP {name: $name}) SET n += $props",
                            name=name,
                            props=props,
                        )
                        restored_ext += 1
                        print(f"  + ext {name}")
        print(f"restored from extractions: {restored_ext}")

        # Delete residual molecular FLPs
        deleted = s.run(
            """
            MATCH (f:FLP)
            WHERE toLower(f.name) CONTAINS 'b(c6f5)'
               OR toLower(f.name) CONTAINS 'tbu3p'
               OR toLower(f.name) CONTAINS 'phosphino-borane'
               OR toLower(f.name) CONTAINS 'phosphinoferrocene'
               OR toLower(f.name) CONTAINS 'phosphine-borane'
               OR toLower(f.name) CONTAINS 'zirconocene'
               OR toLower(f.name) CONTAINS 'lutidine/'
               OR toLower(f.name) CONTAINS 'carbene/b'
               OR toLower(f.name) CONTAINS 'alkenylene-linked'
               OR toLower(f.name) = 'macromolecular flp'
               OR toLower(f.name) = 'amine-borane flp'
               OR toLower(f.name) = 'rotation-activated flp'
               OR f.name IN $wrong
            WITH collect(f) AS fs
            FOREACH (f IN fs | DETACH DELETE f)
            RETURN size(fs) AS c
            """,
            wrong=list(WRONG_HOMO),
        ).single()["c"]
        print(f"deleted residual molecular FLP: {deleted}")

        nodes = s.run("MATCH (n) RETURN count(n) AS c").single()["c"]
        rels = s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"]
        flps = s.run("MATCH (:FLP) RETURN count(*) AS c").single()["c"]
        print(f"final: nodes={nodes} rels={rels} flp={flps}")

        print("\nRemaining FLP names:")
        for r in s.run("MATCH (f:FLP) RETURN f.name AS name ORDER BY name"):
            print(f"  {r['name']}")

    driver.close()


if __name__ == "__main__":
    main()
