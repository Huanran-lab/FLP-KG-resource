"""Merge and deduplicate validated entities / relations."""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .config_utils import load_schema
from .validate import normalize_name


def _merge_props(
    existing: dict[str, Any],
    incoming: dict[str, Any],
    conflicts: list[dict[str, Any]],
    context: dict[str, Any],
    prefer_incoming: bool = False,
) -> dict[str, Any]:
    out = dict(existing)
    for k, v in (incoming or {}).items():
        if k not in out or out[k] in (None, "", [], {}):
            out[k] = v
            continue
        if out[k] == v:
            continue
        if prefer_incoming:
            # Keep previous as alt; new literature wins
            alt_key = f"{k}__alts"
            alts = out.get(alt_key)
            if not isinstance(alts, list):
                alts = []
            if out[k] not in alts:
                alts.append(out[k])
            out[alt_key] = alts
            conflicts.append(
                {
                    "kind": "property_replaced",
                    "key": k,
                    "kept": v,
                    "replaced": out[k],
                    **context,
                }
            )
            out[k] = v
            continue
        # Keep first value; record conflict
        alt_key = f"{k}__alts"
        alts = out.get(alt_key)
        if not isinstance(alts, list):
            alts = []
        if v not in alts and v != out[k]:
            alts.append(v)
        out[alt_key] = alts
        conflicts.append(
            {
                "kind": "property_conflict",
                "key": k,
                "kept": out[k],
                "incoming": v,
                **context,
            }
        )
    return out


def merge_graph(
    nodes: list[dict[str, Any]],
    edges: list[dict[str, Any]],
    prefer_incoming: bool = False,
) -> dict[str, Any]:
    schema = load_schema()
    conflicts: list[dict[str, Any]] = []
    near_dup_candidates: list[dict[str, Any]] = []

    merged_nodes: dict[tuple[str, str], dict[str, Any]] = {}
    # Track surface forms for near-duplicate reporting
    forms: dict[tuple[str, str], set[str]] = defaultdict(set)

    for node in nodes:
        key = (node["label"], node["name_key"])
        forms[key].add(node.get("name", node["name_key"]))
        if key not in merged_nodes:
            merged_nodes[key] = {
                "label": node["label"],
                "name": node.get("name") or node["name_key"],
                "name_key": node["name_key"],
                "props": dict(node.get("props") or {}),
                "sources": list(node.get("sources") or []),
                "needs_review": False,
            }
        else:
            cur = merged_nodes[key]
            if prefer_incoming and node.get("name"):
                cur["name"] = node["name"]
            cur["props"] = _merge_props(
                cur["props"],
                node.get("props") or {},
                conflicts,
                {
                    "label": node["label"],
                    "name_key": node["name_key"],
                    "sources": node.get("sources"),
                },
                prefer_incoming=prefer_incoming,
            )
            cur["sources"].extend(node.get("sources") or [])

    # Prefer a nicer display name among surface forms (shortest),
    # unless prefer_incoming already set the latest literature name.
    for key, node in merged_nodes.items():
        variants = sorted(forms[key], key=lambda s: (len(s), s.lower()))
        if variants:
            if not prefer_incoming:
                node["name"] = variants[0]
            node["props"]["aliases"] = sorted({normalize_name(v) for v in variants})

    # Near-duplicate heuristic: same label, one name_key contains another (len>=4)
    keys_by_label: dict[str, list[str]] = defaultdict(list)
    for label, name_key in merged_nodes:
        keys_by_label[label].append(name_key)
    for label, keys in keys_by_label.items():
        uniq = sorted(set(keys))
        for i, a in enumerate(uniq):
            for b in uniq[i + 1 :]:
                if len(a) >= 4 and len(b) >= 4 and (a in b or b in a) and a != b:
                    near_dup_candidates.append(
                        {
                            "kind": "near_duplicate",
                            "label": label,
                            "name_key_a": a,
                            "name_key_b": b,
                            "name_a": merged_nodes[(label, a)]["name"],
                            "name_b": merged_nodes[(label, b)]["name"],
                        }
                    )

    edge_map: dict[tuple[str, str, str, str, str], dict[str, Any]] = {}
    for edge in edges:
        s = edge["source"]
        t = edge["target"]
        ekey = (edge["type"], s["label"], s["name_key"], t["label"], t["name_key"])
        if ekey not in edge_map:
            edge_map[ekey] = {
                "type": edge["type"],
                "source": {"label": s["label"], "name_key": s["name_key"]},
                "target": {"label": t["label"], "name_key": t["name_key"]},
                "props": dict(edge.get("props") or {}),
                "sources": list(edge.get("sources") or []),
            }
        else:
            cur = edge_map[ekey]
            cur["props"] = _merge_props(
                cur["props"],
                edge.get("props") or {},
                conflicts,
                {"edge": ekey, "sources": edge.get("sources")},
                prefer_incoming=prefer_incoming,
            )
            cur["sources"].extend(edge.get("sources") or [])

    # Incomplete FLP pattern checks
    incomplete: list[dict[str, Any]] = []
    flp_keys = [k for k in merged_nodes if k[0] == "FLP"]
    acid_targets = {
        (e["source"]["name_key"])
        for e in edge_map.values()
        if e["type"] == "HAS_ACID_SITE" and e["source"]["label"] == "FLP"
    }
    base_targets = {
        (e["source"]["name_key"])
        for e in edge_map.values()
        if e["type"] == "HAS_BASE_SITE" and e["source"]["label"] == "FLP"
    }
    for _, name_key in flp_keys:
        missing = []
        if name_key not in acid_targets:
            missing.append("HAS_ACID_SITE")
        if name_key not in base_targets:
            missing.append("HAS_BASE_SITE")
        if missing:
            merged_nodes[("FLP", name_key)]["needs_review"] = True
            incomplete.append(
                {
                    "kind": "incomplete_pattern",
                    "label": "FLP",
                    "name_key": name_key,
                    "name": merged_nodes[("FLP", name_key)]["name"],
                    "missing_relations": missing,
                    "sources": merged_nodes[("FLP", name_key)]["sources"][:5],
                }
            )

    # Deduplicate sources lists
    for node in merged_nodes.values():
        seen = set()
        uniq_sources = []
        for s in node["sources"]:
            sig = (s.get("source_pdf"), s.get("page_start"), s.get("page_end"), s.get("chunk_id"))
            if sig in seen:
                continue
            seen.add(sig)
            uniq_sources.append(s)
        node["sources"] = uniq_sources

    for edge in edge_map.values():
        seen = set()
        uniq_sources = []
        for s in edge["sources"]:
            sig = (s.get("source_pdf"), s.get("page_start"), s.get("page_end"), s.get("chunk_id"))
            if sig in seen:
                continue
            seen.add(sig)
            uniq_sources.append(s)
        edge["sources"] = uniq_sources

    return {
        "nodes": list(merged_nodes.values()),
        "edges": list(edge_map.values()),
        "conflicts": conflicts,
        "near_duplicates": near_dup_candidates,
        "incomplete_patterns": incomplete,
        "schema_name": schema.get("name"),
    }
