# Claude Reader

A local app for reading textbooks with an AI professor sitting next to you. PDF on the left, chat on the right, current page automatically in context.

See `CLAUDE.md` for the full build spec.

## Quick start (once built)

```bash
# 1. install deps
pip install -r requirements.txt

# 2. set up env vars
cp .env.example .env
# edit .env and add your ANTHROPIC_API_KEY and VOYAGE_API_KEY

# 3. ingest a textbook
python ingest/ingest_book.py path/to/textbook.pdf --title "Textbook Title"

# 4. run the app
python backend/app.py
# open http://localhost:5000
```
