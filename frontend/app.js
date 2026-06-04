"use strict";

// ── PDF.js setup ────────────────────────────────────────────────────────────
pdfjsLib.GlobalWorkerOptions.workerSrc =
  "https://unpkg.com/pdfjs-dist@3.11.174/build/pdf.worker.min.js";

// ── State ────────────────────────────────────────────────────────────────────
let pdfDoc = null;
let currentPage = 1;
let totalPages = 0;
let currentBookId = null;
let renderInProgress = false;
let pageDebounceTimer = null;
let sendingMessage = false;

// ── DOM refs ─────────────────────────────────────────────────────────────────
const bookSelect     = document.getElementById("book-select");
const prevBtn        = document.getElementById("prev-page");
const nextBtn        = document.getElementById("next-page");
const pageInput      = document.getElementById("page-input");
const pageTotal      = document.getElementById("page-total");
const pdfPane        = document.getElementById("pdf-pane");
const canvas         = document.getElementById("pdf-canvas");
const placeholder    = document.getElementById("pdf-placeholder");
const messages       = document.getElementById("messages");
const toolIndicator  = document.getElementById("tool-indicator");
const chatInput      = document.getElementById("chat-input");
const sendBtn        = document.getElementById("send-btn");
const newConvoBtn    = document.getElementById("new-conversation");
const chatPane       = document.getElementById("chat-pane");
const chatToggle     = document.getElementById("chat-toggle");
const toggleIcon     = document.getElementById("toggle-icon");

const ctx = canvas.getContext("2d");

// ── Initialisation ───────────────────────────────────────────────────────────
async function init() {
  const [sessionRes, booksRes] = await Promise.all([
    fetch("/session"),
    fetch("/books"),
  ]);
  const { session } = await sessionRes.json();
  const books = await booksRes.json();

  populateBookSelect(books, session.current_book_id);

  if (session.current_book_id) {
    currentBookId = session.current_book_id;
    currentPage = session.current_page || 1;
    await loadPdf(currentBookId, currentPage);
  }
}

// ── Book picker ──────────────────────────────────────────────────────────────
function populateBookSelect(books, activeBookId) {
  bookSelect.innerHTML = "";
  if (books.length === 0) {
    bookSelect.innerHTML = '<option value="">No books ingested yet</option>';
    return;
  }
  if (!activeBookId) {
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = "Select a book…";
    bookSelect.appendChild(placeholder);
  }
  books.forEach(book => {
    const opt = document.createElement("option");
    opt.value = book.book_id;
    opt.textContent = book.title;
    if (book.book_id === activeBookId) opt.selected = true;
    bookSelect.appendChild(opt);
  });
}

bookSelect.addEventListener("change", async () => {
  const bookId = bookSelect.value;
  if (!bookId) return;
  await fetch("/session/book", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ book_id: bookId }),
  });
  currentBookId = bookId;
  currentPage = 1;
  clearMessages();
  await loadPdf(bookId, 1);
});

// ── PDF loading & rendering ──────────────────────────────────────────────────
async function loadPdf(bookId, startPage) {
  placeholder.style.display = "none";
  canvas.style.display = "block";

  pdfDoc = await pdfjsLib.getDocument(`/pdf/${bookId}`).promise;
  totalPages = pdfDoc.numPages;
  pageTotal.textContent = `/ ${totalPages}`;
  pageInput.max = totalPages;

  await renderPage(startPage);
}

async function renderPage(n) {
  if (!pdfDoc) return;
  n = Math.max(1, Math.min(n, totalPages));
  if (renderInProgress) return;
  renderInProgress = true;

  const pageChanged = n !== currentPage;

  try {
    const page = await pdfDoc.getPage(n);
    const dpr = window.devicePixelRatio || 1;
    const cssScale = getScale(page);
    const viewport = page.getViewport({ scale: cssScale * dpr });

    // Canvas backing store is at full device resolution
    canvas.width = viewport.width;
    canvas.height = viewport.height;
    // CSS size stays at logical pixels so layout is unchanged
    canvas.style.width  = (viewport.width  / dpr) + "px";
    canvas.style.height = (viewport.height / dpr) + "px";

    await page.render({ canvasContext: ctx, viewport }).promise;

    currentPage = n;
    pageInput.value = n;
    prevBtn.disabled = n <= 1;
    nextBtn.disabled = n >= totalPages;

    if (pageChanged) pdfPane.scrollTop = 0;
    notifyPageChange(n);
  } finally {
    renderInProgress = false;
  }
}

function getScale(page) {
  const availableWidth = pdfPane.clientWidth - 32; // 16px padding each side
  const viewport = page.getViewport({ scale: 1 });
  return availableWidth / viewport.width;
}

// Debounced page-change notification to backend
function notifyPageChange(n) {
  clearTimeout(pageDebounceTimer);
  pageDebounceTimer = setTimeout(() => {
    fetch("/session/page", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ page_number: n }),
    });
  }, 200);
}

// ── Page navigation ───────────────────────────────────────────────────────────
prevBtn.addEventListener("click", () => renderPage(currentPage - 1));
nextBtn.addEventListener("click", () => renderPage(currentPage + 1));

pageInput.addEventListener("change", () => {
  const n = parseInt(pageInput.value, 10);
  if (!isNaN(n)) renderPage(n);
});

document.addEventListener("keydown", (e) => {
  // Only navigate when not typing in an input/textarea
  if (["INPUT", "TEXTAREA", "SELECT"].includes(e.target.tagName)) return;
  if (e.key === "ArrowLeft")  renderPage(currentPage - 1);
  if (e.key === "ArrowRight") renderPage(currentPage + 1);
});

// ── Chat ─────────────────────────────────────────────────────────────────────
function appendMessage(role, html) {
  const div = document.createElement("div");
  div.className = `message ${role}`;
  if (role === "assistant") {
    div.innerHTML = html;
  } else {
    div.textContent = html; // user text is plain
  }
  messages.appendChild(div);
  messages.scrollTop = messages.scrollHeight;
  return div;
}

function clearMessages() {
  messages.innerHTML = "";
}

async function sendMessage() {
  const text = chatInput.value.trim();
  if (!text || sendingMessage) return;
  if (!currentBookId) {
    alert("Please select a book first.");
    return;
  }

  sendingMessage = true;
  sendBtn.disabled = true;
  chatInput.value = "";

  appendMessage("user", text);

  // Placeholder for streaming assistant response
  const assistantDiv = document.createElement("div");
  assistantDiv.className = "message assistant";
  messages.appendChild(assistantDiv);

  let fullText = "";

  try {
    const response = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });

    if (!response.ok) {
      assistantDiv.textContent = "Error: server returned " + response.status;
      return;
    }

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;

      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop(); // keep incomplete last line

      for (const line of lines) {
        if (!line.startsWith("data: ")) continue;
        const payload = JSON.parse(line.slice(6));

        if (payload.type === "text") {
          fullText += payload.text;
          assistantDiv.innerHTML = marked.parse(fullText);
          messages.scrollTop = messages.scrollHeight;

        } else if (payload.type === "tool_use") {
          const labels = {
            get_current_page: "📖 reading current page…",
            get_pages_around: "📖 reading nearby pages…",
            search_book:      "🔍 searching the book…",
            get_table_of_contents: "📋 checking table of contents…",
          };
          toolIndicator.textContent = labels[payload.tool] || `🔧 calling ${payload.tool}…`;
          toolIndicator.classList.remove("hidden");

        } else if (payload.type === "done") {
          toolIndicator.classList.add("hidden");
        }
      }
    }
  } catch (err) {
    assistantDiv.textContent = "Error: " + err.message;
  } finally {
    sendingMessage = false;
    sendBtn.disabled = false;
    toolIndicator.classList.add("hidden");
    messages.scrollTop = messages.scrollHeight;
  }
}

sendBtn.addEventListener("click", sendMessage);

chatInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendMessage();
  }
});

newConvoBtn.addEventListener("click", async () => {
  await fetch("/chat/reset", { method: "POST" });
  clearMessages();
});

chatToggle.addEventListener("click", () => {
  const collapsed = chatPane.classList.toggle("collapsed");
  toggleIcon.textContent = collapsed ? "‹" : "›";
  // Re-render after transition so PDF uses the gained/lost width
  setTimeout(() => { if (pdfDoc) renderPage(currentPage); }, 220);
});

// ── Boot ─────────────────────────────────────────────────────────────────────
init();
