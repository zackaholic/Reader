"""
server.py — MCP server exposing four tools for reading book content.

Runs as a streamable-HTTP server on MCP_PORT (default 5002) so the Flask
backend can connect via the Anthropic SDK's URL-based MCP support.

Session state is read from data/session_state.json on every tool call —
no caching, so the server always sees the latest page/book.

Tools:
  get_current_page       — text of the current page
  get_pages_around       — text of pages surrounding the current page
  search_book            — vector search across the current book
  get_table_of_contents  — TOC from metadata.json
"""

import json
import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from book_store import load_metadata, load_page, load_pages_range, search_chunks

SESSION_STATE_PATH = Path(__file__).parent.parent / "data" / "session_state.json"
MCP_PORT = int(os.environ.get("MCP_PORT", "5002"))

mcp = FastMCP("claude-reader", port=MCP_PORT)


def _read_session() -> dict:
    with open(SESSION_STATE_PATH, encoding="utf-8") as f:
        return json.load(f)


@mcp.tool()
def get_current_page() -> dict:
    """
    Get the full text of the page the student is currently viewing.

    Returns the book title, page number, total pages, and page text.
    No arguments needed — book and page come from session state.

    Note: when multi-book Q&A is added later, a book_id parameter will be
    introduced here as a deliberate extension, not a refactor.
    """
    session = _read_session()
    book_id = session["current_book_id"]
    page_number = session["current_page"]

    meta = load_metadata(book_id)
    page = load_page(book_id, page_number)

    return {
        "book_title": meta["title"],
        "page_number": page_number,
        "total_pages": meta["total_pages"],
        "text": page["text"] if page else "",
    }


@mcp.tool()
def get_pages_around(before: int = 2, after: int = 2) -> dict:
    """
    Get the text of pages surrounding the current page.

    Args:
        before: number of pages before the current page to include (default 2)
        after:  number of pages after the current page to include (default 2)

    Returns concatenated text for the page range, clamped to book bounds.
    """
    session = _read_session()
    book_id = session["current_book_id"]
    current = session["current_page"]

    meta = load_metadata(book_id)
    start = current - before
    end = current + after

    pages = load_pages_range(book_id, start, end)
    combined_text = "\n\n".join(
        f"[Page {p['page_number']}]\n{p['text']}" for p in pages
    )

    actual_start = pages[0]["page_number"] if pages else current
    actual_end = pages[-1]["page_number"] if pages else current

    return {
        "book_title": meta["title"],
        "start_page": actual_start,
        "end_page": actual_end,
        "text": combined_text,
    }


@mcp.tool()
def search_book(query: str, k: int = 5) -> list[dict]:
    """
    Search the current book for chunks semantically similar to the query.

    Args:
        query: the search query
        k:     number of results to return (default 5)

    Returns a list of {page_number, snippet, score} dicts, ordered by relevance.
    Lower score = more similar (cosine distance).
    """
    session = _read_session()
    book_id = session["current_book_id"]
    return search_chunks(book_id, query, k)


@mcp.tool()
def get_table_of_contents() -> dict:
    """
    Get the table of contents for the current book.

    Returns the TOC as a list of {level, title, page} entries, or null if
    the book has no extracted TOC.

    TODO: future extension — also search the back-of-book index here, which
    is often more useful than the TOC for "where does the author discuss X"
    questions. The index is typically the last few pages of the PDF.
    """
    session = _read_session()
    book_id = session["current_book_id"]
    meta = load_metadata(book_id)
    toc = meta.get("toc")

    if toc is None:
        return {"toc": None, "note": "This book has no extracted table of contents."}
    return {"toc": toc}


if __name__ == "__main__":
    mcp.run(transport="stdio")
