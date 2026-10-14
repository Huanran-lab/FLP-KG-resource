"""Ensure Material CONTAINS LewisAcid/Base also has HAS_ACID/BASE_SITE.
Fix missing InNH2 LOCATED_ON in amine FLP file.
"""
from __future__ import annotations

import json
from pathlib import Path

EDITED = Path(__file__).resolve().parents[1] / "out" / "edited"


def edge_key(e: dict) -> tuple:
    s, t = e["source"], e["target"]
    return (e.get("type"), s.get("label"), s.get("name"), t.get("label"), t.get("name"))


def sync_contains(path: Path) -> int:
    d = json.loads(path.read_text(encoding="utf-8"))
    edges = d.get("edges", [])
    seen = {edge_key(e) for e in edges}
    added = 0
    for e in list(edges):
        if e.get("type") != "CONTAINS":
            continue
        s, t = e["source"], e["target"]
        if s.get("label") != "Material":
            continue
        if t.get("label") == "LewisAcid":
            typ = "HAS_ACID_SITE"
        elif t.get("label") == "LewisBase":
            typ = "HAS_BASE_SITE"
        else:
            continue
        ne = {
            "type": typ,
            "source": {"label": "Material", "name": s["name"]},
            "target": {"label": t["label"], "name": t["name"]},
            "props": {},
        }
        k = edge_key(ne)
        if k not in seen:
            edges.append(ne)
            seen.add(k)
            added += 1
    if added:
        d["edges"] = edges
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return added


def fix_0557() -> None:
    path = EDITED / "055724a0b11bdf92.json"
    d = json.loads(path.read_text(encoding="utf-8"))
    edges = d["edges"]
    seen = {edge_key(e) for e in edges}
    ne = {
        "type": "LOCATED_ON",
        "source": {"label": "FLP", "name": "InNH2...In SFLP"},
        "target": {"label": "Material", "name": "In2O3-x(EDA)y"},
        "props": {},
    }
    if edge_key(ne) not in seen:
        edges.append(ne)
        d["edges"] = edges
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print("added InNH2 LOCATED_ON")


def main() -> None:
    total = 0
    for p in sorted(EDITED.glob("*.json")):
        if p.name == "example_edit_me.json":
            continue
        try:
            n = sync_contains(p)
        except json.JSONDecodeError:
            continue
        if n:
            print(p.name, "+", n)
            total += n
    print("total_added", total)
    fix_0557()


if __name__ == "__main__":
    main()
