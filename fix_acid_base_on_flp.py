"""Move HAS_ACID_SITE / HAS_BASE_SITE from Material onto FLP (surface FLP)."""
from __future__ import annotations

import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    ROOT / "out/edited/005a12ee264bfe53.json",
    ROOT / "out/edited/0493bc849a3a28b5.json",
    ROOT / "out/edited/058ae45bc3ed5b86.json",
]


def fix_missing_edges_key(text: str) -> str:
    # nodes closed then edge objects without "edges":
    marker = '  ],\n    {\n      "type":'
    if marker in text and '"edges"' not in text.split(marker, 1)[0][-80:]:
        # only if edges key truly missing after nodes
        if '"edges"' not in text:
            text = text.replace(
                '  ],\n    {\n      "type":',
                '  ],\n  "edges": [\n    {\n      "type":',
                1,
            )
    return text


def main() -> None:
    for path in FILES:
        text = path.read_text(encoding="utf-8")
        text = fix_missing_edges_key(text)
        text = re.sub(r",(\s*[}\]])", r"\1", text)
        data = json.loads(text)
        flp_names = [n["name"] for n in data.get("nodes", []) if n.get("label") == "FLP"]
        flp_name = "surface FLP" if "surface FLP" in flp_names else (flp_names[0] if flp_names else "surface FLP")
        changed = 0
        for e in data.get("edges", []):
            if e.get("type") not in ("HAS_ACID_SITE", "HAS_BASE_SITE"):
                continue
            src = e.get("source") or {}
            if (src.get("label") or "").strip() == "Material":
                e["source"] = {"label": "FLP", "name": flp_name}
                changed += 1
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{path.name}: moved {changed} acid/base edge(s) -> FLP({flp_name})")


if __name__ == "__main__":
    main()
