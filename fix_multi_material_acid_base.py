"""Fix multi-material acid/base over-assignment (e.g. Wurtzite GaI/NII pairs)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITED = ROOT / "out" / "edited"

# explicit III-V / II-VI anion pairing for common wurtzite cases
ANION_TO_MATERIALS = {
    "n": {"gan", "aln", "inn", "lan"},
    "o": {"zno", "cdo", "bao", "beo", "mgo"},
    "p": {"alp", "gap", "inp"},
    "s": {"cds", "hgs", "zns", "mgs"},
}


def edge_key(e: dict) -> tuple:
    s, t = e["source"], e["target"]
    return (e.get("type"), s.get("label"), s.get("name"), t.get("label"), t.get("name"))


def leading_element(name: str) -> str | None:
    """GaI / NII / OII / PII / ZnI -> ga/n/o/p/zn"""
    m = re.match(r"^([A-Z][a-z]?)I{1,3}$", name.strip())
    if m:
        return m.group(1).lower()
    m = re.match(r"^([A-Z][a-z]?)\b", name.strip())
    if m and len(name.strip()) <= 6:
        return m.group(1).lower()
    return None


def material_elements(mat: str) -> set[str]:
    # split Camel / formula-ish: GaN ZnO AlP Ni/CeO2
    parts = re.findall(r"[A-Z][a-z]?", mat)
    return {p.lower() for p in parts}


def best_materials_for_site(site_name: str, materials: list[str]) -> list[str]:
    el = leading_element(site_name)
    if not el:
        return materials

    # anion special-case tables
    key = site_name.lower().replace(" ", "")
    if el in ANION_TO_MATERIALS:
        hits = [m for m in materials if m.lower().replace(" ", "") in ANION_TO_MATERIALS[el]]
        if hits:
            return hits
        # also: material whose formula ends with this anion element
        hits = [m for m in materials if material_elements(m) == {el} or (
            el in material_elements(m) and len(material_elements(m)) >= 2 and list(material_elements(m))[-1:] == [el]
        )]
        # materials containing anion as last element token
        hits = []
        for m in materials:
            elems = re.findall(r"[A-Z][a-z]?", m)
            if elems and elems[-1].lower() == el:
                hits.append(m)
        if hits:
            return hits

    # cation: material that contains this element
    hits = [m for m in materials if el in material_elements(m) or el in m.lower()]
    if hits:
        return hits
    return materials


def flp_materials(edges: list[dict], flp_name: str) -> list[str]:
    mats = []
    for e in edges:
        if e.get("type") == "LOCATED_ON" and e["source"].get("label") == "FLP" and e["source"].get("name") == flp_name:
            if e["target"].get("label") == "Material":
                n = e["target"]["name"]
                if n not in mats:
                    mats.append(n)
    return mats


def process(path: Path) -> int:
    d = json.loads(path.read_text(encoding="utf-8"))
    edges = d.get("edges", [])

    # collect acid/base currently on materials; we need to know which FLPs exist via LOCATED_ON
    # Rebuild acid/base edges that look over-assigned: same site on >1 material while a tighter match exists
    site_owners: dict[tuple[str, str], list[str]] = {}
    # (type, site_name) -> [material names]
    other_edges = []
    ab_edges = []
    for e in edges:
        if e.get("type") in ("HAS_ACID_SITE", "HAS_BASE_SITE") and e["source"].get("label") == "Material":
            ab_edges.append(e)
            key = (e["type"], e["target"]["name"])
            site_owners.setdefault(key, []).append(e["source"]["name"])
        else:
            other_edges.append(e)

    changed = 0
    new_ab = []
    seen = set()
    for e in ab_edges:
        typ = e["type"]
        site = e["target"]["name"]
        owners = site_owners.get((typ, site), [])
        uniq_owners = list(dict.fromkeys(owners))
        if len(uniq_owners) <= 1:
            k = edge_key(e)
            if k not in seen:
                seen.add(k)
                new_ab.append(e)
            continue

        # over-assigned: re-pick
        chosen = best_materials_for_site(site, uniq_owners)
        if set(chosen) == set(uniq_owners):
            # couldn't refine
            k = edge_key(e)
            if k not in seen:
                seen.add(k)
                new_ab.append(e)
            continue

        # drop this edge if current material not in chosen; add chosen once
        # handled globally below
        continue

    # rebuild refined over-assigned sites
    refined_sites = {}
    for (typ, site), owners in site_owners.items():
        uniq = list(dict.fromkeys(owners))
        if len(uniq) <= 1:
            continue
        chosen = best_materials_for_site(site, uniq)
        if set(chosen) != set(uniq):
            refined_sites[(typ, site)] = chosen

    new_ab = []
    seen = set()
    for e in ab_edges:
        typ = e["type"]
        site = e["target"]["name"]
        key = (typ, site)
        if key in refined_sites:
            continue  # rebuild below
        k = edge_key(e)
        if k not in seen:
            seen.add(k)
            new_ab.append(e)

    for (typ, site), mats in refined_sites.items():
        # find a template target node from old edges
        target = None
        props = {}
        for e in ab_edges:
            if e["type"] == typ and e["target"]["name"] == site:
                target = e["target"]
                props = e.get("props") or {}
                break
        for mat in mats:
            ne = {
                "type": typ,
                "source": {"label": "Material", "name": mat},
                "target": dict(target),
                "props": dict(props),
            }
            k = edge_key(ne)
            if k not in seen:
                seen.add(k)
                new_ab.append(ne)
                changed += 1

    if refined_sites:
        d["edges"] = other_edges + new_ab
        path.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return len(refined_sites)


def main() -> None:
    nfiles = 0
    nsites = 0
    for p in sorted(EDITED.glob("*.json")):
        if p.name == "example_edit_me.json":
            continue
        try:
            c = process(p)
        except json.JSONDecodeError:
            continue
        if c:
            nfiles += 1
            nsites += c
            print("fixed", p.name, "sites", c)
    print("files_fixed", nfiles, "sites_refined", nsites)


if __name__ == "__main__":
    main()
