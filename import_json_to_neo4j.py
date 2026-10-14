"""Import edited extraction JSON files into Neo4j.

Typical workflow:
  1. Edit JSON under out/ref_expand/extractions/  (or copy to out/edited/)
  2. Run this script to validate → merge → write Neo4j

Examples:
  # Import one edited file (merge into existing graph; new literature wins on props)
  .\\.venv\\Scripts\\python.exe scripts\\import_json_to_neo4j.py --files out\\ref_expand\\extractions\\abc123.json

  # Import a folder of edited JSONs
  .\\.venv\\Scripts\\python.exe scripts\\import_json_to_neo4j.py --dir out\\edited

  # Rebuild WHOLE graph from all Ref extractions (DESTRUCTIVE: clears Neo4j first)
  .\\.venv\\Scripts\\python.exe scripts\\import_json_to_neo4j.py --dir out\\ref_expand\\extractions --clear

  # Import into a SECOND Neo4j Desktop instance (different Bolt port)
  .\\.venv\\Scripts\\python.exe scripts\\import_json_to_neo4j.py --dir out\\edited --clear ^
      --uri bolt://localhost:7688 --password YOUR_NEW_PASSWORD
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.merge import merge_graph  # noqa: E402
from src.neo4j_writer import Neo4jWriter  # noqa: E402
from src.validate import validate_all  # noqa: E402


def load_extraction(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path}: root must be a JSON object")
    if "nodes" not in data:
        data["nodes"] = []
    if "edges" not in data:
        data["edges"] = []
    meta = data.get("meta") or {}
    if not isinstance(meta, dict):
        meta = {}
    meta.setdefault("chunk_id", path.stem)
    meta.setdefault("source_pdf", meta.get("source_pdf") or path.stem)
    data["meta"] = meta
    return data


def collect_paths(args: argparse.Namespace) -> list[Path]:
    paths: list[Path] = []
    if args.files:
        for f in args.files:
            p = Path(f)
            if not p.is_absolute():
                p = ROOT / p
            if not p.exists():
                raise FileNotFoundError(p)
            paths.append(p)
    if args.dir:
        d = Path(args.dir)
        if not d.is_absolute():
            d = ROOT / d
        if not d.is_dir():
            raise NotADirectoryError(d)
        paths.extend(sorted(d.glob("*.json")))
    # de-dupe preserve order
    seen: set[Path] = set()
    out: list[Path] = []
    for p in paths:
        rp = p.resolve()
        if rp not in seen:
            seen.add(rp)
            out.append(rp)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="Import extraction JSON → Neo4j")
    ap.add_argument("--files", nargs="+", help="Specific JSON file(s)")
    ap.add_argument("--dir", help="Directory of *.json extractions")
    ap.add_argument(
        "--clear",
        action="store_true",
        help="CLEAR the entire Neo4j graph before import (destructive)",
    )
    ap.add_argument(
        "--no-prefer-incoming",
        action="store_true",
        help="Keep old property values on conflict (default: edited JSON wins)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate+merge only; do not write Neo4j",
    )
    ap.add_argument("--uri", help="Neo4j Bolt URI (default from config.yaml)")
    ap.add_argument("--user", default=None, help="Neo4j user (default neo4j)")
    ap.add_argument("--password", help="Neo4j password (default from .env)")
    ap.add_argument("--database", help="Neo4j database name (default neo4j)")
    args = ap.parse_args()

    if not args.files and not args.dir:
        ap.error("Provide --files and/or --dir")

    paths = collect_paths(args)
    if not paths:
        print("No JSON files found.")
        sys.exit(1)

    print(f"Loading {len(paths)} JSON file(s)...")
    extractions: list[dict[str, Any]] = []
    for p in paths:
        try:
            extractions.append(load_extraction(p))
        except Exception as exc:  # noqa: BLE001
            print(f"  SKIP {p.name}: {exc}")

    nodes, edges, rejected = validate_all(extractions)
    prefer = not args.no_prefer_incoming
    graph = merge_graph(nodes, edges, prefer_incoming=prefer)

    report = {
        "files": len(paths),
        "loaded": len(extractions),
        "accepted_nodes_raw": len(nodes),
        "accepted_edges_raw": len(edges),
        "rejected": len(rejected),
        "merged_nodes": len(graph["nodes"]),
        "merged_edges": len(graph["edges"]),
        "clear": bool(args.clear),
        "prefer_incoming": prefer,
        "dry_run": bool(args.dry_run),
        "uri": args.uri,
        "database": args.database,
    }

    out_dir = ROOT / "out"
    out_dir.mkdir(exist_ok=True)
    (out_dir / "import_json_preview_graph.json").write_text(
        json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if rejected:
        (out_dir / "import_json_rejected.json").write_text(
            json.dumps(rejected[:200], ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )

    print(json.dumps({k: report[k] for k in report if k != "dry_run"}, ensure_ascii=False, indent=2))
    print(f"preview graph → {out_dir / 'import_json_preview_graph.json'}")
    if rejected:
        print(f"rejected sample → {out_dir / 'import_json_rejected.json'} ({len(rejected)} total)")

    if args.dry_run:
        print("Dry-run: Neo4j not modified.")
        return

    if args.clear:
        print("WARNING: clearing entire Neo4j graph first...")

    writer = Neo4jWriter(
        uri=args.uri,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    try:
        print(f"Connecting → {args.uri or 'config default'} / db={writer.database}")
        writer.verify()
        with writer.driver.session(database=writer.database) as s:
            before = {
                "nodes": s.run("MATCH (n) RETURN count(n) AS c").single()["c"],
                "relationships": s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"],
            }
        stats = writer.write_graph(graph, clear_first=bool(args.clear))
        with writer.driver.session(database=writer.database) as s:
            after = {
                "nodes": s.run("MATCH (n) RETURN count(n) AS c").single()["c"],
                "relationships": s.run("MATCH ()-[r]->() RETURN count(r) AS c").single()["c"],
            }
    finally:
        writer.close()

    report["neo4j_before"] = before
    report["neo4j_write_stats"] = stats
    report["neo4j_after"] = after
    report["target_database"] = writer.database
    (out_dir / "import_json_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("Neo4j before:", before)
    print("Neo4j after:", after)
    print(f"report → {out_dir / 'import_json_report.json'}")


if __name__ == "__main__":
    main()
