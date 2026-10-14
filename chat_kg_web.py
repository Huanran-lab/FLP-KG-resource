"""Browser GraphRAG chat UI (stdlib HTTP server, no extra deps).

Flow: 自然语言 → Cypher → Neo4j → 自然语言
"""
from __future__ import annotations

import argparse
import json
import sys
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from src.graphrag_chat import GraphRAGChat  # noqa: E402

HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>Solid FLP GraphRAG</title>
<style>
  :root {
    --bg: #f3efe6;
    --panel: #fffdf8;
    --ink: #1c2430;
    --muted: #5c6675;
    --line: #d7d0c3;
    --accent: #0f6a5a;
    --accent-soft: #d9efe9;
    --code: #12202b;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    color: var(--ink); background:
      radial-gradient(900px 420px at 10% -10%, #e7f3ef 0%, transparent 55%),
      radial-gradient(700px 380px at 100% 0%, #f7e8d6 0%, transparent 50%),
      var(--bg);
    min-height: 100vh;
  }
  .wrap { max-width: 920px; margin: 0 auto; padding: 28px 18px 40px; }
  h1 { margin: 0 0 6px; font-size: 1.55rem; letter-spacing: .01em; }
  .sub { color: var(--muted); margin-bottom: 18px; font-size: .95rem; }
  .flow {
    display: flex; flex-wrap: wrap; gap: 8px; margin-bottom: 18px;
  }
  .flow span {
    background: var(--accent-soft); color: var(--accent);
    padding: 6px 10px; border-radius: 999px; font-size: .82rem; font-weight: 600;
  }
  #log {
    background: var(--panel); border: 1px solid var(--line); border-radius: 16px;
    min-height: 420px; max-height: 62vh; overflow: auto; padding: 16px;
    box-shadow: 0 10px 30px rgba(28,36,48,.05);
  }
  .msg { margin: 0 0 14px; }
  .msg .role { font-size: .75rem; font-weight: 700; color: var(--muted); margin-bottom: 4px; }
  .msg.user .bubble {
    background: #eaf3ff; border: 1px solid #c9dbf5; padding: 10px 12px; border-radius: 12px;
  }
  .msg.bot .bubble {
    background: #fff; border: 1px solid var(--line); padding: 10px 12px; border-radius: 12px;
  }
  .meta {
    margin-top: 8px; font-size: .8rem; color: var(--muted);
  }
  pre {
    white-space: pre-wrap; word-break: break-word; background: var(--code); color: #d7e7f2;
    padding: 10px 12px; border-radius: 10px; font-size: .78rem; overflow: auto;
  }
  .composer {
    display: flex; gap: 10px; margin-top: 14px;
  }
  textarea {
    flex: 1; resize: vertical; min-height: 64px; max-height: 160px;
    border: 1px solid var(--line); border-radius: 12px; padding: 12px;
    font: inherit; background: #fff;
  }
  button {
    border: 0; border-radius: 12px; background: var(--accent); color: white;
    padding: 0 18px; font-weight: 700; cursor: pointer; min-width: 96px;
  }
  button:disabled { opacity: .55; cursor: wait; }
  .hints { margin-top: 6px; }
  .hint {
    display: inline-block; margin: 2px 4px 0 0; padding: 2px 8px;
    border-radius: 999px; background: #f0ebe1; font-size: .75rem;
  }
  .err { color: #9b2c2c; }
  .step {
    margin-top: 10px; padding-top: 8px; border-top: 1px dashed var(--line);
  }
  .step-title {
    font-size: .78rem; font-weight: 700; color: var(--accent); margin-bottom: 4px;
  }
  details.rows-box { margin-top: 6px; }
  details.rows-box summary {
    cursor: pointer; color: var(--muted); font-size: .8rem; user-select: none;
  }
</style>
</head>
<body>
  <div class="wrap">
    <h1>Solid FLP 知识图谱对话</h1>
    <div class="sub">可看见完整调用链：实体锚定 → 生成 Cypher → Neo4j 返回行 → 再总结成回答（库：FLPCO2RR）</div>
    <div class="flow">
      <span>1. 提问</span>
      <span>2. 锚定实体</span>
      <span>3. NL → Cypher</span>
      <span>4. Neo4j 查库</span>
      <span>5. 证据 → 回答</span>
    </div>
    <div id="log"></div>
    <div class="composer">
      <textarea id="q" placeholder="例如：CeO2 上的氧空位如何形成 surface FLP？它能催化哪些反应？"></textarea>
      <button id="send">发送</button>
    </div>
  </div>
<script>
const log = document.getElementById('log');
const q = document.getElementById('q');
const send = document.getElementById('send');

function addUser(text) {
  const el = document.createElement('div');
  el.className = 'msg user';
  el.innerHTML = `<div class="role">你</div><div class="bubble"></div>`;
  el.querySelector('.bubble').textContent = text;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

function addBot(data) {
  const el = document.createElement('div');
  el.className = 'msg bot';
  const hints = (data.hints || []).map(h =>
    `<span class="hint">${h.label}: ${h.name}</span>`).join('');
  const rowsJson = JSON.stringify(data.rows || [], null, 2);
  el.innerHTML = `
    <div class="role">图谱助手 · KG 调用过程</div>
    <div class="bubble">
      <div class="step">
        <div class="step-title">① 锚定实体（从问题匹配到图谱节点）</div>
        <div class="hints">${hints || '<span class="hint">无</span>'}</div>
      </div>
      <div class="step">
        <div class="step-title">② 生成的 Cypher（实际发给 Neo4j 的查询）</div>
        <pre class="cypher"></pre>
        ${data.error ? `<div class="meta err">查询错误：${data.error}</div>` : ''}
      </div>
      <div class="step">
        <div class="step-title">③ Neo4j 返回</div>
        <div class="meta">行数：${data.row_count ?? 0}</div>
        <details class="rows-box" open>
          <summary>展开/收起原始行（最多 20 条）</summary>
          <pre class="rows"></pre>
        </details>
      </div>
      <div class="step">
        <div class="step-title">④ 基于图谱证据的回答</div>
        <div class="answer"></div>
      </div>
    </div>`;
  el.querySelector('.answer').textContent = data.answer || '(无回答)';
  el.querySelector('.cypher').textContent = data.cypher || '';
  el.querySelector('.rows').textContent = rowsJson;
  log.appendChild(el);
  log.scrollTop = log.scrollHeight;
}

async function ask() {
  const text = q.value.trim();
  if (!text) return;
  q.value = '';
  addUser(text);
  send.disabled = true;
  try {
    const resp = await fetch('/api/chat', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({question: text}),
    });
    const data = await resp.json();
    if (!resp.ok) throw new Error(data.error || resp.statusText);
    addBot(data);
  } catch (e) {
    addBot({answer: '请求失败：' + e.message, cypher: '', hints: [], row_count: 0, error: String(e)});
  } finally {
    send.disabled = false;
    q.focus();
  }
}

send.addEventListener('click', ask);
q.addEventListener('keydown', (e) => {
  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); ask(); }
});
</script>
</body>
</html>
"""


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--uri", help="Neo4j Bolt URI (default from config.yaml)")
    ap.add_argument("--user", default=None, help="Neo4j user")
    ap.add_argument("--password", help="Neo4j password (default from .env)")
    ap.add_argument("--database", help="Neo4j database name")
    ap.add_argument("--max-rows", type=int, default=40)
    args = ap.parse_args()

    chat = GraphRAGChat(
        max_rows=args.max_rows,
        uri=args.uri,
        user=args.user,
        password=args.password,
        database=args.database,
    )
    print(f"Neo4j: {chat.uri}  user={chat.user}  db={chat.database}")
    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *a) -> None:  # quieter
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % a))

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path in ("/", "/index.html"):
                self._send(200, HTML.encode("utf-8"), "text/html; charset=utf-8")
                return
            if path == "/api/health":
                self._send(200, b'{"ok":true}', "application/json")
                return
            self._send(404, b"not found", "text/plain")

        def do_POST(self) -> None:  # noqa: N802
            path = urlparse(self.path).path
            if path != "/api/chat":
                self._send(404, b"not found", "text/plain")
                return
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n else b"{}"
            try:
                payload = json.loads(raw.decode("utf-8"))
                question = (payload.get("question") or "").strip()
                if not question:
                    raise ValueError("empty question")
                with lock:
                    turn = chat.ask(question)
                out = {
                    "answer": turn.answer,
                    "cypher": turn.cypher,
                    "hints": turn.hints,
                    "row_count": len(turn.rows),
                    "error": turn.error,
                    "rows": turn.rows[:20],
                }
                body = json.dumps(out, ensure_ascii=False).encode("utf-8")
                self._send(200, body, "application/json; charset=utf-8")
            except Exception as exc:  # noqa: BLE001
                body = json.dumps({"error": str(exc)}, ensure_ascii=False).encode("utf-8")
                self._send(500, body, "application/json; charset=utf-8")

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"GraphRAG web chat: {url}")
    print("Flow: 自然语言 -> Cypher -> Neo4j -> 自然语言")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nBye.")
    finally:
        server.server_close()
        chat.close()


if __name__ == "__main__":
    main()
