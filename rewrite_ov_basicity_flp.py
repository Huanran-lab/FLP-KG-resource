"""Rewrite oxygen-vacancy -> FLP into OV -> Lewis basicity -> surface FLP."""
from __future__ import annotations

import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = [
    "00391520122e3074.json",
    "010faf1190221e86.json",
    "02aa5ab9fda97b01.json",
    "0493bc849a3a28b5.json",
    "055724a0b11bdf92.json",
    "058ae45bc3ed5b86.json",
    "061572adc60280ab.json",
]


def ensure_node(nodes: list, label: str, name: str, props: dict | None = None) -> None:
    for n in nodes:
        if n.get("label") == label and n.get("name") == name:
            return
    nodes.append({"label": label, "name": name, "props": props or {}})


def has_edge(edges: list, typ: str, s_label: str, s_name: str, t_label: str, t_name: str) -> bool:
    for e in edges:
        if e.get("type") != typ:
            continue
        s, t = e.get("source") or {}, e.get("target") or {}
        if (
            s.get("label") == s_label
            and s.get("name") == s_name
            and t.get("label") == t_label
            and t.get("name") == t_name
        ):
            return True
    return False


def add_edge(edges: list, typ: str, s_label: str, s_name: str, t_label: str, t_name: str) -> None:
    if not has_edge(edges, typ, s_label, s_name, t_label, t_name):
        edges.append(
            {
                "type": typ,
                "source": {"label": s_label, "name": s_name},
                "target": {"label": t_label, "name": t_name},
                "props": {},
            }
        )


def main() -> None:
    for fname in FILES:
        path = ROOT / "out" / "edited" / fname
        data = json.loads(path.read_text(encoding="utf-8"))
        nodes = data.setdefault("nodes", [])
        edges = data.setdefault("edges", [])

        # Collect FLP targets currently enabled directly by oxygen vacancy
        flp_targets: list[str] = []
        kept: list = []
        removed = 0
        for e in edges:
            s, t = e.get("source") or {}, e.get("target") or {}
            if (
                e.get("type") == "ENABLES"
                and s.get("label") == "Defect"
                and s.get("name") == "oxygen vacancy"
                and t.get("label") == "FLP"
            ):
                flp_targets.append(t.get("name") or "surface FLP")
                removed += 1
                continue
            kept.append(e)
        edges[:] = kept

        if not flp_targets and fname != "061572adc60280ab.json":
            # still ensure chain if OV exists and some FLP exists? only if we removed or file already partial
            pass

        # For 061572: also ensure if chain partially exists
        ensure_node(
            nodes,
            "Descriptor",
            "Lewis basicity",
            {"note": "basicity strengthened via oxygen vacancy"},
        )
        add_edge(edges, "ENABLES", "Defect", "oxygen vacancy", "Descriptor", "Lewis basicity")

        targets = flp_targets or (
            ["surface FLP"]
            if any(n.get("label") == "FLP" and n.get("name") == "surface FLP" for n in nodes)
            else []
        )
        # de-dupe preserve order
        seen = set()
        uniq = []
        for name in targets:
            if name not in seen:
                seen.add(name)
                uniq.append(name)

        for flp_name in uniq:
            add_edge(edges, "FACILITATES", "Descriptor", "Lewis basicity", "FLP", flp_name)

        path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(f"{fname}: removed OV->FLP={removed}; chain -> {uniq}")


if __name__ == "__main__":
    main()
