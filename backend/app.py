"""
app.py — Flask backend for Claude Reader.
"""

import json
import os
from pathlib import Path

import anthropic
from dotenv import load_dotenv
from flask import Flask, Response, jsonify, request, send_file, stream_with_context

import session_state as ss
from mcp_client import call_tool, list_tools
from prompt_builder import build_system_prompt

load_dotenv()

# ── Constants ────────────────────────────────────────────────────────────────

CLAUDE_MODEL = "claude-sonnet-4-5"   # change here to swap models
MAX_TOKENS = 4096
BOOKS_ROOT = Path(__file__).parent.parent / "books"
FRONTEND_DIR = Path(__file__).parent.parent / "frontend"

# ── App setup ────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=str(FRONTEND_DIR), static_url_path="")

client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

# In-memory conversation history (single-user personal tool — global is fine for v1)
conversation_history: list[dict] = []

# ── Helpers ──────────────────────────────────────────────────────────────────

def _book_metadata(book_id: str) -> dict | None:
    meta_path = BOOKS_ROOT / book_id / "metadata.json"
    if not meta_path.exists():
        return None
    with open(meta_path, encoding="utf-8") as f:
        return json.load(f)


def _list_books() -> list[dict]:
    books = []
    if BOOKS_ROOT.exists():
        for book_dir in sorted(BOOKS_ROOT.iterdir()):
            if book_dir.is_dir():
                meta = _book_metadata(book_dir.name)
                if meta:
                    books.append(meta)
    return books


# ── Routes ───────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return send_file(FRONTEND_DIR / "index.html")


@app.route("/pdf/<book_id>")
def serve_pdf(book_id):
    pdf_path = BOOKS_ROOT / book_id / "source.pdf"
    if not pdf_path.exists():
        return jsonify({"error": "not found"}), 404
    return send_file(pdf_path, mimetype="application/pdf")


@app.route("/books")
def list_books():
    return jsonify(_list_books())


@app.route("/session")
def get_session():
    state = ss.load()
    meta = _book_metadata(state["current_book_id"]) if state["current_book_id"] else None
    return jsonify({"session": state, "book_metadata": meta})


@app.route("/session/page", methods=["POST"])
def set_page():
    data = request.get_json()
    page_number = data.get("page_number")
    if not isinstance(page_number, int) or page_number < 1:
        return jsonify({"error": "invalid page_number"}), 400
    state = ss.load()
    ss.update(state, current_page=page_number)
    return "", 204


@app.route("/session/book", methods=["POST"])
def set_book():
    global conversation_history
    data = request.get_json()
    book_id = data.get("book_id")
    if not book_id:
        return jsonify({"error": "book_id required"}), 400
    meta = _book_metadata(book_id)
    if not meta:
        return jsonify({"error": "book not found"}), 404
    state = ss.load()
    ss.update(state, current_book_id=book_id, current_page=1)
    conversation_history = []
    return jsonify(meta)


@app.route("/chat/reset", methods=["POST"])
def reset_chat():
    global conversation_history
    conversation_history = []
    return "", 204


@app.route("/chat", methods=["POST"])
def chat():
    global conversation_history
    data = request.get_json()
    user_message = data.get("message", "").strip()
    if not user_message:
        return jsonify({"error": "message required"}), 400

    state = ss.load()
    if not state.get("current_book_id"):
        return jsonify({"error": "no book selected"}), 400

    system_prompt = build_system_prompt(state)
    conversation_history.append({"role": "user", "content": user_message})

    tools = list_tools()

    def generate():
        global conversation_history

        # Working copy of history for this turn (may grow with tool calls)
        messages = list(conversation_history)
        final_assistant_text = ""

        while True:
            # Collect all content blocks from the stream
            response_blocks = []
            streamed_text = ""
            stop_reason = None

            with client.messages.stream(
                model=CLAUDE_MODEL,
                max_tokens=MAX_TOKENS,
                system=system_prompt,
                messages=messages,
                tools=tools,
            ) as stream:
                for event in stream:
                    event_type = type(event).__name__

                    if event_type == "RawContentBlockStartEvent":
                        block = event.content_block
                        response_blocks.append(block)
                        # Notify UI of tool use
                        if hasattr(block, "type") and block.type == "tool_use":
                            yield f"data: {json.dumps({'type': 'tool_use', 'tool': block.name})}\n\n"

                    elif event_type == "RawContentBlockDeltaEvent":
                        delta = event.delta
                        if hasattr(delta, "text"):
                            streamed_text += delta.text
                            final_assistant_text += delta.text
                            yield f"data: {json.dumps({'type': 'text', 'text': delta.text})}\n\n"
                        elif hasattr(delta, "partial_json"):
                            # Accumulate partial JSON for tool_use input
                            if response_blocks:
                                last = response_blocks[-1]
                                if hasattr(last, "_partial_json"):
                                    last._partial_json += delta.partial_json
                                else:
                                    last._partial_json = delta.partial_json

                    elif event_type == "RawMessageDeltaEvent":
                        if hasattr(event.delta, "stop_reason"):
                            stop_reason = event.delta.stop_reason

            # Reconstruct full content for message history
            full_content = []
            for block in response_blocks:
                if hasattr(block, "type"):
                    if block.type == "text":
                        full_content.append({"type": "text", "text": streamed_text})
                    elif block.type == "tool_use":
                        partial = getattr(block, "_partial_json", "{}")
                        try:
                            tool_input = json.loads(partial) if partial else {}
                        except json.JSONDecodeError:
                            tool_input = {}
                        full_content.append({
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": tool_input,
                        })

            messages.append({"role": "assistant", "content": full_content})

            if stop_reason != "tool_use":
                break

            # Execute all tool calls and add results
            tool_results = []
            for block in full_content:
                if block["type"] == "tool_use":
                    tool_result = call_tool(block["name"], block["input"])
                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block["id"],
                        "content": tool_result,
                    })

            messages.append({"role": "user", "content": tool_results})

        # Persist the final turn to conversation history
        conversation_history.append({"role": "assistant", "content": final_assistant_text})
        yield f"data: {json.dumps({'type': 'done'})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


# ── Entry point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(debug=False, port=5001, threaded=True)
