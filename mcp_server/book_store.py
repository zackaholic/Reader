"""
book_store.py — helpers for loading book artifacts and querying the vector index.
This is the only place that touches pages.json, metadata.json, and chunks.sqlite.
"""

import json
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
    """Embed query and return top-k matching chunks from the vector index."""
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

    rows = conn.execute(
        """
        SELECT c.page_number, c.text, v.distance
        FROM chunk_vecs v
        JOIN chunks c ON c.id = v.id
        WHERE v.embedding MATCH ?
          AND k = ?
        ORDER BY v.distance
        """,
        (packed, k),
    ).fetchall()
    conn.close()

    return [
        {
            "page_number": row[0],
            "snippet": row[1][:300],
            "score": row[2],
        }
        for row in rows
    ]
