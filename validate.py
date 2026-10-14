"""Schema validation for extracted nodes/edges."""
from __future__ import annotations

import re
import unicodedata
from copy import deepcopy
from typing import Any

from .config_utils import load_schema


def soft_name_key(name: str) -> str:
    """Normalize names for identity / MERGE (keeps charge signs)."""
    s = unicodedata.normalize("NFKC", name or "")
    for ch in ("\u2010", "\u2011", "\u2012", "\u2013", "\u2014", "\u2212"):
        s = s.replace(ch, "-")
    s = s.lower().strip()
    s = s.replace("/", "-")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"\s*-\s*", "-", s)
    s = s.replace(" ", "")
    s = re.sub(r"(?<=[a-z0-9\)\]])\-+(?=[a-z0-9\(\[])", "", s)
    if s.startswith("*") and not s.endswith("*"):
        s = s[1:] + "*"
    elif s.startswith("*") and s.endswith("*") and len(s) > 2:
        s = s.strip("*") + "*"
    return s


def normalize_name(name: str) -> str:
    return soft_name_key(name)


def apply_synonyms(name: str, synonyms: dict[str, list[str]] | None) -> str:
    n = soft_name_key(name)
    if not synonyms:
        return n
    for canonical, variants in synonyms.items():
        canon = soft_name_key(canonical)
        opts = {soft_name_key(v) for v in variants} | {canon}
        if n in opts:
            return canon
    return n


def display_name(normalized: str) -> str:
    # Keep chemical formulas roughly intact; title-case multi-word phrases
    if re.fullmatch(r"[a-z0-9\-_/]+", normalized) and any(c.isdigit() for c in normalized):
        # e.g. ceo2 -> CeO2 best-effort later; keep as stored canonical key in name_key
        return normalized
    if " " in normalized:
        return normalized.title()
    return normalized


class ValidationResult:
    def __init__(self) -> None:
        self.accepted_nodes: list[dict[str, Any]] = []
        self.accepted_edges: list[dict[str, Any]] = []
        self.rejected: list[dict[str, Any]] = []
        self.warnings: list[dict[str, Any]] = []


def _clean_node(node: dict[str, Any], labels: set[str], synonyms: dict[str, list[str]]) -> dict[str, Any] | str:
    if not isinstance(node, dict):
        return "node is not an object"
    label = node.get("label")
    name = node.get("name")
    if not label or label not in labels:
        return f"invalid label: {label!r}"
    if not name or not str(name).strip():
        return "missing name"
    name_key = apply_synonyms(str(name), synonyms)
    props = node.get("props") or {}
    if not isinstance(props, dict):
        props = {"_raw_props": props}
    return {
        "label": label,
        "name": str(name).strip(),
        "name_key": name_key,
        "props": props,
    }


def validate_extraction(extraction: dict[str, Any], schema: dict[str, Any] | None = None) -> ValidationResult:
    schema = schema or load_schema()
    labels = set(schema["node_labels"])
    rels = set(schema["relationship_types"])
    synonyms = schema.get("synonyms") or {}
    meta = extraction.get("meta") or {}
    result = ValidationResult()

    if meta.get("error"):
        result.rejected.append({"reason": f"extraction error: {meta['error']}", "meta": meta})
        return result

    node_index: dict[tuple[str, str], dict[str, Any]] = {}
    for node in extraction.get("nodes") or []:
        cleaned = _clean_node(node, labels, synonyms)
        if isinstance(cleaned, str):
            result.rejected.append({"kind": "node", "reason": cleaned, "raw": node, "meta": meta})
            continue
        cleaned["sources"] = [
            {
                "source_pdf": meta.get("source_pdf"),
                "page_start": meta.get("page_start"),
                "page_end": meta.get("page_end"),
                "chunk_id": meta.get("chunk_id"),
            }
        ]
        key = (cleaned["label"], cleaned["name_key"])
        node_index[key] = cleaned
        result.accepted_nodes.append(cleaned)

    for edge in extraction.get("edges") or []:
        if not isinstance(edge, dict):
            result.rejected.append({"kind": "edge", "reason": "edge is not an object", "raw": edge, "meta": meta})
            continue
        etype = edge.get("type")
        if etype not in rels:
            result.rejected.append({"kind": "edge", "reason": f"invalid type: {etype!r}", "raw": edge, "meta": meta})
            continue
        src = edge.get("source") or {}
        tgt = edge.get("target") or {}
        src_clean = _clean_node(src if "name" in src else {"label": src.get("label"), "name": src.get("name")}, labels, synonyms)
        tgt_clean = _clean_node(tgt if "name" in tgt else {"label": tgt.get("label"), "name": tgt.get("name")}, labels, synonyms)
        # Allow edges to introduce endpoints
        if isinstance(src_clean, str):
            # try treating source as full node-like
            src_clean = _clean_node(
                {"label": src.get("label"), "name": src.get("name"), "props": src.get("props") or {}},
                labels,
                synonyms,
            )
        if isinstance(tgt_clean, str):
            tgt_clean = _clean_node(
                {"label": tgt.get("label"), "name": tgt.get("name"), "props": tgt.get("props") or {}},
                labels,
                synonyms,
            )
        if isinstance(src_clean, str) or isinstance(tgt_clean, str):
            result.rejected.append(
                {
                    "kind": "edge",
                    "reason": f"bad endpoints: src={src_clean if isinstance(src_clean, str) else 'ok'}, "
                    f"tgt={tgt_clean if isinstance(tgt_clean, str) else 'ok'}",
                    "raw": edge,
                    "meta": meta,
                }
            )
            continue

        for endpoint in (src_clean, tgt_clean):
            key = (endpoint["label"], endpoint["name_key"])
            if key not in node_index:
                endpoint = deepcopy(endpoint)
                endpoint["sources"] = [
                    {
                        "source_pdf": meta.get("source_pdf"),
                        "page_start": meta.get("page_start"),
                        "page_end": meta.get("page_end"),
                        "chunk_id": meta.get("chunk_id"),
                    }
                ]
                node_index[key] = endpoint
                result.accepted_nodes.append(endpoint)

        props = edge.get("props") or {}
        if not isinstance(props, dict):
            props = {"_raw_props": props}
        result.accepted_edges.append(
            {
                "type": etype,
                "source": {"label": src_clean["label"], "name_key": src_clean["name_key"], "name": src_clean["name"]},
                "target": {"label": tgt_clean["label"], "name_key": tgt_clean["name_key"], "name": tgt_clean["name"]},
                "props": props,
                "sources": [
                    {
                        "source_pdf": meta.get("source_pdf"),
                        "page_start": meta.get("page_start"),
                        "page_end": meta.get("page_end"),
                        "chunk_id": meta.get("chunk_id"),
                    }
                ],
            }
        )
    return result


def validate_all(extractions: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    schema = load_schema()
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for ext in extractions:
        vr = validate_extraction(ext, schema)
        nodes.extend(vr.accepted_nodes)
        edges.extend(vr.accepted_edges)
        rejected.extend(vr.rejected)
    return nodes, edges, rejected
