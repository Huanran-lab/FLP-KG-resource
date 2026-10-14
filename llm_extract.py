"""DeepSeek LLM extraction constrained by flp_schema.yaml."""
from __future__ import annotations

import json
import re
from typing import Any

import httpx
from tenacity import retry, stop_after_attempt, wait_exponential
from tqdm import tqdm

from .config_utils import (
    ensure_out_dir,
    load_config,
    load_schema,
    project_root,
    read_api_key,
)
from .pdf_extract import TextChunk, load_chunks_from_out, process_pdf_dir


def _schema_prompt(schema: dict[str, Any]) -> str:
    labels = schema["node_labels"]
    rels = schema["relationship_types"]
    patterns = schema.get("canonical_patterns", [])
    notes = schema.get("notes", [])
    return f"""You are an expert in heterogeneous / solid Frustrated Lewis Pairs (FLP) catalysis.
Extract a knowledge-graph fragment from the given scientific text.

STRICT RULES:
1. Output ONLY valid JSON (no markdown fences, no commentary).
2. Use ONLY these node labels: {labels}
3. Use ONLY these relationship types: {rels}
4. Prefer these canonical patterns when supported by the text:
{chr(10).join('- ' + p for p in patterns)}
5. Notes:
{chr(10).join('- ' + n for n in notes)}
6. Every node MUST have: label (string), name (short canonical string), props (object, may be empty).
7. Every edge MUST have: type, source {{label, name}}, target {{label, name}}, props (object, may be empty).
8. Do NOT invent facts not supported by the text. If unsure, omit.
9. Prefer concise entity names (e.g. "CeO2", "oxygen vacancy", "surface FLP", "CO2 hydrogenation").
10. Do not invent labels or relationship types outside the allowed lists above.
11. Focus on solid / heterogeneous FLP structure, defects, acid/base sites, activation, and catalysis.

JSON schema:
{{
  "nodes": [{{"label": "Material", "name": "CeO2", "props": {{}}}}],
  "edges": [{{
    "type": "HAS_DEFECT",
    "source": {{"label": "Material", "name": "CeO2"}},
    "target": {{"label": "Defect", "name": "oxygen vacancy"}},
    "props": {{}}
  }}]
}}
"""


def _strip_code_fence(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _parse_json(text: str) -> dict[str, Any]:
    text = _strip_code_fence(text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        # Try to salvage the first {...} block
        m = re.search(r"\{.*\}", text, flags=re.DOTALL)
        if not m:
            raise
        data = json.loads(m.group(0))
    if not isinstance(data, dict):
        raise ValueError("LLM output is not a JSON object")
    data.setdefault("nodes", [])
    data.setdefault("edges", [])
    return data


class FLPExtractor:
    def __init__(self) -> None:
        self.cfg = load_config()
        self.schema = load_schema()
        timeout = float(self.cfg["llm"].get("timeout_s", 120))
        # trust_env=False avoids broken system HTTP proxies on Windows.
        # Use raw httpx (not openai SDK) to avoid Windows App Control blocking jiter DLL.
        self._http = httpx.Client(trust_env=False, timeout=timeout)
        self.api_key = read_api_key(self.cfg)
        self.base_url = self.cfg["llm"]["base_url"].rstrip("/")
        self.model = self.cfg["llm"]["model"]
        self.temperature = float(self.cfg["llm"].get("temperature", 0.1))
        self.max_tokens = int(self.cfg["llm"].get("max_tokens", 4096))
        self.system = _schema_prompt(self.schema)
        self.max_retries = int(self.cfg["llm"].get("max_retries", 4))

    def close(self) -> None:
        self._http.close()

    def _chat_json(self, user: str) -> str:
        url = f"{self.base_url}/chat/completions"
        payload = {
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "messages": [
                {"role": "system", "content": self.system},
                {"role": "user", "content": user},
            ],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
        }
        resp = self._http.post(
            url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
        )
        resp.raise_for_status()
        data = resp.json()
        return data["choices"][0]["message"].get("content") or "{}"

    @retry(wait=wait_exponential(multiplier=1, min=2, max=30), stop=stop_after_attempt(4), reraise=True)
    def extract_chunk(self, chunk: TextChunk) -> dict[str, Any]:
        user = (
            f"Source PDF: {chunk.source_pdf}\n"
            f"Pages: {chunk.page_start}-{chunk.page_end}\n"
            f"Keywords hit: {', '.join(chunk.keywords_hit) or 'none'}\n\n"
            f"TEXT:\n{chunk.text}"
        )
        content = self._chat_json(user)
        data = _parse_json(content)
        data["meta"] = {
            "chunk_id": chunk.chunk_id,
            "source_pdf": chunk.source_pdf,
            "page_start": chunk.page_start,
            "page_end": chunk.page_end,
            "score": chunk.score,
        }
        return data


def extract_all(chunks: list[TextChunk] | None = None, resume: bool = True) -> list[dict[str, Any]]:
    cfg = load_config()
    out = ensure_out_dir(cfg)
    if chunks is None:
        chunks_path = project_root() / "out" / "chunks_all.json"
        if chunks_path.exists():
            chunks = load_chunks_from_out()
        else:
            chunks = process_pdf_dir()

    extractor = FLPExtractor()
    results: list[dict[str, Any]] = []
    done_ids: set[str] = set()
    agg_path = out / "extractions_all.json"
    if resume and agg_path.exists():
        try:
            prev = json.loads(agg_path.read_text(encoding="utf-8"))
            for item in prev:
                cid = item.get("meta", {}).get("chunk_id")
                if cid:
                    done_ids.add(cid)
                    results.append(item)
        except Exception:
            results = []
            done_ids = set()

    pending = [c for c in chunks if c.chunk_id not in done_ids]
    try:
        for chunk in tqdm(pending, desc="LLM extract"):
            try:
                data = extractor.extract_chunk(chunk)
                results.append(data)
                (out / "extractions" / f"{chunk.chunk_id}.json").write_text(
                    json.dumps(data, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                agg_path.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
            except Exception as exc:  # noqa: BLE001
                err = {
                    "meta": {
                        "chunk_id": chunk.chunk_id,
                        "source_pdf": chunk.source_pdf,
                        "page_start": chunk.page_start,
                        "page_end": chunk.page_end,
                        "score": chunk.score,
                        "error": str(exc),
                    },
                    "nodes": [],
                    "edges": [],
                }
                results.append(err)
                (out / "extractions" / f"{chunk.chunk_id}.error.json").write_text(
                    json.dumps(err, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
                agg_path.write_text(
                    json.dumps(results, ensure_ascii=False, indent=2),
                    encoding="utf-8",
                )
    finally:
        extractor.close()
    return results


if __name__ == "__main__":
    extract_all()
