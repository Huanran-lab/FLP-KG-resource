"""Interactive GraphRAG chat over the Solid FLP Neo4j knowledge graph."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Windows consoles often default to GBK; force UTF-8 for chemical formulas.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graphrag_chat import GraphRAGChat  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Chat with the FLP knowledge graph (GraphRAG)")
    parser.add_argument("-q", "--question", help="Single question (non-interactive)")
    parser.add_argument("--show-cypher", action="store_true", default=True)
    parser.add_argument("--hide-cypher", action="store_true")
    parser.add_argument("--show-rows", action="store_true", help="Print raw query rows JSON")
    parser.add_argument("--max-rows", type=int, default=40)
    parser.add_argument("--uri", help="Neo4j Bolt URI (default from config.yaml)")
    parser.add_argument("--user", default=None, help="Neo4j user")
    parser.add_argument("--password", help="Neo4j password (default from .env)")
    parser.add_argument("--database", help="Neo4j database name")
    args = parser.parse_args()
    show_cypher = not args.hide_cypher

    chat = GraphRAGChat(
        max_rows=args.max_rows,
        uri=args.uri,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    try:
        if args.question:
            turn = chat.ask(args.question)
            if turn.hints:
                print("\n[Entity hints]")
                for h in turn.hints[:12]:
                    print(f"  - {h['label']}: {h['name']}")
            if show_cypher:
                print("\n[Cypher]")
                print(turn.cypher)
            if turn.error:
                print("\n[Query error]")
                print(turn.error)
            if args.show_rows:
                print("\n[Rows]")
                print(json.dumps(turn.rows, ensure_ascii=False, indent=2))
            print("\n[Answer]")
            print(turn.answer)
            return

        print("Solid FLP Knowledge Graph Chat (GraphRAG)")
        print("Flow: 自然语言 -> Cypher -> Neo4j -> 自然语言回答")
        print("Web UI:  .\\.venv\\Scripts\\python.exe scripts\\chat_kg_web.py")
        print("Type a question in Chinese or English.")
        print("Commands: /cypher (toggle)  /rows (toggle)  /exit")
        print("-" * 60)
        show_rows = args.show_rows
        while True:
            try:
                q = input("\nYou> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                break
            if not q:
                continue
            if q.lower() in {"/exit", "/quit", "exit", "quit"}:
                print("Bye.")
                break
            if q.lower() == "/cypher":
                show_cypher = not show_cypher
                print(f"show_cypher = {show_cypher}")
                continue
            if q.lower() == "/rows":
                show_rows = not show_rows
                print(f"show_rows = {show_rows}")
                continue

            turn = chat.ask(q)
            if turn.hints:
                print("\n[Entity hints]")
                for h in turn.hints[:12]:
                    print(f"  - {h['label']}: {h['name']}")
            if show_cypher:
                print("\n[Cypher]")
                print(turn.cypher)
            if turn.error:
                print("\n[Query error]")
                print(turn.error)
            if show_rows:
                print("\n[Rows]")
                print(json.dumps(turn.rows, ensure_ascii=False, indent=2)[:4000])
            print("\n[Answer]")
            print(turn.answer)
    finally:
        chat.close()


if __name__ == "__main__":
    main()
