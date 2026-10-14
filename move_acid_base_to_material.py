"""Move HAS_ACID_SITE / HAS_BASE_SITE from FLP onto Material.

Target structure:
  FLP -LOCATED_ON-> Material
  Material -HAS_ACID_SITE-> LewisAcid
  Material -HAS_BASE_SITE-> LewisBase
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITED = ROOT / "out" / "edited"


def edge_key(e: dict) -> tuple:
    s, t = e["source"], e["target"]
    return (
        e.get("type"),
        s.get("label"),
        s.get("name"),
        t.get("label"),
        t.get("name"),
    )


def unique_mats(names: list[str]) -> list[str]:
    out: list[str] = []
    for n in names:
        if n not in out:
            out.append(n)
    return out


def flp_to_materials(edges: list[dict]) -> dict[tuple[str, str], list[str]]:
    loc: dict[tuple[str, str], list[str]] = {}
    for e in edges:
        typ = e.get("type")
        s, t = e.get("source", {}), e.get("target", {})
        if typ == "LOCATED_ON" and s.get("label") == "FLP" and t.get("label") == "Material":
            loc.setdefault((s["label"], s["name"]), []).append(t["name"])
        if typ == "HAS_ACTIVE_SITE" and s.get("label") == "Material" and t.get("label") == "FLP":
            loc.setdefault((t["label"], t["name"]), []).append(s["name"])
    return {k: unique_mats(v) for k, v in loc.items()}


def element_tokens(name: str) -> set[str]:
    """Rough chemical-element-ish tokens from a site/material name."""
    n = name.lower()
    # common multi-letter elements first
    elems = [
        "ce", "ti", "nb", "zn", "ga", "al", "in", "ni", "cu", "fe", "co", "mn",
        "mo", "w", "pt", "pd", "ru", "rh", "ir", "ag", "au", "mg", "ca", "ba",
        "sr", "la", "zr", "hf", "sn", "bi", "sb", "as", "se", "te", "ge", "si",
        "b", "n", "o", "p", "s", "c", "h", "f", "cl", "br", "i",
    ]
    found = set()
    for el in elems:
        # word-ish or formula-ish presence
        if re.search(rf"(?<![a-z]){re.escape(el)}(?![a-z])", n) or el in n:
            # avoid matching single letter too aggressively inside longer words
            if len(el) == 1:
                if re.search(rf"(?<![a-z]){el}(?![a-z])", n):
                    found.add(el)
            else:
                found.add(el)
    # explicit roman/site suffixes like GaI, NII
    m = re.match(r"^([A-Za-z]{1,2})(?:I{1,3}|$)", name.strip())
    if m:
        found.add(m.group(1).lower())
    return found


def match_materials(site_name: str, materials: list[str]) -> list[str]:
    """Pick materials whose names share element tokens with the acid/base site."""
    site_toks = element_tokens(site_name)
    if not site_toks or not materials:
        return list(materials)

    scored: list[tuple[int, str]] = []
    for mat in materials:
        mat_toks = element_tokens(mat)
        overlap = site_toks & mat_toks
        # filter trivial overlaps like only 'c' or 'h' when better exist
        nontrivial = {t for t in overlap if t not in {"c", "h"} or len(site_toks) == 1}
        score = len(nontrivial) * 10 + len(overlap)
        # direct substring boost
        sn = site_name.lower().replace(" ", "")
        mn = mat.lower().replace(" ", "")
        if sn and sn in mn:
            score += 50
        # metal site names often start with metal symbol present in material
        for t in site_toks:
            if len(t) >= 2 and t in mn:
                score += 5
        scored.append((score, mat))

    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0][0]
    if best <= 0:
        return list(materials)
    return [m for s, m in scored if s == best]


def process_file(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    edges = d.get("edges", [])
    loc_map = flp_to_materials(edges)

    new_edges: list[dict] = []
    seen: set[tuple] = set()
    moved = 0
    unresolved = 0
    multi_assigned = 0

    for e in edges:
        typ = e.get("type")
        s = e.get("source", {})
        if typ not in ("HAS_ACID_SITE", "HAS_BASE_SITE") or s.get("label") != "FLP":
            k = edge_key(e)
            if k not in seen:
                seen.add(k)
                new_edges.append(e)
            continue

        flp_key = (s.get("label"), s.get("name"))
        mats = loc_map.get(flp_key, [])
        target_name = e.get("target", {}).get("name", "")

        if not mats:
            # keep on FLP but flag
            unresolved += 1
            ne = dict(e)
            props = dict(ne.get("props") or {})
            props["note"] = (props.get("note", "") + "; unresolved Material for acid/base").strip("; ")
            ne["props"] = props
            k = edge_key(ne)
            if k not in seen:
                seen.add(k)
                new_edges.append(ne)
            continue

        chosen = mats
        if len(mats) > 1:
            chosen = match_materials(target_name, mats)
            if len(chosen) != 1:
                multi_assigned += 1

        for mat_name in chosen:
            ne = {
                "type": typ,
                "source": {"label": "Material", "name": mat_name},
                "target": dict(e["target"]),
                "props": dict(e.get("props") or {}),
            }
            k = edge_key(ne)
            if k not in seen:
                seen.add(k)
                new_edges.append(ne)
                moved += 1

    d["edges"] = new_edges
    meta = d.setdefault("meta", {})
    note = meta.get("note") or ""
    tag = "acid-base-on-material"
    if tag not in note:
        meta["note"] = f"{note}; {tag}".strip("; ") if note else tag

    path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {
        "file": path.name,
        "moved": moved,
        "unresolved": unresolved,
        "multi_assigned": multi_assigned,
    }


def main() -> None:
    files = sorted(p for p in EDITED.glob("*.json") if p.name != "example_edit_me.json")
    totals = {"moved": 0, "unresolved": 0, "multi_assigned": 0, "touched": 0}
    unresolved_files = []
    multi_files = []
    for p in files:
        try:
            json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print("SKIP_BAD_JSON", p.name, e)
            continue
        r = process_file(p)
        if r["moved"] or r["unresolved"] or r["multi_assigned"]:
            totals["touched"] += 1
        totals["moved"] += r["moved"]
        totals["unresolved"] += r["unresolved"]
        totals["multi_assigned"] += r["multi_assigned"]
        if r["unresolved"]:
            unresolved_files.append((r["file"], r["unresolved"]))
        if r["multi_assigned"]:
            multi_files.append((r["file"], r["multi_assigned"]))

    print("files", len(files))
    print("touched", totals["touched"])
    print("moved_edges", totals["moved"])
    print("unresolved_edges", totals["unresolved"])
    print("multi_material_edges", totals["multi_assigned"])
    print("--- unresolved files ---")
    for f, n in unresolved_files[:40]:
        print(f, n)
    print("--- multi-assign files ---")
    for f, n in multi_files[:40]:
        print(f, n)


if __name__ == "__main__":
    main()
