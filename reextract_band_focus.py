"""Re-extract d-band / p-band / d-p hybridization from already-extracted Ref PDFs.

Skips papers previously marked skip_not_solid_flp.
Default: first 40 completed PDFs' extracted subset (29 papers).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

from tqdm import tqdm

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_utils import ensure_out_dir, load_config, project_root  # noqa: E402
from src.llm_extract import FLPExtractor  # noqa: E402
from src.merge import merge_graph  # noqa: E402
from src.neo4j_writer import Neo4jWriter  # noqa: E402
from src.section_extract import sections_to_chunks  # noqa: E402
from src.validate import soft_name_key, validate_all  # noqa: E402

STATE_NAME = "ref_expand_state.json"

BAND_PROMPT = """
ADDITIONAL STRICT RULES — BAND DESCRIPTOR RE-PASS:
12. Extract ONLY solid/heterogeneous FLP context PLUS electronic band descriptors.
13. PRIORITY targets (must extract whenever mentioned, even briefly):
    - ElectronicDescriptor named exactly: "d-band center"
      (aliases in text: d band center, εd, Ed, d-band-center)
    - ElectronicDescriptor named exactly: "p-band center"
      (aliases: p band center, εp, Ep, O p-band center → still use "p-band center"
       and put specificity like "O 2p" in props.note)
    - ElectronicDescriptor named exactly: "d-p hybridization"
      (aliases: d–p hybridization, d-p orbital hybridization, d-p interaction,
       d–p interaction, orbital hybridization between d and p)
14. Also extract closely related support nodes if needed for edges:
    Material/Catalyst/FLP/LewisAcid/LewisBase/ActiveSite/Defect/Activity/Reaction.
15. Required edge patterns when text supports them:
    (Material|Catalyst|FLP|ActiveSite|LewisAcid|LewisBase)-[:HAS_DESCRIPTOR|DESCRIBED_BY]->(ElectronicDescriptor)
    (ElectronicDescriptor)-[:CORRELATES_WITH]->(Activity|Reaction)
    Optional: (ElectronicDescriptor)-[:FACILITATES|INVOLVES]->(FLP|Mechanism)
16. Capture qualitative shifts in props when stated, e.g.
    props.shift = "upshift toward Fermi level" / "downshift" / numeric εd if given.
17. If a chunk has NO mention of d-band / p-band / d-p / εd / εp / hybridization
    related to d/p orbitals, return {"nodes":[], "edges":[]}.
18. Do NOT invent band descriptors absent from the text.
19. Output JSON only with nodes/edges.
"""

BAND_TEXT_RE = re.compile(
    r"(?i)("
    r"d\s*[-–—]?\s*band|p\s*[-–—]?\s*band|"
    r"ε\s*d|ε\s*p|\bed\b|\bep\b|"
    r"d\s*[-–—]\s*p|d\s*/\s*p|"
    r"orbital\s+hybrid|hybridization|"
    r"d\s*[-–—]\s*p\s+interact|"
    r"pdos|projected\s+density\s+of\s+states"
    r")"
)

CANON = {
    "d-band center": re.compile(
        r"(?i)^(o\s*)?(d\s*[-–—]?\s*band(\s*center)?|ε\s*d|ed|d-band-center)$"
    ),
    "p-band center": re.compile(
        r"(?i)^(o\s*)?(p\s*[-–—]?\s*band(\s*center)?|ε\s*p|ep|p-band-center|"
        r"o\s*p\s*[-–—]?\s*band(\s*center)?)$"
    ),
    "d-p hybridization": re.compile(
        r"(?i)^(d\s*[-–—/]\s*p(\s*orbital)?\s*(hybridization|interaction|interact)|"
        r"orbital\s+hybridization)$"
    ),
}


def load_extracted_from_first_n(n: int = 40) -> list[str]:
    state = json.loads((project_root() / "out" / STATE_NAME).read_text(encoding="utf-8"))
    first = set((state.get("completed_pdfs") or [])[:n])
    out: list[str] = []
    for b in (state.get("batches") or [])[:4]:
        for row in b.get("per_pdf") or []:
            if row.get("pdf") in first and row.get("status") == "extracted":
                if row["pdf"] not in out:
                    out.append(row["pdf"])
    return out


def canonicalize_extraction(data: dict[str, Any]) -> dict[str, Any]:
    """Force band descriptor names/labels to canonical ElectronicDescriptor."""
    nodes = []
    for n in data.get("nodes") or []:
        if not isinstance(n, dict):
            continue
        name = str(n.get("name") or "").strip()
        low = soft_name_key(name)
        canon = None
        if "dband" in low and "center" in low or low in {"ed", "εd", "dband"}:
            canon = "d-band center"
        elif "pband" in low and "center" in low or low in {"ep", "εp", "pband", "opbandcenter"}:
            canon = "p-band center"
        elif ("dp" in low or "d-p" in name.lower() or "d–p" in name) and (
            "hybrid" in low or "interact" in low
        ):
            canon = "d-p hybridization"
        elif low in {"orbitalhybridization", "dphybridization", "dporbitalhybridization"}:
            canon = "d-p hybridization"
        else:
            for cname, pat in CANON.items():
                if pat.match(name.strip()):
                    canon = cname
                    break

        if canon:
            props = dict(n.get("props") or {})
            if name and name != canon:
                props.setdefault("note", name)
            n = {
                **n,
                "label": "ElectronicDescriptor",
                "name": canon,
                "props": props,
            }
        nodes.append(n)

    edges = []
    for e in data.get("edges") or []:
        if not isinstance(e, dict):
            continue
        e = dict(e)
        for end in ("source", "target"):
            ep = e.get(end) or {}
            if not isinstance(ep, dict):
                continue
            nm = str(ep.get("name") or "")
            low = soft_name_key(nm)
            if "dband" in low and "center" in low:
                e[end] = {**ep, "label": "ElectronicDescriptor", "name": "d-band center"}
            elif "pband" in low and "center" in low:
                e[end] = {**ep, "label": "ElectronicDescriptor", "name": "p-band center"}
            elif ("hybrid" in low or "interact" in low) and ("dp" in low or "d-p" in nm.lower()):
                e[end] = {**ep, "label": "ElectronicDescriptor", "name": "d-p hybridization"}
        edges.append(e)

    return {**data, "nodes": nodes, "edges": edges}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--first-n", type=int, default=40, help="Among first N completed PDFs")
    ap.add_argument("--limit", type=int, default=0, help="Debug: only first K extracted PDFs")
    args = ap.parse_args()

    cfg = load_config()
    out = ensure_out_dir(cfg)
    band_dir = out / "band_repass"
    band_dir.mkdir(parents=True, exist_ok=True)
    (band_dir / "extractions").mkdir(exist_ok=True)

    pdf_names = load_extracted_from_first_n(args.first_n)
    if args.limit:
        pdf_names = pdf_names[: args.limit]
    ref = project_root() / "Ref"
    pdfs = [ref / n for n in pdf_names if (ref / n).exists()]
    print(f"Band re-pass on {len(pdfs)} extracted PDFs (excluded skips)")

    keywords = list(cfg.get("priority_keywords") or [])
    for kw in (
        "d-band",
        "p-band",
        "d-band center",
        "p-band center",
        "hybridization",
        "d-p",
        "εd",
        "εp",
        "pdos",
    ):
        if kw not in keywords:
            keywords.append(kw)

    chunk_cfg = cfg["chunking"]
    extractor = FLPExtractor()
    extractor.system = extractor.system + BAND_PROMPT

    all_extractions: list[dict[str, Any]] = []
    report_rows: list[dict[str, Any]] = []

    try:
        for pdf in tqdm(pdfs, desc="Band re-pass"):
            row: dict[str, Any] = {
                "pdf": pdf.name,
                "status": "",
                "chunks": 0,
                "band_chunks": 0,
                "nodes": 0,
                "edges": 0,
            }
            try:
                chunks = sections_to_chunks(
                    pdf,
                    target_chars=int(chunk_cfg["target_chars"]),
                    overlap_chars=int(chunk_cfg["overlap_chars"]),
                    min_chars=int(chunk_cfg["min_chars"]),
                    keywords=keywords,
                )
                chunks = sorted(chunks, key=lambda c: (-c.score, c.page_start))[:20]
                row["chunks"] = len(chunks)
                band_chunks = [c for c in chunks if BAND_TEXT_RE.search(c.text or "")]
                row["band_chunks"] = len(band_chunks)
                if not band_chunks:
                    row["status"] = "skip_no_band_mention"
                    report_rows.append(row)
                    continue

                n_nodes = n_edges = 0
                for ch in band_chunks:
                    data = extractor.extract_chunk(ch)
                    data = canonicalize_extraction(data)
                    if not data.get("nodes") and not data.get("edges"):
                        continue
                    all_extractions.append(data)
                    n_nodes += len(data.get("nodes") or [])
                    n_edges += len(data.get("edges") or [])
                    (band_dir / "extractions" / f"{ch.chunk_id}.json").write_text(
                        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
                    )
                row["nodes"] = n_nodes
                row["edges"] = n_edges
                row["status"] = "extracted" if n_nodes or n_edges else "skip_empty_band_extract"
            except Exception as exc:  # noqa: BLE001
                row["status"] = f"error: {exc}"
            report_rows.append(row)
    finally:
        extractor.close()

    nodes, edges, rejected = validate_all(all_extractions)
    graph = merge_graph(nodes, edges, prefer_incoming=True)

    writer = Neo4jWriter()
    before = after = {"nodes": 0, "relationships": 0}
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
            # Ensure canonical band nodes are ElectronicDescriptor
            for name in ("d-band center", "p-band center", "d-p hybridization"):
                s.run(
                    """
                    MATCH (n)
                    WHERE toLower(n.name) = toLower($name)
                       OR n.name_key = $nk
                    SET n:ElectronicDescriptor:Descriptor,
                        n.name = $name,
                        n.name_key = $nk
                    """,
                    name=name,
                    nk=soft_name_key(name),
                )
            band_stats = list(
                s.run(
                    """
                    MATCH (n:ElectronicDescriptor)
                    WHERE n.name IN ['d-band center','p-band center','d-p hybridization']
                    OPTIONAL MATCH (n)-[r]-(x)
                    RETURN n.name AS name, size([(n)--()|1]) AS deg,
                           collect(DISTINCT type(r)+':'+coalesce(labels(x)[0],'?')+':'+coalesce(x.name,'?'))[0..8] AS links
                    ORDER BY name
                    """
                )
            )
    finally:
        writer.close()

    report = {
        "pdf_count": len(pdfs),
        "per_pdf": report_rows,
        "extracted_items": len(all_extractions),
        "merged_nodes": len(graph["nodes"]),
        "merged_edges": len(graph["edges"]),
        "rejected": len(rejected),
        "neo4j_before": before,
        "neo4j_write_stats": stats,
        "neo4j_after": after,
        "band_nodes": [dict(r) for r in band_stats],
        "summary": {
            "with_band_chunks": sum(1 for r in report_rows if r.get("band_chunks", 0) > 0),
            "extracted": sum(1 for r in report_rows if r.get("status") == "extracted"),
            "skip_no_band": sum(1 for r in report_rows if r.get("status") == "skip_no_band_mention"),
            "skip_empty": sum(1 for r in report_rows if r.get("status") == "skip_empty_band_extract"),
            "errors": sum(1 for r in report_rows if str(r.get("status", "")).startswith("error")),
        },
    }
    (band_dir / "band_repass_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    print(json.dumps({"summary": report["summary"], "before": before, "after": after, "band_nodes": report["band_nodes"]}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
