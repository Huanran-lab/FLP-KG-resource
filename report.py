"""Conflict / ambiguity review report generation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .config_utils import ensure_out_dir, load_config


def _src_str(sources: list[dict[str, Any]] | None, limit: int = 3) -> str:
    if not sources:
        return "-"
    parts = []
    for s in sources[:limit]:
        pdf = s.get("source_pdf") or "?"
        ps = s.get("page_start")
        pe = s.get("page_end")
        pages = f"p.{ps}" if ps == pe or pe is None else f"p.{ps}-{pe}"
        parts.append(f"{pdf} ({pages})")
    return "; ".join(parts)


def build_report(
    rejected: list[dict[str, Any]],
    graph: dict[str, Any],
    write_stats: dict[str, int] | None = None,
) -> dict[str, Any]:
    report = {
        "summary": {
            "nodes_merged": len(graph.get("nodes", [])),
            "edges_merged": len(graph.get("edges", [])),
            "rejected_items": len(rejected),
            "property_conflicts": len(graph.get("conflicts", [])),
            "near_duplicates": len(graph.get("near_duplicates", [])),
            "incomplete_patterns": len(graph.get("incomplete_patterns", [])),
            "neo4j_stats": write_stats or {},
        },
        "rejected": rejected,
        "property_conflicts": graph.get("conflicts", []),
        "near_duplicates": graph.get("near_duplicates", []),
        "incomplete_patterns": graph.get("incomplete_patterns", []),
    }
    return report


def write_report(report: dict[str, Any], out_dir: Path | None = None) -> tuple[Path, Path]:
    cfg = load_config()
    out_dir = out_dir or ensure_out_dir(cfg)
    json_path = out_dir / "review_report.json"
    md_path = out_dir / "review_report.md"
    json_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    s = report["summary"]
    lines = [
        "# FLP Knowledge Graph Review Report",
        "",
        "## Summary",
        "",
        f"- Merged nodes: **{s['nodes_merged']}**",
        f"- Merged edges: **{s['edges_merged']}**",
        f"- Rejected extraction items: **{s['rejected_items']}**",
        f"- Property conflicts: **{s['property_conflicts']}**",
        f"- Near-duplicate candidates: **{s['near_duplicates']}**",
        f"- Incomplete patterns: **{s['incomplete_patterns']}**",
    ]
    if s.get("neo4j_stats"):
        lines.append(
            f"- Neo4j after write: **{s['neo4j_stats'].get('nodes', '?')}** nodes, "
            f"**{s['neo4j_stats'].get('relationships', '?')}** relationships"
        )
    lines += ["", "## Rejected items (schema / parse)", ""]
    if not report["rejected"]:
        lines.append("_None_")
    else:
        for i, item in enumerate(report["rejected"][:200], 1):
            meta = item.get("meta") or {}
            lines.append(
                f"{i}. `{item.get('kind', '?')}` — {item.get('reason')} "
                f"— source: {meta.get('source_pdf', '?')} "
                f"p.{meta.get('page_start')}-{meta.get('page_end')}"
            )
        if len(report["rejected"]) > 200:
            lines.append(f"... and {len(report['rejected']) - 200} more (see JSON).")

    lines += ["", "## Property conflicts", ""]
    if not report["property_conflicts"]:
        lines.append("_None_")
    else:
        for i, c in enumerate(report["property_conflicts"][:100], 1):
            lines.append(
                f"{i}. `{c.get('label', c.get('edge', '?'))}` "
                f"`{c.get('name_key', '')}` field `{c.get('key')}`: "
                f"kept={c.get('kept')!r}, incoming={c.get('incoming')!r} "
                f"— {_src_str(c.get('sources'))}"
            )

    lines += ["", "## Near-duplicate candidates", ""]
    if not report["near_duplicates"]:
        lines.append("_None_")
    else:
        for i, d in enumerate(report["near_duplicates"][:100], 1):
            lines.append(
                f"{i}. `{d['label']}`: `{d['name_a']}` (`{d['name_key_a']}`) vs "
                f"`{d['name_b']}` (`{d['name_key_b']}`)"
            )

    lines += ["", "## Incomplete FLP patterns", ""]
    if not report["incomplete_patterns"]:
        lines.append("_None_")
    else:
        for i, p in enumerate(report["incomplete_patterns"][:100], 1):
            lines.append(
                f"{i}. FLP `{p['name']}` missing {', '.join(p['missing_relations'])} "
                f"— {_src_str(p.get('sources'))}"
            )

    lines += [
        "",
        "## Suggested Neo4j checks",
        "",
        "```cypher",
        "MATCH (m:Material)-[:HAS_DEFECT]->(d:Defect)-[:ENABLES]->(f:FLP)-[:HAS_ACID_SITE]->(a)",
        "RETURN m.name, d.name, f.name, a.name LIMIT 25;",
        "```",
        "",
        "```cypher",
        "MATCH (n:FLP) WHERE n.needs_review = true RETURN n.name, n.name_key LIMIT 50;",
        "```",
        "",
    ]
    md_path.write_text("\n".join(lines), encoding="utf-8")
    return md_path, json_path
