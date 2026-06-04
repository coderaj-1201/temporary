"""
PDF Parser
==========

Stack (no Document Intelligence, no OCR):
  - pdfplumber  — text extraction, table detection, bounding boxes
  - pymupdf     — page layout, font sizes for heading detection, metadata
  - LLM (light) — per-page cleaning + table → NL serialisation

Strategy:
  1. pymupdf extracts page text with font metadata → detect headings by font size
  2. pdfplumber extracts tables atomically per page
  3. Header/footer removed by y-position threshold (top 7% / bottom 7% of page)
  4. Light LLM pass per page cleans garbled text, confirms structure
  5. Table → LLM NL summary (embedded) + markdown kept as table_raw
  6. Parent-child chunking: parent = full section, children = paragraphs + tables
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from uuid import uuid4

import pdfplumber
import pymupdf  # fitz

from shared.azure_clients import get_openai_client
from shared.config import settings
from shared.models import ChunkType, RawChunk

logger = logging.getLogger(__name__)


# ── LLM helpers ───────────────────────────────────────────────────────────────

def _llm_clean_page(raw_text: str, page_num: int) -> str:
    """
    Light LLM pass: fix broken hyphenation, remove artefacts,
    normalise whitespace. Returns cleaned text.
    Skips LLM if page is very short (not worth the call).
    """
    text = raw_text.strip()
    if len(text) < 40:
        return text

    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "You are a document cleaning assistant. "
                    "Fix broken hyphenation at line-ends, remove repeated artefacts, "
                    "normalise whitespace. Return ONLY the cleaned text, nothing else."
                ),
            },
            {"role": "user", "content": f"Page {page_num}:\n\n{text[:3000]}"},
        ],
        temperature=0,
        max_tokens=1500,
    )
    return resp.choices[0].message.content.strip()


def _llm_serialise_table(table_markdown: str, context_heading: str) -> str:
    """
    Convert a markdown table to natural language for embedding.
    Original markdown is preserved separately as table_raw.
    """
    if not table_markdown.strip():
        return ""

    client = get_openai_client()
    resp = client.chat.completions.create(
        model=settings.AZURE_OPENAI_LIGHT_LLM_DEPLOYMENT,
        messages=[
            {
                "role": "system",
                "content": (
                    "Convert this table into 2–5 clear natural language sentences "
                    "that capture all key data. Be factual and complete. "
                    "Return ONLY the sentences, no preamble."
                ),
            },
            {
                "role": "user",
                "content": f"Section: {context_heading}\n\nTable:\n{table_markdown}",
            },
        ],
        temperature=0,
        max_tokens=400,
    )
    return resp.choices[0].message.content.strip()


# ── Table extraction via pdfplumber ───────────────────────────────────────────

def _pdfplumber_table_to_markdown(table: list[list]) -> str:
    """Convert pdfplumber table (list of rows, each a list of cells) to markdown."""
    if not table or not table[0]:
        return ""

    # Sanitise cells
    def cell(v):
        return str(v or "").strip().replace("\n", " ")

    rows = [[cell(c) for c in row] for row in table]
    col_count = max(len(r) for r in rows)

    lines = []
    for i, row in enumerate(rows):
        padded = row + [""] * (col_count - len(row))
        lines.append("| " + " | ".join(padded) + " |")
        if i == 0:
            lines.append("| " + " | ".join(["---"] * col_count) + " |")

    return "\n".join(lines)


# ── Heading detection via font size ──────────────────────────────────────────

def _detect_heading_level(span_size: float, body_size: float) -> str | None:
    """
    Compare span font size to body font size.
    Returns None if not a heading.
    """
    ratio = span_size / body_size if body_size else 1.0
    if ratio >= 1.6:
        return "h1"
    if ratio >= 1.3:
        return "h2"
    if ratio >= 1.1:
        return "h3"
    return None


def _estimate_body_font_size(page_dict: dict) -> float:
    """Find the most common font size on a page — that's the body text size."""
    sizes: dict[float, int] = {}
    for block in page_dict.get("blocks", []):
        for line in block.get("lines", []):
            for span in line.get("spans", []):
                s = round(span.get("size", 12), 1)
                sizes[s] = sizes.get(s, 0) + len(span.get("text", ""))
    return max(sizes, key=sizes.get) if sizes else 12.0


# ── Header/footer removal ─────────────────────────────────────────────────────

def _is_header_footer(y0: float, y1: float, page_height: float) -> bool:
    margin = page_height * settings.HEADER_FOOTER_MARGIN_PCT
    return y0 < margin or y1 > (page_height - margin)


# ── Main parser ───────────────────────────────────────────────────────────────

def parse_pdf(
    file_bytes: bytes,
    doc_name: str,
    doc_url: str,
    domain: str,
    blob_path: str,
) -> list[RawChunk]:
    """
    Full native PDF parsing pipeline.
    Returns list[RawChunk] (parents + children).
    """
    ingested_at = datetime.now(timezone.utc).isoformat()
    chunks: list[RawChunk] = []

    # Open with both libraries
    fitz_doc    = pymupdf.open(stream=file_bytes, filetype="pdf")
    plumber_doc = pdfplumber.open(file_bytes.__class__(file_bytes)
                                  if not hasattr(file_bytes, 'read')
                                  else file_bytes)

    # Use BytesIO for pdfplumber
    import io
    plumber_doc = pdfplumber.open(io.BytesIO(file_bytes))

    # Extract document title from metadata or first heading
    doc_title = fitz_doc.metadata.get("title", "").strip() or doc_name.replace(".pdf", "")

    current_heading    = ""
    current_subheading = ""
    current_parent_id  = str(uuid4())
    current_parent_content: list[str] = []
    current_parent_page = 1

    def _flush_parent():
        nonlocal current_parent_id, current_parent_content
        if not current_parent_content:
            return
        chunks.append(RawChunk(
            chunk_id           = current_parent_id,
            parent_id          = "",
            chunk_type         = ChunkType.HEADING if current_heading else ChunkType.PARAGRAPH,
            domain             = domain,
            doc_name           = doc_name,
            source             = doc_name,
            doc_url            = doc_url,
            file_type          = "pdf",
            blob_path          = blob_path,
            ingested_at        = ingested_at,
            page_number        = current_parent_page,
            title              = doc_title,
            section_heading    = current_heading,
            section_subheading = current_subheading,
            content            = "\n\n".join(current_parent_content),
        ))
        current_parent_id      = str(uuid4())
        current_parent_content = []

    for page_num in range(len(fitz_doc)):
        fitz_page    = fitz_doc[page_num]
        plumber_page = plumber_doc.pages[page_num]
        page_height  = fitz_page.rect.height
        display_page = page_num + 1

        # Get tables from pdfplumber FIRST so we can skip those regions in text
        tables      = plumber_page.extract_tables() or []
        table_bboxes = [t.bbox for t in plumber_page.find_tables()] if tables else []

        # Extract structured text blocks from pymupdf
        page_dict  = fitz_page.get_text("dict", flags=pymupdf.TEXT_PRESERVE_WHITESPACE)
        body_size  = _estimate_body_font_size(page_dict)

        # Accumulate text spans by block, skipping header/footer zones
        page_paragraphs: list[dict] = []  # {text, heading_level, y0}

        for block in page_dict.get("blocks", []):
            if block.get("type") != 0:  # type 0 = text
                continue

            b_y0 = block["bbox"][1]
            b_y1 = block["bbox"][3]

            if _is_header_footer(b_y0, b_y1, page_height):
                continue

            # Check if this block overlaps a table bbox — skip if so
            in_table = any(
                not (b_y1 < tb[1] or b_y0 > tb[3])
                for tb in table_bboxes
            )
            if in_table:
                continue

            # Collect spans
            block_text = ""
            block_heading = None
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    t = span.get("text", "").strip()
                    if not t:
                        continue
                    span_size = span.get("size", body_size)
                    lvl       = _detect_heading_level(span_size, body_size)
                    if lvl and not block_heading:
                        block_heading = lvl
                    block_text += t + " "

            block_text = block_text.strip()
            if block_text:
                page_paragraphs.append({
                    "text":    block_text,
                    "heading": block_heading,
                    "y0":      b_y0,
                })

        # LLM clean all paragraph text together for this page (one call per page)
        if page_paragraphs:
            combined_raw = "\n\n".join(p["text"] for p in page_paragraphs)
            combined_clean = _llm_clean_page(combined_raw, display_page)
            # Re-split by paragraph count (best effort)
            clean_parts = [s.strip() for s in combined_clean.split("\n\n") if s.strip()]
            # Align cleaned parts back to paragraphs
            for i, para in enumerate(page_paragraphs):
                para["text"] = clean_parts[i] if i < len(clean_parts) else para["text"]

        # Process paragraphs
        for para in page_paragraphs:
            text    = para["text"]
            heading = para["heading"]

            if heading in ("h1", "h2"):
                _flush_parent()
                current_heading    = text
                current_subheading = ""
                current_parent_page = display_page

                # First page heading may be the doc title
                if display_page == 1 and not current_heading:
                    doc_title = text

                chunks.append(RawChunk(
                    chunk_id           = str(uuid4()),
                    parent_id          = current_parent_id,
                    chunk_type         = ChunkType.HEADING,
                    domain             = domain,
                    doc_name           = doc_name,
                    source             = doc_name,
                    doc_url            = doc_url,
                    file_type          = "pdf",
                    blob_path          = blob_path,
                    ingested_at        = ingested_at,
                    page_number        = display_page,
                    title              = doc_title,
                    section_heading    = current_heading,
                    section_subheading = current_subheading,
                    content            = text,
                ))
                current_parent_content.append(text)

            elif heading == "h3":
                current_subheading = text
                current_parent_content.append(text)

            else:
                current_parent_content.append(text)
                chunks.append(RawChunk(
                    chunk_id           = str(uuid4()),
                    parent_id          = current_parent_id,
                    chunk_type         = ChunkType.PARAGRAPH,
                    domain             = domain,
                    doc_name           = doc_name,
                    source             = doc_name,
                    doc_url            = doc_url,
                    file_type          = "pdf",
                    blob_path          = blob_path,
                    ingested_at        = ingested_at,
                    page_number        = display_page,
                    title              = doc_title,
                    section_heading    = current_heading,
                    section_subheading = current_subheading,
                    content            = text,
                ))

        # Process tables from pdfplumber
        for table_data in tables:
            if not table_data:
                continue
            tbl_md     = _pdfplumber_table_to_markdown(table_data)
            if not tbl_md:
                continue
            nl_summary = _llm_serialise_table(tbl_md, current_heading)
            if not nl_summary:
                continue

            chunks.append(RawChunk(
                chunk_id           = str(uuid4()),
                parent_id          = current_parent_id,
                chunk_type         = ChunkType.TABLE,
                domain             = domain,
                doc_name           = doc_name,
                source             = doc_name,
                doc_url            = doc_url,
                file_type          = "pdf",
                blob_path          = blob_path,
                ingested_at        = ingested_at,
                page_number        = display_page,
                title              = doc_title,
                section_heading    = current_heading,
                section_subheading = current_subheading,
                content            = nl_summary,
                table_raw          = tbl_md,
            ))
            current_parent_content.append(nl_summary)

    _flush_parent()

    fitz_doc.close()
    plumber_doc.close()

    logger.info("PDF parsed: %s → %d chunks", doc_name, len(chunks))
    return chunks
