"""One-shot pipeline: PDF -> LLM extract -> validate/merge -> Neo4j -> report."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Allow `python scripts/run_pipeline.py` from project root
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config_utils import ensure_out_dir, load_config, project_root  # noqa: E402
from src.llm_extract import extract_all  # noqa: E402
from src.merge import merge_graph  # noqa: E402
from src.neo4j_writer import smoke_queries, write_merged_graph  # noqa: E402
from src.pdf_extract import process_pdf_dir  # noqa: E402
from src.report import build_report, write_report  # noqa: E402
from src.validate import validate_all  # noqa: E402


def run(
    skip_pdf: bool = False,
    skip_llm: bool = False,
    skip_neo4j: bool = False,
    clear_neo4j: bool = True,
    resume_llm: bool = True,
) -> None:
    cfg = load_config()
    out = ensure_out_dir(cfg)
    print(f"[1/5] Project root: {project_root()}")

    if skip_pdf:
        print("[2/5] Skip PDF chunking (using existing out/chunks_all.json)")
    else:
        print("[2/5] Extracting and chunking PDFs...")
        chunks = process_pdf_dir()
        print(f"       Selected {len(chunks)} chunks")

    if skip_llm:
        print("[3/5] Skip LLM extraction (using existing out/extractions_all.json)")
        extractions = json.loads((out / "extractions_all.json").read_text(encoding="utf-8"))
    else:
        print("[3/5] Running DeepSeek extraction...")
        extractions = extract_all(resume=resume_llm)
        print(f"       Extractions: {len(extractions)}")

    print("[4/5] Validating + merging...")
    nodes, edges, rejected = validate_all(extractions)
    graph = merge_graph(nodes, edges)
    (out / "merged_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(
        f"       Nodes={len(graph['nodes'])} Edges={len(graph['edges'])} "
        f"Rejected={len(rejected)} Conflicts={len(graph['conflicts'])}"
    )

    write_stats = None
    smoke = None
    if skip_neo4j:
        print("[5/5] Skip Neo4j write")
    else:
        print("[5/5] Writing to Neo4j...")
        try:
            write_stats = write_merged_graph(graph, clear_first=clear_neo4j)
            print(f"       Neo4j stats: {write_stats}")
            smoke = smoke_queries()
            (out / "smoke_queries.json").write_text(
                json.dumps(smoke, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            print(f"       Neo4j write FAILED: {exc}")
            print("       Graph JSON and review report will still be generated.")

    report = build_report(rejected, graph, write_stats=write_stats)
    md_path, json_path = write_report(report)
    print(f"Report: {md_path}")
    print(f"Report JSON: {json_path}")
    if smoke:
        print(f"Smoke queries: {out / 'smoke_queries.json'}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Solid FLP KG pipeline (zongshu MVP)")
    parser.add_argument("--skip-pdf", action="store_true")
    parser.add_argument("--skip-llm", action="store_true")
    parser.add_argument("--skip-neo4j", action="store_true")
    parser.add_argument("--no-clear", action="store_true", help="Do not clear Neo4j before write")
    parser.add_argument("--no-resume", action="store_true", help="Do not resume LLM extractions")
    args = parser.parse_args()
    run(
        skip_pdf=args.skip_pdf,
        skip_llm=args.skip_llm,
        skip_neo4j=args.skip_neo4j,
        clear_neo4j=not args.no_clear,
        resume_llm=not args.no_resume,
    )


if __name__ == "__main__":
    main()
