# Claude Reader

A local single-page web app for reading textbooks with an AI "professor" sitting next to you. The PDF and chat live side-by-side, the current page flows automatically into Claude's context, and questions can be asked about the page you're looking at without copy-pasting or re-uploading anything.

This document is the build spec. **Read it end-to-end before starting.** Sections marked **SEAM** describe extension points that must be preserved even though they look unused in v1 — they exist so future features land as additions rather than rewrites. Do not optimize these away.

---

## Goal of v1

Open the app → a pre-ingested textbook is shown in a PDF viewer on the left → chat sidebar on the right → type a question → Claude answers in the context of the current page. That loop, working well, is the entire v1 success criterion. Everything else is deferred (see "Explicitly out of scope" below).

The user is a working engineer building this for personal use. Optimize for clarity, hackability, and easy local dev — not for production hardening, multi-user support, auth, or deployment.

---

## High-level architecture

```
┌──────────────────────────────────────────────────────────┐
│                   Browser (one tab)                       │
│  ┌─────────────────────┐    ┌─────────────────────────┐  │
│  │  PDF.js viewer      │    │  Chat sidebar           │  │
│  │  (left pane)        │    │  (right pane)           │  │
│  │  emits page change  │    │  streams from backend   │  │
│  └──────────┬──────────┘    └────────────┬────────────┘  │
└─────────────┼──────────────────────────────┼──────────────┘
              │ POST /session/page           │ POST /chat (SSE)
              ▼                              ▼
┌──────────────────────────────────────────────────────────┐
│              Flask backend (localhost)                    │
│  - owns session state (current book, current page, ...)  │
│  - calls Anthropic SDK with MCP server attached          │
│  - streams responses back to frontend via SSE            │
└──────────────────────┬───────────────────────────────────┘
                       │ session state file (JSON on disk)
                       ▼
┌──────────────────────────────────────────────────────────┐
│              Local MCP server (stdio or HTTP)             │
│  Tools: get_current_page, get_pages_around,              │
│         search_book, get_table_of_contents               │
│  Reads session state to know which book / which page.    │
│  ONLY component that touches book content.               │
└──────────────────────┬───────────────────────────────────┘
                       ▼
              books/<book_id>/
                source.pdf
                pages.json       (page-indexed text)
                chunks.sqlite    (vector index)
                metadata.json    (title, author, TOC)
```

The ingest pipeline (`ingest/ingest_book.py`) is run separately, once per book, and produces the artifacts in `books/<book_id>/`. The runtime never writes to those files.

---

## The four critical seams

These are the design decisions that make future features cheap. **Preserve them even where they look like over-engineering for v1.**

### SEAM 1: The MCP server is the only thing that touches book content
The Flask app and the frontend never read PDFs, never query the vector index, never parse pages. They only call MCP tools. This means:
- Future tools (`get_definition`, `get_figure`, `save_note`, `get_index_entry`, `get_current_page_image` for vision mode, etc.) are added by editing one file.
- The same MCP server is portable to other frontends (Claude desktop, CLI) later.
- **Do not** add a "shortcut" where Flask reads `pages.json` directly even if it would be one fewer hop. That defeats the seam.

### SEAM 2: Session state is a structured object with named-but-empty future fields
Session state is owned by the Flask app and persisted to a JSON file on disk that the MCP server reads. The schema in v1 must include these fields even though only the first two are populated:

```python
{
  "current_book_id": "<string>",
  "current_page": <int>,
  "student_context": "",      # future: learning style, comfort level
  "persona": "",               # future: professor personality
  "session_notes": ""          # future: running scratchpad
}
```

The empty fields exist so the persona/memory upgrade is "fill in the blanks" rather than a refactor. **Do not remove them.** Add a comment in the code explaining why they're there.

### SEAM 3: The system prompt is templated from blocks, not hardcoded
Build the system prompt by concatenating named blocks, even if some are empty in v1:

```
[base instructions block]              # always present
[book context block]                   # current book title, page N of M, anchor snippet
[student context block]                # empty in v1
[persona block]                        # empty in v1
[session notes block]                  # empty in v1
[tool usage hints block]               # always present
```

Implement this as a function `build_system_prompt(session_state) -> str` that assembles the blocks from the session state object. Empty blocks render as nothing. **Do not** hardcode the v1 prompt as a single string.

### SEAM 4: Ingest is fully decoupled from runtime
`ingest/ingest_book.py` is a standalone script. The runtime app only *reads* the artifacts it produces. This means future ingest improvements (better chunking, OCR for scanned PDFs, figure extraction, page rasterization for vision mode) are changes to one script with no runtime impact.

---

## Component specs

### Ingest pipeline (`ingest/ingest_book.py`)

**Input:** path to a PDF, plus optional `--book-id` and `--title` flags. If not given, derive book_id from the filename slug.

**Process:**
1. Open the PDF with PyMuPDF (`fitz`).
2. Extract per-page text. Store as `pages.json`: `{"pages": [{"page_number": 1, "text": "..."}, ...], "total_pages": N}`. Page numbers are 1-indexed and match the PDF's logical page numbers as displayed by PDF.js.
3. Extract metadata: title (from PDF metadata or `--title` flag), author, total pages, ingest date, embedding model name. Store as `metadata.json`.
4. Extract the TOC if the PDF has bookmarks (`doc.get_toc()`). Store inside `metadata.json` under `toc`. If no TOC, store `null` — do not fail.
5. Build a vector index for search:
   - Chunk strategy: ~500 token windows with ~50 token overlap, NOT page-aligned. Each chunk carries `{page_number, chunk_index, text}` metadata. If a chunk straddles pages, use the page where the chunk *starts*.
   - Embed with Voyage AI (`voyage-3` or current default). Read the API key from `VOYAGE_API_KEY` env var.
   - Store in `chunks.sqlite` using `sqlite-vec`. Schema: one table for chunk metadata, one virtual table for the vectors.
6. Copy the source PDF to `books/<book_id>/source.pdf`.

**Output:** `books/<book_id>/{source.pdf, pages.json, metadata.json, chunks.sqlite}`

**CLI:** `python ingest/ingest_book.py path/to/book.pdf [--book-id slug] [--title "Book Title"]`

The script should be idempotent — re-running it on the same book_id wipes and rebuilds.

### MCP server (`mcp_server/server.py`)

Use the Python MCP SDK (`mcp` package). Run it as a stdio server that the Flask backend launches as a subprocess and connects to via the Anthropic SDK's MCP support.

**Session state access:** read from `data/session_state.json` on every tool call. Cheap, simple, no IPC needed. Do not cache — always re-read so the MCP server sees the latest page.

**Tools:**

1. `get_current_page() -> dict`
   - Reads session state → current_book_id, current_page.
   - Loads `books/<book_id>/pages.json`, returns the entry for current_page.
   - Returns: `{book_title, page_number, total_pages, text}`
   - No arguments. Book/page identity comes from session state. (SEAM 1: do not add a book_id parameter — when multi-book Q&A is added later, that's when the parameter gets introduced as a deliberate change.)

2. `get_pages_around(before: int = 2, after: int = 2) -> dict`
   - Same as above but returns concatenated text for `[current_page - before, current_page + after]`, clamped to valid range.
   - Returns: `{book_title, start_page, end_page, text}`

3. `search_book(query: str, k: int = 5) -> list[dict]`
   - Embeds the query, runs vector search against `chunks.sqlite` for the current book.
   - Returns list of `{page_number, snippet, score}` where snippet is the chunk text (truncated to ~300 chars for the snippet view).

4. `get_table_of_contents() -> dict`
   - Returns the TOC stored in `metadata.json`. If null, returns `{"toc": null, "note": "This book has no extracted table of contents."}`.
   - **Future extension noted in code comment:** this tool will later also search the back-of-book index, which is more useful than the TOC for "where does he discuss X" questions. Leave a TODO.

### Flask backend (`backend/app.py`)

**Routes:**

- `GET /` → serves the single-page frontend (`frontend/index.html`).
- `GET /pdf/<book_id>` → serves the source PDF for the viewer (so PDF.js can load it).
- `POST /session/page` → body `{page_number: int}`. Updates session state. Returns 204.
- `POST /session/book` → body `{book_id: string}`. Switches the current book, resets current_page to 1, clears conversation history. Returns the book's metadata.
- `GET /session` → returns full session state and current book metadata. Used on page load to initialize.
- `POST /chat` → body `{message: string}`. Streams the assistant's response back as SSE. Maintains conversation history server-side (a list in memory, keyed by some session id or just a single global since it's a personal tool — global is fine for v1).
- `POST /chat/reset` → clears the conversation history. For the "new conversation" button.
- `GET /books` → lists available books from the `books/` directory by reading each `metadata.json`. For the book picker dropdown.

**Chat handling:**
1. Build the system prompt via `build_system_prompt(session_state)` (SEAM 3).
2. Append the user message to in-memory conversation history.
3. Call the Anthropic SDK with:
   - `model="claude-sonnet-4-5"` (or current Sonnet — make this a constant at the top of the file so it's easy to change)
   - `max_tokens=4096`
   - `system=<built prompt>`
   - `messages=<conversation history>`
   - `mcp_servers=[<local mcp server config>]`
   - `stream=True`
4. Stream text deltas back to the frontend via SSE. Also stream tool-use events as separate SSE events so the UI can show "searching the book..." indicators (nice-to-have, not required for v1).
5. When the stream completes, append the assistant message to history.

**The book context block** in the system prompt should always include: book title, "page N of M", and the first ~150 characters of the current page as an anchor snippet. This is the lightweight header that lets the model know where you are without a tool call. The model can call `get_current_page` for the full text when needed.

**Conversation lifecycle:** per-session, persists for the life of the Flask process. No automatic summarization in v1. The "new conversation" button is the only reset mechanism. Switching books also resets.

**Read the Anthropic API key from the `ANTHROPIC_API_KEY` env var.**

### Frontend (`frontend/index.html` + `frontend/app.js` + `frontend/style.css`)

Single HTML page. No build step. No React. Vanilla JS is fine and preferred — this keeps the project hackable. PDF.js loaded from a CDN or vendored locally.

**Layout:** CSS grid or flexbox, two columns. Left column ~60-65% width: PDF viewer. Right column: chat sidebar with a scrolling message list and a textarea + send button at the bottom. Header bar across the top with: book picker dropdown, current page indicator, "new conversation" button.

**PDF viewer:**
- Use PDF.js's default viewer if it's easy to embed, otherwise build a minimal one: render one page at a time to a canvas, prev/next buttons, page number input, keyboard shortcuts (←/→ for page nav).
- On every page change, POST to `/session/page` with the new page number. Debounce by ~200ms so rapid arrow-key navigation doesn't spam the backend.

**Chat sidebar:**
- Message list, user messages right-aligned, assistant messages left-aligned. Markdown rendering for assistant messages (use `marked` from a CDN). Code blocks should render with monospace font; full syntax highlighting is not required for v1.
- Textarea with Enter-to-send, Shift+Enter for newline.
- On send: POST to `/chat` and consume the SSE stream, appending text deltas to the in-progress assistant message bubble as they arrive.
- Optional: show a small "🔍 searching the book" or "📖 reading page" indicator when tool-use SSE events arrive.

**On page load:** call `GET /session` and `GET /books` to initialize state and populate the book dropdown.

---

## Project layout

```
Claude-Reader/
├── CLAUDE.md                    # this file
├── README.md                    # short user-facing readme
├── .env.example                 # ANTHROPIC_API_KEY, VOYAGE_API_KEY
├── requirements.txt             # python deps
├── backend/
│   ├── app.py                   # Flask app
│   ├── session_state.py         # session state object + persistence
│   ├── prompt_builder.py        # build_system_prompt (SEAM 3)
│   └── mcp_client.py            # launches & connects to local MCP server
├── mcp_server/
│   ├── server.py                # MCP server with the four tools
│   └── book_store.py            # helpers to load pages.json, query chunks.sqlite
├── ingest/
│   └── ingest_book.py           # PDF → artifacts pipeline
├── frontend/
│   ├── index.html
│   ├── app.js
│   └── style.css
├── books/                       # populated by ingest, gitignored
│   └── <book_id>/
│       ├── source.pdf
│       ├── pages.json
│       ├── metadata.json
│       └── chunks.sqlite
└── data/
    └── session_state.json       # written by Flask, read by MCP server
```

---

## Dependencies (Python)

- `flask` — backend
- `anthropic` — Claude SDK with MCP support
- `mcp` — MCP server SDK
- `pymupdf` — PDF text/TOC extraction
- `voyageai` — embeddings
- `sqlite-vec` — vector storage
- `python-dotenv` — env var loading

Pin nothing in v1; let pip resolve. Add a `requirements.txt` with the package names only.

---

## Build order (suggested)

1. **Ingest pipeline first.** Get a real textbook ingested into `books/<book_id>/` so you have something to test against. Verify `pages.json` looks right and `search_book`-style queries against the SQLite index return sensible chunks.
2. **MCP server next.** Build it standalone, test the tools by calling them directly with a hardcoded session state file.
3. **Flask backend without the frontend.** Wire up the Anthropic SDK + MCP server, build the prompt template, expose `/chat` and test with `curl`.
4. **Frontend last.** PDF.js viewer + chat sidebar, wire up the page-change events.

After each step, the user should be able to run something and see it work. Don't try to build the whole stack before testing any of it.

---

## Explicitly out of scope for v1

These are real future features. The architecture above is set up to absorb them cleanly, but **none of them go in v1.** If a design choice would make any of these *harder* to add later, flag it and ask.

- Persona / professor personality
- Student profile (learning style, comfort level)
- Memory / session notes that persist across sessions
- Highlights, notes, bookmarks
- Multi-book library management UI beyond the simple dropdown
- Voice input
- Page-as-image vision mode for figures and equations
- Index-based search in `get_table_of_contents`
- Conversation summarization when context fills
- Model switching (Sonnet for everything in v1)
- Authentication, multi-user support, deployment
- Cross-book queries

---

## Notes for whoever is building this

- The user is Zack — a systems engineer comfortable with Flask, Python, hardware, and MCP servers (he's built one before). Default to being concise in code comments; he doesn't need basics explained.
- When in doubt about a design choice, prefer the option that keeps the four seams clean over the option that's slightly less code in v1.
- If something in this spec is ambiguous or contradictory, ask before guessing. Don't paper over it.
- The project is for personal use. No tests required for v1 unless they'd genuinely speed up development of a specific component (the ingest pipeline might benefit from a smoke test).
