"""Apply agreed KG conventions to remaining out/edited/*.json files.

Rules (from user review):
1. HAS_ACID_SITE / HAS_BASE_SITE source = FLP (prefer surface FLP)
2. ACTIVATES / CATALYZES source = Material (prefer LOCATED_ON target of that FLP)
3. Material -HAS_ACTIVE_SITE-> FLP  =>  FLP -LOCATED_ON-> Material
4. Defect(oxygen vacancy) -ENABLES-> FLP  =>
     OV -ENABLES-> Lewis basicity -FACILITATES-> FLP
"""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
EDITED_DIR = ROOT / "out" / "edited"

# Already manually reviewed — skip
SKIP = {
    "00391520122e3074.json",
    "005a12ee264bfe53.json",
    "010faf1190221e86.json",
    "01853cdd5381eff7.json",
    "02aa5ab9fda97b01.json",
    "046475deb91a409e.json",
    "0493bc849a3a28b5.json",
    "055724a0b11bdf92.json",
    "058ae45bc3ed5b86.json",
    "061572adc60280ab.json",
    "example_edit_me.json",
}


def ensure_node(nodes: list, label: str, name: str, props: dict | None = None) -> None:
    for n in nodes:
        if n.get("label") == label and n.get("name") == name:
            return
    nodes.append({"label": label, "name": name, "props": props or {}})


def edge_key(e: dict) -> tuple:
    s, t = e.get("source") or {}, e.get("target") or {}
    return (
        e.get("type"),
        s.get("label"),
        s.get("name"),
        t.get("label"),
        t.get("name"),
    )


def prefer_flp_name(nodes: list, fallback: str | None = None) -> str | None:
    names = [n.get("name") for n in nodes if n.get("label") == "FLP"]
    if "surface FLP" in names:
        return "surface FLP"
    if fallback and fallback in names:
        return fallback
    return names[0] if names else None


def materials(nodes: list) -> list[str]:
    return [n.get("name") for n in nodes if n.get("label") == "Material"]


def located_materials(edges: list, flp_name: str) -> list[str]:
    out = []
    for e in edges:
        if e.get("type") != "LOCATED_ON":
            continue
        s, t = e.get("source") or {}, e.get("target") or {}
        if s.get("label") == "FLP" and s.get("name") == flp_name and t.get("label") == "Material":
            out.append(t.get("name"))
    return out


def process(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    nodes = data.setdefault("nodes", [])
    edges = data.setdefault("edges", [])
    stats = {
        "acid_base_to_flp": 0,
        "act_cat_to_material": 0,
        "has_active_to_located": 0,
        "ov_chain": 0,
    }

    flp_default = prefer_flp_name(nodes)
    mats = materials(nodes)

    # 1) Convert HAS_ACTIVE_SITE Material->FLP into LOCATED_ON FLP->Material
    new_edges: list = []
    seen = set()
    for e in edges:
        typ = e.get("type")
        s, t = e.get("source") or {}, e.get("target") or {}
        sl = (s.get("label") or "").strip()
        tl = (t.get("label") or "").strip()
        if typ == "HAS_ACTIVE_SITE" and sl == "Material" and tl == "FLP":
            ne = {
                "type": "LOCATED_ON",
                "source": {"label": "FLP", "name": t.get("name")},
                "target": {"label": "Material", "name": s.get("name")},
                "props": e.get("props") or {},
            }
            k = edge_key(ne)
            if k not in seen:
                new_edges.append(ne)
                seen.add(k)
            stats["has_active_to_located"] += 1
            continue
        # keep for now; further transforms below
        new_edges.append(e)
        seen.add(edge_key(e))
    edges = new_edges

    # 2) Move acid/base from Material onto FLP
    for e in edges:
        typ = e.get("type")
        if typ not in ("HAS_ACID_SITE", "HAS_BASE_SITE"):
            continue
        s = e.get("source") or {}
        if (s.get("label") or "").strip() != "Material":
            continue
        flp_name = flp_default or prefer_flp_name(nodes)
        if not flp_name:
            continue
        e["source"] = {"label": "FLP", "name": flp_name}
        stats["acid_base_to_flp"] += 1

    # 3) Move ACTIVATES/CATALYZES from FLP onto Material
    for e in edges:
        typ = e.get("type")
        if typ not in ("ACTIVATES", "CATALYZES"):
            continue
        s = e.get("source") or {}
        if (s.get("label") or "").strip() != "FLP":
            continue
        flp_name = s.get("name")
        cand = located_materials(edges, flp_name) if flp_name else []
        if not cand:
            cand = mats
        if not cand:
            continue
        e["source"] = {"label": "Material", "name": cand[0]}
        stats["act_cat_to_material"] += 1

    # 4) OV -> FLP ENABLES  =>  OV -> Lewis basicity -> FLP
    kept = []
    flp_targets: list[str] = []
    for e in edges:
        s, t = e.get("source") or {}, e.get("target") or {}
        if (
            e.get("type") == "ENABLES"
            and s.get("label") == "Defect"
            and s.get("name") == "oxygen vacancy"
            and t.get("label") == "FLP"
        ):
            flp_targets.append(t.get("name") or "surface FLP")
            stats["ov_chain"] += 1
            continue
        kept.append(e)
    edges = kept

    if flp_targets:
        ensure_node(
            nodes,
            "Descriptor",
            "Lewis basicity",
            {"note": "basicity strengthened via oxygen vacancy"},
        )
        # OV -> Lewis basicity
        ov_b = {
            "type": "ENABLES",
            "source": {"label": "Defect", "name": "oxygen vacancy"},
            "target": {"label": "Descriptor", "name": "Lewis basicity"},
            "props": {},
        }
        if edge_key(ov_b) not in {edge_key(x) for x in edges}:
            edges.append(ov_b)
        # Lewis basicity -> each FLP
        uniq = []
        seen_n = set()
        for n in flp_targets:
            if n not in seen_n:
                seen_n.add(n)
                uniq.append(n)
        for flp_name in uniq:
            be = {
                "type": "FACILITATES",
                "source": {"label": "Descriptor", "name": "Lewis basicity"},
                "target": {"label": "FLP", "name": flp_name},
                "props": {},
            }
            if edge_key(be) not in {edge_key(x) for x in edges}:
                edges.append(be)

    # de-dupe edges
    dedup = []
    seen_e = set()
    for e in edges:
        k = edge_key(e)
        if k in seen_e:
            continue
        seen_e.add(k)
        dedup.append(e)

    data["nodes"] = nodes
    data["edges"] = dedup
    note = (data.get("meta") or {}).get("note", "")
    meta = data.setdefault("meta", {})
    tag = "batch-convention-pass"
    if tag not in str(note):
        meta["note"] = (str(note) + "; " if note else "") + tag

    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return stats


def main() -> None:
    files = sorted(p for p in EDITED_DIR.glob("*.json") if p.name not in SKIP)
    total = {k: 0 for k in ["acid_base_to_flp", "act_cat_to_material", "has_active_to_located", "ov_chain"]}
    bad = []
    touched = 0
    for p in files:
        try:
            st = process(p)
        except Exception as exc:  # noqa: BLE001
            bad.append((p.name, str(exc)))
            continue
        if any(st.values()):
            touched += 1
        for k, v in st.items():
            total[k] += v
    print(json.dumps({"files": len(files), "touched": touched, "changes": total, "bad": bad[:20], "bad_count": len(bad)}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
