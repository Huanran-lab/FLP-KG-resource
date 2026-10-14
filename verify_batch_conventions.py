from pathlib import Path
import json

skip = {
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

names = sorted(p.name for p in Path("out/edited").glob("*.json"))
i = names.index("061572adc60280ab.json")
sample = names[i + 1]
print("sample", sample)
d = json.loads(Path("out/edited", sample).read_text(encoding="utf-8"))
for e in d["edges"]:
    s, t = e["source"], e["target"]
    if e["type"] in (
        "ACTIVATES",
        "CATALYZES",
        "HAS_ACID_SITE",
        "HAS_BASE_SITE",
        "ENABLES",
        "FACILITATES",
        "LOCATED_ON",
        "HAS_ACTIVE_SITE",
    ):
        print(f"{s['label']}({s['name']}) -{e['type']}-> {t['label']}({t['name']})")

viol = {"flp_cat": 0, "ov_flp": 0, "mat_acid": 0, "has_active": 0}
for p in Path("out/edited").glob("*.json"):
    if p.name in skip:
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    for e in d.get("edges") or []:
        s, t = e.get("source") or {}, e.get("target") or {}
        if e.get("type") in ("ACTIVATES", "CATALYZES") and s.get("label") == "FLP":
            viol["flp_cat"] += 1
        if (
            e.get("type") == "ENABLES"
            and s.get("label") == "Defect"
            and s.get("name") == "oxygen vacancy"
            and t.get("label") == "FLP"
        ):
            viol["ov_flp"] += 1
        if e.get("type") in ("HAS_ACID_SITE", "HAS_BASE_SITE") and (s.get("label") or "").strip() == "Material":
            viol["mat_acid"] += 1
        if e.get("type") == "HAS_ACTIVE_SITE":
            viol["has_active"] += 1
print("remaining_violations", viol)
