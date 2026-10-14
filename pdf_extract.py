"""PDF text extraction and keyword-prioritized chunking."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import fitz  # PyMuPDF

from .config_utils import ensure_out_dir, load_config, project_root


@dataclass
class TextChunk:
    chunk_id: str
    source_pdf: str
    page_start: int
    page_end: int
    text: str
    score: float
    keywords_hit: list[str]


def _normalize_ws(text: str) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def extract_pages(pdf_path: Path) -> list[tuple[int, str]]:
    doc = fitz.open(pdf_path)
    pages: list[tuple[int, str]] = []
    try:
        for i, page in enumerate(doc, start=1):
            text = _normalize_ws(page.get_text("text"))
            if text:
                pages.append((i, text))
    finally:
        doc.close()
    return pages


def _score_text(text: str, keywords: list[str]) -> tuple[float, list[str]]:
    lower = text.lower()
    hits: list[str] = []
    score = 0.0
    for kw in keywords:
        count = lower.count(kw.lower())
        if count:
            hits.append(kw)
            score += count
    # Mild bonus for denser scientific signal
    if "frustrated" in lower and "lewis" in lower:
        score += 2.0
    return score, hits


def chunk_pages(
    pages: list[tuple[int, str]],
    source_pdf: str,
    target_chars: int,
    overlap_chars: int,
    min_chars: int,
    keywords: list[str],
) -> list[TextChunk]:
    # Build a continuous stream with page markers
    units: list[tuple[int, str]] = []
    for page_no, text in pages:
        paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
        if not paras:
            paras = [text]
        for para in paras:
            units.append((page_no, para))

    def _emit(buf_local: list[tuple[int, str]]) -> None:
        if not buf_local:
            return
        text = "\n\n".join(p for _, p in buf_local)
        if len(text) < min_chars:
            return
        score, hits = _score_text(text, keywords)
        page_start = buf_local[0][0]
        page_end = buf_local[-1][0]
        raw_id = f"{source_pdf}|{page_start}-{page_end}|{len(chunks)}|{text[:80]}"
        chunk_id = hashlib.sha1(raw_id.encode("utf-8")).hexdigest()[:16]
        chunks.append(
            TextChunk(
                chunk_id=chunk_id,
                source_pdf=source_pdf,
                page_start=page_start,
                page_end=page_end,
                text=text,
                score=score,
                keywords_hit=hits,
            )
        )

    chunks: list[TextChunk] = []
    buf: list[tuple[int, str]] = []
    buf_len = 0
    i = 0
    while i < len(units):
        page_no, para = units[i]

        # Oversized single paragraph: flush buffer, then emit paragraph alone.
        if len(para) > target_chars:
            _emit(buf)
            _emit([(page_no, para)])
            buf, buf_len = [], 0
            i += 1
            continue

        if not buf or buf_len + len(para) + 1 <= target_chars:
            buf.append((page_no, para))
            buf_len += len(para) + 1
            i += 1
            continue

        # Current paragraph does not fit: emit, keep overlap, then retry same i.
        _emit(buf)
        if overlap_chars <= 0:
            buf, buf_len = [], 0
            continue

        keep: list[tuple[int, str]] = []
        keep_len = 0
        for page_no2, para2 in reversed(buf):
            if keep and keep_len + len(para2) + 1 > overlap_chars:
                break
            keep.insert(0, (page_no2, para2))
            keep_len += len(para2) + 1

        # Guarantee progress: if overlap still cannot accept next para, drop overlap.
        if keep_len + len(para) + 1 > target_chars:
            buf, buf_len = [], 0
        else:
            buf, buf_len = keep, keep_len

    _emit(buf)
    return chunks


def prioritize_chunks(chunks: list[TextChunk], max_chunks: int | None) -> list[TextChunk]:
    ranked = sorted(chunks, key=lambda c: (c.score, -c.page_start), reverse=True)
    # Keep score>0 first; if not enough, fill with remaining
    signal = [c for c in ranked if c.score > 0]
    rest = [c for c in ranked if c.score <= 0]
    ordered = signal + rest
    if max_chunks and max_chunks > 0:
        return ordered[:max_chunks]
    return ordered


def process_pdf_dir(pdf_dir: Path | None = None, save: bool = True) -> list[TextChunk]:
    cfg = load_config()
    root = project_root()
    pdf_dir = pdf_dir or (root / cfg["paths"]["pdf_dir"])
    keywords = cfg.get("priority_keywords", [])
    chunk_cfg = cfg["chunking"]
    max_per_pdf = int(cfg["llm"].get("max_chunks_per_pdf", 40))

    all_chunks: list[TextChunk] = []
    pdfs = sorted(pdf_dir.glob("*.pdf"))
    if not pdfs:
        raise FileNotFoundError(f"No PDFs found in {pdf_dir}")

    out = ensure_out_dir(cfg) if save else None
    for pdf in pdfs:
        pages = extract_pages(pdf)
        chunks = chunk_pages(
            pages=pages,
            source_pdf=pdf.name,
            target_chars=int(chunk_cfg["target_chars"]),
            overlap_chars=int(chunk_cfg["overlap_chars"]),
            min_chars=int(chunk_cfg["min_chars"]),
            keywords=keywords,
        )
        selected = prioritize_chunks(chunks, max_per_pdf)
        all_chunks.extend(selected)
        if out is not None:
            payload = {
                "source_pdf": pdf.name,
                "n_pages": len(pages),
                "n_chunks_total": len(chunks),
                "n_chunks_selected": len(selected),
                "chunks": [asdict(c) for c in selected],
            }
            stem = re.sub(r"[^\w\-]+", "_", pdf.stem)[:80]
            (out / "chunks" / f"{stem}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    if out is not None:
        (out / "chunks_all.json").write_text(
            json.dumps([asdict(c) for c in all_chunks], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return all_chunks


def load_chunks_from_out() -> list[TextChunk]:
    path = project_root() / "out" / "chunks_all.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return [TextChunk(**item) for item in data]


if __name__ == "__main__":
    chunks = process_pdf_dir()
    print(f"Selected {len(chunks)} chunks from PDFs")
