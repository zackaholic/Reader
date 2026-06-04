"""
book_store.py — helpers for loading book artifacts and querying the vector index.
This is the only place that touches pages.json, metadata.json, and chunks.sqlite.
"""

import json
import re
import sqlite3
import struct
from pathlib import Path

import sqlite_vec
import voyageai
from dotenv import load_dotenv
import os

load_dotenv()

VOYAGE_API_KEY = os.environ.get("VOYAGE_API_KEY")
BOOKS_ROOT = Path(__file__).parent.parent / "books"


def _book_dir(book_id: str) -> Path:
    return BOOKS_ROOT / book_id


def load_metadata(book_id: str) -> dict:
    path = _book_dir(book_id) / "metadata.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_page(book_id: str, page_number: int) -> dict | None:
    """Return the page dict for page_number (1-indexed), or None if out of range."""
    path = _book_dir(book_id) / "pages.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data["pages"]
    # pages list is 0-indexed, page_number is 1-indexed
    idx = page_number - 1
    if idx < 0 or idx >= len(pages):
        return None
    return pages[idx]


def load_pages_range(book_id: str, start: int, end: int) -> list[dict]:
    """Return pages from start to end inclusive (1-indexed), clamped to valid range."""
    path = _book_dir(book_id) / "pages.json"
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    pages = data["pages"]
    total = data["total_pages"]
    start = max(1, start)
    end = min(total, end)
    return [pages[i] for i in range(start - 1, end)]


def search_chunks(book_id: str, query: str, k: int = 5) -> list[dict]:
    """
    Hybrid search: vector (Voyage AI) + BM25 (FTS5), merged with Reciprocal Rank Fusion.
    Falls back to vector-only if the FTS5 table is absent (pre-migration index).
    """
    if not VOYAGE_API_KEY:
        raise RuntimeError("VOYAGE_API_KEY not set")

    meta = load_metadata(book_id)
    embedding_model = meta.get("embedding_model", "voyage-3")

    client = voyageai.Client(api_key=VOYAGE_API_KEY)
    result = client.embed([query], model=embedding_model, input_type="query")
    query_vec = result.embeddings[0]

    db_path = _book_dir(book_id) / "chunks.sqlite"
    conn = sqlite3.connect(db_path)
    conn.enable_load_extension(True)
    sqlite_vec.load(conn)
    conn.enable_load_extension(False)

    vec_dim = len(query_vec)
    packed = struct.pack(f"{vec_dim}f", *query_vec)
    fetch_k = k * 3  # over-fetch before RRF merge

    # Vector search
    vec_rows = conn.execute(
        """
        SELECT c.id, c.page_number, c.text, v.distance
        FROM chunk_vecs v
        JOIN chunks c ON c.id = v.id
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (packed, fetch_k),
    ).fetchall()

    # BM25 search via FTS5; silently skipped if table doesn't exist
    bm25_rows = []
    fts_query = " ".join(re.sub(r"[^\w\s]", " ", query).split())
    if fts_query:
        try:
            bm25_rows = conn.execute(
                """
                SELECT c.id, c.page_number, c.text, bm25(chunks_fts) AS score
                FROM chunks_fts
                JOIN chunks c ON c.id = chunks_fts.rowid
                WHERE chunks_fts MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (fts_query, fetch_k),
            ).fetchall()
        except sqlite3.OperationalError:
            pass

    conn.close()

    # Reciprocal Rank Fusion — k=60 is the standard constant
    RRF_K = 60
    scores: dict[int, float] = {}
    chunk_data: dict[int, tuple] = {}

    for rank, (chunk_id, page_number, text, _) in enumerate(vec_rows):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        chunk_data[chunk_id] = (page_number, text)

    for rank, (chunk_id, page_number, text, _) in enumerate(bm25_rows):
        scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (RRF_K + rank + 1)
        chunk_data[chunk_id] = (page_number, text)

    merged = sorted(scores.items(), key=lambda x: x[1], reverse=True)[:k]

    return [
        {
            "page_number": chunk_data[cid][0],
            "snippet": chunk_data[cid][1][:300],
            "score": rrf_score,
        }
        for cid, rrf_score in merged
    ]
