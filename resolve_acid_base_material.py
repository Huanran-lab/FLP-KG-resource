"""Second pass: resolve remaining FLP -HAS_ACID/BASE-> by inferring Material.

Also add missing FLP -LOCATED_ON-> Material when acid/base are moved.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITED = ROOT / "out" / "edited"


def edge_key(e: dict) -> tuple:
    s, t = e["source"], e["target"]
    return (e.get("type"), s.get("label"), s.get("name"), t.get("label"), t.get("name"))


def element_tokens(name: str) -> set[str]:
    n = name.lower()
    elems = [
        "ce", "ti", "nb", "zn", "ga", "al", "in", "ni", "cu", "fe", "co", "mn",
        "mo", "w", "pt", "pd", "ru", "rh", "ir", "ag", "au", "mg", "ca", "ba",
        "sr", "la", "zr", "hf", "sn", "bi", "sb", "as", "se", "te", "ge", "si",
        "bo", "bn", "ox", "oh",
        "b", "n", "o", "p", "s", "c", "h", "f",
    ]
    found = set()
    for el in elems:
        if len(el) == 1:
            if re.search(rf"(?<![a-z]){el}(?![a-z])", n):
                found.add(el)
        elif el in n:
            found.add(el)
    m = re.match(r"^([A-Za-z]{1,2})(?:I{1,3}|$)", name.strip())
    if m:
        found.add(m.group(1).lower())
    return found


def score_mat(site_or_flp: str, mat: str) -> int:
    site_toks = element_tokens(site_or_flp)
    mat_toks = element_tokens(mat)
    overlap = site_toks & mat_toks
    nontrivial = {t for t in overlap if t not in {"c", "h"}}
    score = len(nontrivial) * 10 + len(overlap)
    sn = site_or_flp.lower().replace(" ", "")
    mn = mat.lower().replace(" ", "")
    if sn and sn in mn:
        score += 40
    for t in site_toks:
        if len(t) >= 2 and t in mn:
            score += 5
    return score


def choose_materials(
    flp_name: str,
    site_name: str,
    materials: list[str],
    preferred: list[str],
) -> list[str]:
    candidates = preferred or materials
    if not candidates:
        return []
    if len(candidates) == 1:
        return candidates

    # score by site then FLP name
    scored = []
    for m in candidates:
        scored.append((max(score_mat(site_name, m), score_mat(flp_name, m)), m))
    scored.sort(key=lambda x: (-x[0], x[1]))
    best = scored[0][0]
    if best > 0:
        top = [m for s, m in scored if s == best]
        return top

    # series like BN-NCN-550/650: if all share prefix, assign to all preferred/materials
    if preferred:
        return preferred
    return candidates


def process_file(path: Path) -> dict:
    d = json.loads(path.read_text(encoding="utf-8"))
    nodes = d.get("nodes", [])
    edges = d.get("edges", [])
    materials = [n["name"] for n in nodes if n.get("label") == "Material"]

    preferred: list[str] = []
    for e in edges:
        if e.get("type") in ("CATALYZES", "ACTIVATES") and e["source"].get("label") == "Material":
            name = e["source"]["name"]
            if name in materials and name not in preferred:
                preferred.append(name)
    # also materials that already are LOCATED_ON targets
    for e in edges:
        if e.get("type") == "LOCATED_ON" and e["source"].get("label") == "FLP" and e["target"].get("label") == "Material":
            name = e["target"]["name"]
            if name in materials and name not in preferred:
                preferred.append(name)

    new_edges: list[dict] = []
    seen = set()
    moved = 0
    still = 0
    added_loc = 0

    # existing located pairs
    located = set()
    for e in edges:
        if e.get("type") == "LOCATED_ON" and e["source"].get("label") == "FLP" and e["target"].get("label") == "Material":
            located.add((e["source"]["name"], e["target"]["name"]))

    pending_loc: set[tuple[str, str]] = set()

    for e in edges:
        typ = e.get("type")
        s = e.get("source", {})
        if typ not in ("HAS_ACID_SITE", "HAS_BASE_SITE") or s.get("label") != "FLP":
            # strip unresolved notes we added earlier if any leftover
            k = edge_key(e)
            if k not in seen:
                seen.add(k)
                new_edges.append(e)
            continue

        flp_name = s.get("name", "")
        site_name = e.get("target", {}).get("name", "")
        # materials already linked to this FLP
        linked = [m for (f, m) in located if f == flp_name]
        chosen = choose_materials(flp_name, site_name, materials, linked or preferred or materials)
        if not chosen:
            still += 1
            k = edge_key(e)
            if k not in seen:
                seen.add(k)
                new_edges.append(e)
            continue

        for mat_name in chosen:
            ne = {
                "type": typ,
                "source": {"label": "Material", "name": mat_name},
                "target": dict(e["target"]),
                "props": {k: v for k, v in (e.get("props") or {}).items() if "unresolved" not in str(v)},
            }
            k = edge_key(ne)
            if k not in seen:
                seen.add(k)
                new_edges.append(ne)
                moved += 1
            pending_loc.add((flp_name, mat_name))

    for flp_name, mat_name in sorted(pending_loc):
        if (flp_name, mat_name) in located:
            continue
        le = {
            "type": "LOCATED_ON",
            "source": {"label": "FLP", "name": flp_name},
            "target": {"label": "Material", "name": mat_name},
            "props": {},
        }
        k = edge_key(le)
        if k not in seen:
            seen.add(k)
            new_edges.append(le)
            added_loc += 1

    d["edges"] = new_edges
    path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"file": path.name, "moved": moved, "still": still, "added_loc": added_loc}


def main() -> None:
    files = sorted(p for p in EDITED.glob("*.json") if p.name != "example_edit_me.json")
    tot = {"moved": 0, "still": 0, "added_loc": 0, "touched": 0}
    still_files = []
    for p in files:
        try:
            before = json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError as e:
            print("SKIP", p.name, e)
            continue
        need = any(
            e.get("type") in ("HAS_ACID_SITE", "HAS_BASE_SITE") and e.get("source", {}).get("label") == "FLP"
            for e in before.get("edges", [])
        )
        if not need:
            continue
        r = process_file(p)
        tot["touched"] += 1
        tot["moved"] += r["moved"]
        tot["still"] += r["still"]
        tot["added_loc"] += r["added_loc"]
        if r["still"]:
            still_files.append((r["file"], r["still"]))
    print(tot)
    print("still_files", still_files)


if __name__ == "__main__":
    main()
