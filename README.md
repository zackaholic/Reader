# Claude Reader

A local single-page app for reading textbooks with an AI professor sitting next to you. PDF on the left, streaming chat on the right. The current page flows automatically into Claude's context — no copy-pasting, no re-uploading.

See `CLAUDE.md` for the full architecture and design spec.

---

## How it works

- **Ingest pipeline** (`ingest/ingest_book.py`) — run once per book. Extracts text, builds a vector index (Voyage AI embeddings), stores artifacts in `books/<book_id>/`.
- **MCP server** (`mcp_server/server.py`) — exposes 4 tools: `get_current_page`, `get_pages_around`, `search_book`, `get_table_of_contents`. Reads session state from `data/session_state.json`.
- **Flask backend** (`backend/app.py`) — serves the frontend, manages session state, runs the Claude tool-calling loop via the Anthropic SDK, streams responses via SSE.
- **Frontend** (`frontend/`) — vanilla JS, PDF.js viewer, markdown chat sidebar.

---

## Setup (first time)

### Requirements
- Python 3.11+
- API keys: `ANTHROPIC_API_KEY` and `VOYAGE_API_KEY`

### Steps

```bash
git clone https://github.com/zackaholic/claude-reader.git
cd claude-reader

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
# edit .env and fill in both API keys
```

### Ingest a book

```bash
python ingest/ingest_book.py path/to/book.pdf --title "Book Title"
# artifacts land in books/<slug>/
```

### Run

```bash
python backend/app.py
# open http://localhost:5001
```

---

## Mini PC deployment (Linux)

This section is written for a fresh Claude instance doing the migration. The app is working and tested on macOS. The task is to get it running on the mini PC and keep it running as a system service.

### 1. Clone and set up

```bash
git clone https://github.com/zackaholic/claude-reader.git
cd claude-reader
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in ANTHROPIC_API_KEY and VOYAGE_API_KEY
```

### 2. Transfer books

The `books/` directory is gitignored (large files). Copy it from the source machine:

```bash
# on the source Mac:
rsync -av books/ user@minipc-ip:~/claude-reader/books/
```

Or re-ingest from the original PDFs if they're available on the mini PC.

### 3. Verify the stack works before setting up the service

```bash
source .venv/bin/activate
python backend/app.py
# curl http://localhost:5001/books — should list ingested books
# curl http://localhost:5001/session — should return session state
# test a chat message end-to-end before proceeding
```

### 4. Set up a systemd service

Create `/etc/systemd/system/claude-reader.service`:

```ini
[Unit]
Description=Claude Reader
After=network.target

[Service]
Type=simple
User=<your-user>
WorkingDirectory=/home/<your-user>/claude-reader
ExecStart=/home/<your-user>/claude-reader/.venv/bin/python backend/app.py
EnvironmentFile=/home/<your-user>/claude-reader/.env
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```bash
sudo systemctl daemon-reload
sudo systemctl enable claude-reader
sudo systemctl start claude-reader
sudo systemctl status claude-reader
```

### 5. Bind to all interfaces

For LAN access from other machines, update the last line of `backend/app.py`:

```python
app.run(debug=False, host="0.0.0.0", port=5001, threaded=True)
```

Then access the app at `http://<minipc-ip>:5001` from any machine on the network.

### 6. Notes for the Claude instance doing this

- The MCP server is launched as a subprocess by Flask on startup — no separate service needed.
- On first start there's a ~3-5s pause while the MCP subprocess initializes. This is expected.
- The `.env` file is the only thing not in the repo — it must be created manually on the mini PC.
- `data/session_state.json` is also gitignored. Flask creates it automatically on first run if it doesn't exist — but double-check `backend/session_state.py` handles the missing-file case (it does: `load()` returns defaults if the file doesn't exist).
- If something isn't working, check `CLAUDE.md` for the full architecture before digging into code.
