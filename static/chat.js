// Chat widget. Logged-in users get SAVED chats (the latest one reopens, "New chat", "History").
// Visitors who are not logged in can still chat, but nothing is saved.
const panel = document.getElementById("chat-panel");
const log = document.getElementById("log");
const form = document.getElementById("form");
const input = document.getElementById("text");
const send = document.getElementById("send");
const historyBox = document.getElementById("history");
const historyList = document.getElementById("history-list");
const historyBtn = document.getElementById("chat-history-btn");

const LOGGED_IN = panel.dataset.loggedIn === "true";  // set by base.html
const WELCOME = log.innerHTML;                        // the greeting that is already in the page

let conversationId = null;  // which saved chat is open (null = a brand new chat)
let startNew = false;       // tells the server to forget a visitor's old unsaved chat
let loaded = false;         // have we loaded the saved chats yet?

// ---------------------------------------------------------------- open / close
async function openChat() {
  panel.hidden = false;
  if (!loaded) {
    loaded = true;
    if (LOGGED_IN) await loadLatest();  // show the previous chat when a user comes back
  }
  input.focus();
}
document.getElementById("chat-toggle").onclick = () => (panel.hidden ? openChat() : (panel.hidden = true));
document.getElementById("chat-close").onclick = () => (panel.hidden = true);

// ---------------------------------------------------------------- messages
function addMessage(text, cls) {
  const div = document.createElement("div");
  div.className = "msg " + cls;
  const safe = text.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
  div.innerHTML = safe.replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>");
  log.appendChild(div);
  log.scrollTop = log.scrollHeight;
}

async function sendMessage(text) {
  addMessage(text, "me");
  input.disabled = send.disabled = true;
  // Show three animated dots while we wait for Gemini (styled by .typing in style.css)
  const typing = document.createElement("div");
  typing.className = "msg bot typing";
  typing.innerHTML = "<i></i><i></i><i></i>";
  log.appendChild(typing);
  log.scrollTop = log.scrollHeight;
  try {
    const res = await fetch("/chat", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, conversation_id: conversationId, new_chat: startNew }),
    });
    const data = await res.json();
    if (data.reply) {
      startNew = false;
      if (data.conversation_id) conversationId = data.conversation_id;  // the server saved this chat
    }
    addMessage(data.reply || data.error || "No reply.", data.reply ? "bot" : "err");
  } catch (e) {
    addMessage("Cannot reach the server. Is it still running?", "err");
  } finally {
    typing.remove();  // runs on success AND on error, so the dots never get stuck
  }
  input.disabled = send.disabled = false;
  input.focus();
}

// ---------------------------------------------------------------- saved chats
function showHistory(on) {
  historyBox.hidden = !on;
  log.hidden = form.hidden = on;  // the history list takes the place of the messages
  historyBtn.setAttribute("aria-expanded", String(on));
  if (on && LOGGED_IN) refreshHistory();
}
historyBtn.onclick = () => showHistory(historyBox.hidden);

function newChat() {
  conversationId = null;
  startNew = true;
  log.innerHTML = WELCOME;
  showHistory(false);
  input.focus();
}
document.getElementById("chat-new").onclick = newChat;

async function openConversation(id) {
  const res = await fetch("/api/chats/" + id);
  if (!res.ok) return;
  const data = await res.json();
  conversationId = id;
  startNew = false;
  log.innerHTML = "";
  for (const m of data.messages) addMessage(m.content, m.role === "user" ? "me" : "bot");
  showHistory(false);
}

async function loadLatest() {
  const res = await fetch("/api/chats");
  if (!res.ok) return;
  const chats = await res.json();
  if (chats.length) await openConversation(chats[0].id);  // the list comes newest first
}

async function deleteConversation(id) {
  if (!confirm("Delete this chat? This cannot be undone.")) return;
  await fetch("/api/chats/" + id, { method: "DELETE" });
  if (id === conversationId) newChat();
  showHistory(true);
}

function formatDate(iso) {
  return new Date(iso).toLocaleString([], { day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
}

async function refreshHistory() {
  const res = await fetch("/api/chats");
  if (!res.ok) return;
  const chats = await res.json();
  historyList.innerHTML = "";
  if (!chats.length) {
    historyList.innerHTML = '<p class="h-empty">No saved chats yet. Your conversations will appear here.</p>';
    return;
  }
  for (const c of chats) {
    const row = document.createElement("div");
    row.className = "h-item" + (c.id === conversationId ? " current" : "");
    const open = document.createElement("button");
    open.type = "button";
    open.className = "h-open";
    const title = document.createElement("span");
    title.textContent = c.title;  // textContent (not innerHTML) so a title can never inject HTML
    const when = document.createElement("small");
    when.textContent = formatDate(c.updated_at);
    open.append(title, when);
    open.onclick = () => openConversation(c.id);
    const del = document.createElement("button");
    del.type = "button";
    del.className = "h-del";
    del.setAttribute("aria-label", "Delete chat: " + c.title);
    del.textContent = "×";
    del.onclick = () => deleteConversation(c.id);
    row.append(open, del);
    historyList.appendChild(row);
  }
}

// Other buttons on the site call askBot("...") to open the chat and send a question (as a NEW chat).
window.askBot = (text) => { loaded = true; panel.hidden = false; newChat(); sendMessage(text); };

form.addEventListener("submit", (e) => {
  e.preventDefault();
  const text = input.value.trim();
  if (!text) return;
  input.value = "";
  sendMessage(text);
});
