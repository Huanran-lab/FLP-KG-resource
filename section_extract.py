"""Extract Abstract / Results&Discussion / Conclusion text from PDFs."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import fitz

from .pdf_extract import TextChunk, _normalize_ws, chunk_pages


KEEP_PAT = re.compile(
    r"(?ix)(?:^|\n)\s*(?:\d+(?:\.\d+)*\.?\s*)?("
    r"abstract|synopsis|"
    r"results?\s*(?:and|&|/)\s*discussions?|"
    r"results?\s+and\s+discussion|"
    r"discussion\s+and\s+results?|"
    r"results?(?:\s+and\s+discussion)?|"
    r"discussions?(?:\s+and\s+conclusions?)?|"
    r"conclusions?|concluding\s+remarks|"
    r"summary\s+and\s+conclusions?|"
    r"conclusions?\s+and\s+(?:outlook|perspectives?|summary)"
    r")\b"
)

STOP_PAT = re.compile(
    r"(?ix)(?:^|\n)\s*(?:\d+(?:\.\d+)*\.?\s*)?("
    r"introduction|background|"
    r"experimental(?:\s+section)?|experiments?|"
    r"materials?\s+and\s+methods|methods?(?:\s+and\s+materials)?|"
    r"computational(?:\s+details)?|theoretical(?:\s+methods)?|calculation(?:s)?|"
    r"supporting\s+information|acknowledg(?:e)?ments?|references?|bibliography|"
    r"conflicts?\s+of\s+interest|author\s+contributions|data\s+availability|"
    r"appendix|supplementary|associated\s+content|"
    r"keywords?|graphical\s+abstract|toc\s+(?:graphic|entry)|table\s+of\s+contents"
    r")\b"
)


@dataclass
class SectionSpan:
    name: str
    page_start: int
    page_end: int
    text: str


def _kind_from_heading(h: str) -> str:
    key = re.sub(r"\s+", " ", h.lower()).strip()
    if key.startswith("abstract") or key.startswith("synopsis"):
        return "abstract"
    if "conclusion" in key or key.startswith("summary and"):
        return "conclusion"
    return "results_discussion"


def extract_target_sections(pdf_path: Path) -> list[SectionSpan]:
    doc = fitz.open(pdf_path)
    page_texts: list[tuple[int, str]] = []
    try:
        for i, page in enumerate(doc, start=1):
            t = _normalize_ws(page.get_text("text"))
            if t:
                page_texts.append((i, t))
    finally:
        doc.close()

    if not page_texts:
        return []

    # Build global text with page break markers
    parts: list[str] = []
    page_starts: list[tuple[int, int]] = []  # char_offset, page_no
    pos = 0
    for page_no, text in page_texts:
        page_starts.append((pos, page_no))
        parts.append(text)
        pos += len(text) + 2
    full = "\n\n".join(parts)

    def page_at(char_i: int) -> int:
        pg = page_starts[0][1]
        for off, pno in page_starts:
            if off <= char_i:
                pg = pno
            else:
                break
        return pg

    # Find all keep/stop matches as boundary events
    events: list[tuple[int, str, str]] = []  # start, kind|stop, heading
    for m in KEEP_PAT.finditer(full):
        events.append((m.start(), _kind_from_heading(m.group(1)), m.group(1)))
    for m in STOP_PAT.finditer(full):
        events.append((m.start(), "stop", m.group(1)))
    events.sort(key=lambda x: x[0])

    spans: list[SectionSpan] = []
    for i, (start, kind, _h) in enumerate(events):
        if kind == "stop":
            continue
        end = events[i + 1][0] if i + 1 < len(events) else len(full)
        # skip the heading line itself roughly
        body = full[start:end]
        body = re.sub(r"(?is)^\s*(?:\d+(?:\.\d+)*\.?\s*)?" + re.escape(_h) + r"\b[:.\s]*", "", body, count=1)
        body = _normalize_ws(body)
        if len(body) < 80:
            continue
        spans.append(
            SectionSpan(
                name=kind,
                page_start=page_at(start),
                page_end=page_at(max(start, end - 1)),
                text=body,
            )
        )

    merged: dict[str, SectionSpan] = {}
    for sp in spans:
        if sp.name not in merged:
            merged[sp.name] = sp
        else:
            cur = merged[sp.name]
            merged[sp.name] = SectionSpan(
                name=sp.name,
                page_start=min(cur.page_start, sp.page_start),
                page_end=max(cur.page_end, sp.page_end),
                text=_normalize_ws(cur.text + "\n\n" + sp.text),
            )

    # Fallback: if almost nothing found but filename suggests FLP, take early+late pages
    order = ["abstract", "results_discussion", "conclusion"]
    result = [merged[k] for k in order if k in merged]
    fname = pdf_path.name.lower().replace(" ", "")
    flpish_name = "flp" in fname or "frustratedlewis" in fname or "frustrated" in fname
    if len(result) == 0 or (flpish_name and sum(len(s.text) for s in result) < 500):
        early = page_texts[:2]
        late = page_texts[-2:] if len(page_texts) > 3 else []
        blob_pages = early + late
        blob = _normalize_ws("\n\n".join(t for _, t in blob_pages))
        if len(blob) >= 200:
            result = [
                SectionSpan(
                    name="results_discussion",
                    page_start=blob_pages[0][0],
                    page_end=blob_pages[-1][0],
                    text=blob,
                )
            ]
    return result


def sections_to_chunks(
    pdf_path: Path,
    target_chars: int = 3200,
    overlap_chars: int = 400,
    min_chars: int = 200,
    keywords: list[str] | None = None,
) -> list[TextChunk]:
    keywords = keywords or []
    sections = extract_target_sections(pdf_path)
    if not sections:
        return []
    pages: list[tuple[int, str]] = []
    for sp in sections:
        pages.append((sp.page_start, f"[{sp.name.upper()}]\n{sp.text}"))
    return chunk_pages(
        pages,
        source_pdf=pdf_path.name,
        target_chars=target_chars,
        overlap_chars=overlap_chars,
        min_chars=min_chars,
        keywords=keywords,
    )


def looks_like_solid_flp_candidate(chunks: list[TextChunk], pdf_name: str = "") -> bool:
    text = "\n".join(c.text.lower() for c in chunks)
    fname = (pdf_name or (chunks[0].source_pdf if chunks else "")).lower()
    fname_compact = re.sub(r"[\s_\-]+", "", fname)

    has_flp = (
        "frustrated lewis" in text
        or "frustrated lewis pair" in text
        or re.search(r"\bflps?\b", text) is not None
        or ("lewis acid" in text and "lewis base" in text)
        or "flp" in fname_compact
        or "frustratedlewis" in fname_compact
    )
    solidish = any(
        k in text
        for k in (
            "surface",
            "heterogeneous",
            "solid",
            "oxide",
            "ceo2",
            "ceria",
            "zeolite",
            "mof",
            "defect",
            "oxygen vacancy",
            "tio2",
            "support",
            "catalysis",
            "photocatal",
            "indium oxide",
            "interface",
        )
    ) or any(
        k in fname_compact
        for k in ("surface", "hetero", "ceo2", "oxide", "zeolite", "mof", "solid")
    )
    if not has_flp:
        return False
    return solidish or "surface flp" in text or "heterogeneous" in text
