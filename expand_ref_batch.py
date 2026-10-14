"""Expand Neo4j FLP graph from Ref/ PDFs in batches of 10.

Rules:
- Only read Abstract / Results&Discussion / Conclusion
- Solid / heterogeneous FLP only (skip others)
- Do not clear existing graph; overwrite duplicate node/edge props with new literature
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_utils import ensure_out_dir, load_config, load_schema, project_root  # noqa: E402
from src.llm_extract import FLPExtractor  # noqa: E402
from src.merge import merge_graph  # noqa: E402
from src.neo4j_writer import Neo4jWriter  # noqa: E402
from src.section_extract import looks_like_solid_flp_candidate, sections_to_chunks  # noqa: E402
from src.validate import validate_all  # noqa: E402

STATE_NAME = "ref_expand_state.json"


SOLID_SYSTEM_EXTRA = """
ADDITIONAL STRICT RULES FOR THIS BATCH:
12. Extract ONLY solid / heterogeneous / surface FLP knowledge (oxides, zeolites, MOFs,
    supported catalysts, defect-enabled surface FLPs, etc.).
13. If the text is ONLY about homogeneous molecular FLP (e.g. tBu3P/B(C6F5)3 in solution)
    with no solid/surface/heterogeneous context, return {"nodes":[], "edges":[]}.
14. Ignore experimental methods/instrument details outside the provided sections.
15. Prefer entity names already common in solid FLP literature (CeO2, oxygen vacancy,
    surface FLP, Ce3+, lattice O, etc.) when the text supports them.
16. ELECTRONIC STRUCTURE PRIORITY — when the text mentions band descriptors, ALWAYS extract them:
    - Prefer label ElectronicDescriptor (else Descriptor) for:
      "d-band", "d-band center", "d-band center (εd)", "εd", "Ed",
      "p-band", "p-band center", "p-band center (εp)", "εp", "Ep",
      "d–p orbital hybridization", "d-p interaction", "orbital hybridization",
      "band gap", "Fermi level", "density of states", "DOS", "PDOS".
    - Link them with schema edges when supported by text, e.g.:
      ActiveSite|FLP|Material|LewisAcid|LewisBase -HAS_DESCRIPTOR|DESCRIBED_BY-> ElectronicDescriptor
      ElectronicDescriptor -CORRELATES_WITH-> Activity|Reaction
      ElectronicDescriptor -FACILITATES|INVOLVES-> FLP|Mechanism (only if text supports)
    - Keep canonical short names: "d-band center", "p-band center", "d-p hybridization".
    - Capture qualitative shifts (upshift/downshift closer to Fermi level) in node props
      such as props.shift or props.note when stated.
17. Do NOT extract porosity / pore size / pore size distribution nodes (out of scope).
18. Prefer canonical names already in the graph when supported (CeO2, In2O3, Al2O3,
    surface FLP, oxygen vacancy, d-band center, p-band center, d-p hybridization).
"""

# Extra ranking keywords for this expand pipeline (merged with config keywords)
BAND_KEYWORDS = [
    "d-band",
    "d band",
    "p-band",
    "p band",
    "d-band center",
    "p-band center",
    "εd",
    "εp",
    "orbital hybridization",
    "d-p",
    "d–p",
    "density of states",
    "pdos",
    "fermi",
]


def list_ref_pdfs() -> list[Path]:
    ref = project_root() / "Ref"
    return sorted(ref.glob("*.pdf"), key=lambda p: p.name.lower())


def load_state(out: Path) -> dict[str, Any]:
    path = out / STATE_NAME
    if path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    return {"completed_pdfs": [], "batches": []}


def save_state(out: Path, state: dict[str, Any]) -> None:
    (out / STATE_NAME).write_text(
        json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def run_batch(batch_size: int = 10, batch_index: int | None = None) -> dict[str, Any]:
    cfg = load_config()
    out = ensure_out_dir(cfg)
    batch_dir = out / "ref_expand"
    batch_dir.mkdir(parents=True, exist_ok=True)

    state = load_state(out)
    done = set(state.get("completed_pdfs") or [])
    pdfs = list_ref_pdfs()
    pending = [p for p in pdfs if p.name not in done]
    if not pending:
        print("No pending PDFs in Ref/")
        return {"status": "done"}

    # batch_index 0-based among pending, default next
    bi = 0 if batch_index is None else batch_index
    start = bi * batch_size
    batch = pending[start : start + batch_size]
    if not batch:
        print(f"No PDFs for batch_index={bi}")
        return {"status": "empty_batch"}

    print(f"Batch starting at pending index {start}, size {len(batch)}")
    for i, p in enumerate(batch, 1):
        print(f"  {i}. {p.name}")

    keywords = list(cfg.get("priority_keywords") or [])
    for kw in BAND_KEYWORDS:
        if kw not in keywords:
            keywords.append(kw)
    chunk_cfg = cfg["chunking"]
    max_chunks = int(cfg["llm"].get("max_chunks_per_pdf") or 20)
    # Prefer fewer chunks for section-limited text
    max_chunks = min(max_chunks, 20)

    extractor = FLPExtractor()
    # Strengthen prompt for solid-only + electronic band descriptors
    extractor.system = extractor.system + SOLID_SYSTEM_EXTRA

    all_extractions: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []

    try:
        for pdf in tqdm(batch, desc="Ref batch"):
            row: dict[str, Any] = {"pdf": pdf.name, "status": "", "chunks": 0, "nodes": 0, "edges": 0}
            try:
                chunks = sections_to_chunks(
                    pdf,
                    target_chars=int(chunk_cfg["target_chars"]),
                    overlap_chars=int(chunk_cfg["overlap_chars"]),
                    min_chars=int(chunk_cfg["min_chars"]),
                    keywords=keywords,
                )
                # Keep highest-score chunks
                chunks = sorted(chunks, key=lambda c: (-c.score, c.page_start))[:max_chunks]
                row["chunks"] = len(chunks)
                row["sections_ok"] = bool(chunks)

                if not chunks:
                    row["status"] = "skip_no_target_sections"
                    report_rows.append(row)
                    continue
                if not looks_like_solid_flp_candidate(chunks, pdf.name):
                    row["status"] = "skip_not_solid_flp"
                    report_rows.append(row)
                    continue

                pdf_nodes = 0
                pdf_edges = 0
                for ch in chunks:
                    data = extractor.extract_chunk(ch)
                    # Empty after solid filter
                    if not data.get("nodes") and not data.get("edges"):
                        continue
                    all_extractions.append(data)
                    pdf_nodes += len(data.get("nodes") or [])
                    pdf_edges += len(data.get("edges") or [])
                    (batch_dir / "extractions" / f"{ch.chunk_id}.json").parent.mkdir(
                        parents=True, exist_ok=True
                    )
                    (batch_dir / "extractions" / f"{ch.chunk_id}.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                row["nodes"] = pdf_nodes
                row["edges"] = pdf_edges
                row["status"] = "extracted" if pdf_nodes or pdf_edges else "skip_empty_extraction"
            except Exception as exc:  # noqa: BLE001
                row["status"] = f"error: {exc}"
            report_rows.append(row)
    finally:
        extractor.close()

    nodes, edges, rejected = validate_all(all_extractions)
    graph = merge_graph(nodes, edges, prefer_incoming=True)

    writer = Neo4jWriter()
    before = {"nodes": 0, "relationships": 0}
    after = {"nodes": 0, "relationships": 0}
    try:
        writer.verify()
        with writer.driver.session(database=writer.database) as s:
            before = {
                "nodes": s.run("MATCH (n) RETURN count(n) AS c").single()["c"],
                "relationships": s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"],
            }
        stats = writer.write_graph(graph, clear_first=False)
        with writer.driver.session(database=writer.database) as s:
            after = {
                "nodes": s.run("MATCH (n) RETURN count(n) AS c").single()["c"],
                "relationships": s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"],
            }
    finally:
        writer.close()

    # Mark only successful skips/extracts; keep errors pending for retry
    for row in report_rows:
        st = row.get("status") or ""
        if st.startswith("error"):
            continue
        name = row["pdf"]
        if name not in done:
            state.setdefault("completed_pdfs", []).append(name)
            done.add(name)

    batch_report = {
        "batch_size": len(batch),
        "pdfs": [p.name for p in batch],
        "per_pdf": report_rows,
        "extracted_chunks_items": len(all_extractions),
        "merged_nodes": len(graph["nodes"]),
        "merged_edges": len(graph["edges"]),
        "rejected": len(rejected),
        "property_replacements": sum(
            1 for c in graph.get("conflicts") or [] if c.get("kind") == "property_replaced"
        ),
        "neo4j_before": before,
        "neo4j_write_stats": stats,
        "neo4j_after": after,
    }
    state.setdefault("batches", []).append(batch_report)
    save_state(out, state)

    (batch_dir / f"batch_{len(state['batches']):03d}_report.json").write_text(
        json.dumps(batch_report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (batch_dir / "latest_merged_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    print(json.dumps(batch_report, ensure_ascii=False, indent=2))
    return batch_report


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--batch-index", type=int, default=None, help="0-based among pending")
    args = ap.parse_args()
    run_batch(batch_size=args.batch_size, batch_index=args.batch_index)


if __name__ == "__main__":
    main()
