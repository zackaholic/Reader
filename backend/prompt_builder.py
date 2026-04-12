"""
prompt_builder.py — assembles the system prompt from named blocks (SEAM 3).

Build by concatenating blocks rather than a single hardcoded string so that
future blocks (persona, student context, session notes) are "fill in the blanks"
rather than a refactor. Empty blocks render as nothing.
"""

from pathlib import Path
import json


def _base_instructions_block() -> str:
    return """You are an expert professor and tutor helping a student read and understand a textbook. \
You have access to tools that let you read the book's content — use them to give accurate, \
grounded answers. When answering questions about the material, cite the page number you're \
drawing from. Be concise but thorough. If the student asks something not covered in the book, \
say so rather than guessing."""


def _book_context_block(state: dict) -> str:
    book_id = state.get("current_book_id")
    if not book_id:
        return ""

    books_root = Path(__file__).parent.parent / "books"
    meta_path = books_root / book_id / "metadata.json"
    pages_path = books_root / book_id / "pages.json"

    if not meta_path.exists():
        return ""

    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)

    title = meta.get("title", "Unknown")
    total_pages = meta.get("total_pages", "?")
    current_page = state.get("current_page", 1)

    # Grab a short anchor snippet from the current page
    anchor = ""
    if pages_path.exists():
        with open(pages_path, encoding="utf-8") as f:
            pages_data = json.load(f)
        pages = pages_data.get("pages", [])
        idx = current_page - 1
        if 0 <= idx < len(pages):
            anchor = pages[idx]["text"][:150].strip().replace("\n", " ")

    lines = [
        f"The student is currently reading: {title}",
        f"They are on page {current_page} of {total_pages}.",
    ]
    if anchor:
        lines.append(f'Page {current_page} begins: "{anchor}..."')

    return "\n".join(lines)


def _student_context_block(state: dict) -> str:
    # Empty in v1 — future: learning style, comfort level
    return state.get("student_context", "")


def _persona_block(state: dict) -> str:
    # Empty in v1 — future: professor personality
    return state.get("persona", "")


def _session_notes_block(state: dict) -> str:
    # Empty in v1 — future: running scratchpad
    return state.get("session_notes", "")


def _tool_usage_hints_block() -> str:
    return """When you need to read the current page in full, call get_current_page. \
To see surrounding pages for context, call get_pages_around. \
To find relevant passages elsewhere in the book, call search_book. \
To understand book structure or find a topic by chapter, call get_table_of_contents."""


def build_system_prompt(state: dict) -> str:
    blocks = [
        _base_instructions_block(),
        _book_context_block(state),
        _student_context_block(state),
        _persona_block(state),
        _session_notes_block(state),
        _tool_usage_hints_block(),
    ]
    return "\n\n".join(b for b in blocks if b.strip())
