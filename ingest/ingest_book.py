#!/usr/bin/env python3
"""
ingest_book.py — PDF → books/<book_id>/ artifacts pipeline.

Usage:
    python ingest/ingest_book.py path/to/book.pdf [--book-id slug] [--title "Book Title"]

Produces:
    books/<book_id>/source.pdf
    books/<book_id>/pages.json
    books/<book_id>/metadata.json
    books/<book_id>/chunks.sqlite
"""

import argparse
import json
import os
import re
import shutil
import sqlite3
import struct
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import fitz  # PyMuPDF
import sqlite_vec
import voyageai
from dotenv import load_dotenv

load_dotenv()

VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")
EMBEDDING_MODEL = "voyage-3"

# Chunking params
CHUNK_TOKENS = 500
OVERLAP_TOKENS = 50
# Rough chars-per-token approximation (good enough for chunking; we're not doing exact tokenization)
CHARS_PER_TOKEN = 4


def slugify(text: str) -> str:
    text = text.lower()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")


def extract_pages(doc: fitz.Document) -> list[dict]:
    """Extract per-page text. Page numbers are 1-indexed."""
    pages = []
    for i, page in enumerate(doc):
        text = page.get_text()
        pages.append({"page_number": i + 1, "text": text})
    return pages


def chunk_pages(pages: list[dict]) -> list[dict]:
    """
    Chunk page text into ~CHUNK_TOKENS windows with ~OVERLAP_TOKENS overlap.
    Chunks are NOT page-aligned — they flow across page boundaries.
    Each chunk records the page number where it *starts*.
    """
    chunk_chars = CHUNK_TOKENS * CHARS_PER_TOKEN
    overlap_chars = OVERLAP_TOKENS * CHARS_PER_TOKEN

    # Build a flat list of (page_number, text) so we can stream across pages
    segments: list[tuple[int, str]] = []
    for p in pages:
        if p["text"].strip():
            segments.append((p["page_number"], p["text"]))

    # Concatenate everything into one big string, tracking page boundaries
    full_text = ""
    page_starts: list[tuple[int, int]] = []  # (char_offset, page_number)
    for page_number, text in segments:
        page_starts.append((len(full_text), page_number))
        full_text += text

    def page_at_offset(offset: int) -> int:
        """Return the page number that covers the given character offset."""
        result = 1
        for char_offset, page_number in page_starts:
            if char_offset <= offset:
                result = page_number
            else:
                break
        return result

    chunks = []
    start = 0
    chunk_index = 0
    while start < len(full_text):
        end = min(start + chunk_chars, len(full_text))
        chunk_text = full_text[start:end]
        chunks.append({
            "chunk_index": chunk_index,
            "page_number": page_at_offset(start),
            "text": chunk_text,
        })
        chunk_index += 1
        next_start = start + chunk_chars - overlap_chars
        if next_start <= start:
            break
        start = next_start

    return chunks


def embed_chunks(chunks: list[dict]) -> list[list[float]]:
    """Embed all chunk texts with Voyage AI. Returns list of embedding vectors."""
    if not VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY not set")

    client = voyageai.Client(api_key=VOYAGE_API_KEY)
    texts = [c["text"] for c in chunks]

    # Voyage's embed endpoint accepts up to 128 inputs per call
    batch_size = 128
    all_embeddings = []
    for i in range(0, len(texts), batch_size):
        batch = texts[i : i + batch_size]
        print(f"  Embedding chunks {i + 1}–{i + len(batch)} of {len(texts)}...")
        for attempt in range(5):
            try:
                result = client.embed(batch, model=EMBEDDING_MODEL, input_type="document")
                all_embeddings.extend(result.embeddings)
                break
            except voyageai.error.RateLimitError:
                wait = 20 * (attempt + 1)
                print(f"  Rate limited — waiting {wait}s...")
                time.sleep(wait)
        else:
            raise RuntimeError("Exceeded retry limit on Voyage rate limit")

    return all_embeddings


def build_sqlite(book_dir: Path, chunks: list[dict], embeddings: list[list[float]]) -> None:
    """Create chunks.sqlite with a metadata table, a sqlite-vec virtual table, and an FTS5 BM25 index."""
    db_path = book_dir / "chunks.sqlite"
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    vec_dim = len(embeddings[0])

    conn.executescript(f"""
        CREATE TABLE chunks (
            id          INTEGER PRIMARY KEY,
            chunk_index INTEGER NOT NULL,
            page_number INTEGER NOT NULL,
            text        TEXT NOT NULL
        );

        CREATE VIRTUAL TABLE chunk_vecs USING vec0(
            id          INTEGER PRIMARY KEY,
            embedding   FLOAT[{vec_dim}]
        );

        CREATE VIRTUAL TABLE chunks_fts USING fts5(
            text,
            content='chunks',
            content_rowid='id'
        );
    """)

    conn.executemany(
        "INSERT INTO chunks (id, chunk_index, page_number, text) VALUES (?, ?, ?, ?)",
        [(i, c["chunk_index"], c["page_number"], c["text"]) for i, c in enumerate(chunks)],
    )

    conn.executemany(
        "INSERT INTO chunk_vecs (id, embedding) VALUES (?, ?)",
        [
            (i, struct.pack(f"{vec_dim}f", *emb))
            for i, emb in enumerate(embeddings)
        ],
    )

    conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('rebuild')")

    conn.commit()
    conn.close()


def ingest(pdf_path: str, book_id: str | None, title: str | None) -> None:
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.exists():
        print(f"Error: {pdf_path} does not exist", file=sys.stderr)
        sys.exit(1)

    if not book_id:
        book_id = slugify(pdf_path.stem)
    if not book_id:
        book_id = "book"

    books_root = Path(__file__).parent.parent / "books"
    book_dir = books_root / book_id
    book_dir.mkdir(parents=True, exist_ok=True)

    print(f"Ingesting '{pdf_path.name}' → books/{book_id}/")

    # --- Open PDF ---
    doc = fitz.open(str(pdf_path))
    total_pages = doc.page_count

    # --- Resolve title ---
    if not title:
        meta = doc.metadata
        title = meta.get("title") or pdf_path.stem
    print(f"  Title: {title}  |  Pages: {total_pages}")

    # --- Extract pages ---
    print("  Extracting page text...")
    pages = extract_pages(doc)
    pages_data = {"pages": pages, "total_pages": total_pages}
    with open(book_dir / "pages.json", "w", encoding="utf-8") as f:
        json.dump(pages_data, f, ensure_ascii=False)
    print(f"  Wrote pages.json ({total_pages} pages)")

    # --- Extract TOC ---
    toc_raw = doc.get_toc()  # list of [level, title, page]
    toc = [{"level": entry[0], "title": entry[1], "page": entry[2]} for entry in toc_raw] or None

    # --- Write metadata ---
    metadata = {
        "book_id": book_id,
        "title": title,
        "author": doc.metadata.get("author", ""),
        "total_pages": total_pages,
        "ingest_date": datetime.now(timezone.utc).isoformat(),
        "embedding_model": EMBEDDING_MODEL,
        "toc": toc,
    }
    with open(book_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, ensure_ascii=False, indent=2)
    print(f"  Wrote metadata.json  (TOC: {'yes, ' + str(len(toc)) + ' entries' if toc else 'none'})")

    # --- Chunk ---
    print("  Chunking text...")
    chunks = chunk_pages(pages)
    print(f"  {len(chunks)} chunks produced")

    # --- Embed ---
    print("  Embedding with Voyage AI...")
    embeddings = embed_chunks(chunks)

    # --- Build SQLite vector index ---
    print("  Building chunks.sqlite...")
    build_sqlite(book_dir, chunks, embeddings)
    print("  Wrote chunks.sqlite")

    # --- Copy source PDF ---
    dest_pdf = book_dir / "source.pdf"
    shutil.copy2(str(pdf_path), str(dest_pdf))
    print(f"  Copied source.pdf")

    doc.close()
    print(f"\nDone. Artifacts in books/{book_id}/")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Ingest a PDF textbook into the Claude Reader format.")
    parser.add_argument("pdf", help="Path to the PDF file")
    parser.add_argument("--book-id", dest="book_id", default=None, help="Slug identifier for the book (derived from filename if omitted)")
    parser.add_argument("--title", default=None, help="Book title (extracted from PDF metadata if omitted)")
    args = parser.parse_args()

    ingest(args.pdf, args.book_id, args.title)
