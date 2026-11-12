const $ = (sel) => document.querySelector(sel);

let agents = [];
let currentId = window.DEFAULT_AGENT || "";
let busy = false;

function apiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  const base = (window.REELS_API_BASE || "").replace(/\/$/, "");
  return `${base}${p}`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

/** Minimal formatting for agent replies: **bold**, `code`, bullet lines, line breaks. */
function formatText(text) {
  return escapeHtml(text)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/`([^`]+)`/g, "<code>$1</code>")
    .replace(/^\s*[-*]\s+/gm, "• ")
    .replace(/\n/g, "<br>");
}

async function api(path, options = {}) {
  const token = localStorage.getItem("agentsToken") || "";
  const headers = { ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(apiUrl(path), { ...options, headers });
  if (res.status === 401) {
    const t = window.prompt("Nhập AGENTS_TOKEN để dùng Agent Workspace:");
    if (t) {
      localStorage.setItem("agentsToken", t.trim());
      return api(path, options);
    }
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    throw new Error(typeof d === "string" ? d : res.statusText);
  }
  return data;
}

function currentAgent() {
  return agents.find((a) => a.id === currentId);
}

function renderAgentList() {
  const ul = $("#agent-list");
  ul.innerHTML = "";
  agents.forEach((a) => {
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = `agent-item${a.id === currentId ? " active" : ""}`;
    btn.innerHTML = `
      <span class="agent-dot ${a.status.ok ? "ok" : "warn"}" title="${escapeHtml(a.status.detail || "")}"></span>
      <span class="agent-meta">
        <strong>${escapeHtml(a.name)}</strong>
        <small>${escapeHtml(a.role)}</small>
      </span>`;
    btn.addEventListener("click", () => selectAgent(a.id));
    li.appendChild(btn);
    ul.appendChild(li);
  });
}

function renderArtifacts(list) {
  if (!list || !list.length) return "";
  const parts = list.map((a) => {
    const url = escapeHtml(apiUrl(a.url));
    const label = escapeHtml(a.label || a.filename || "");
    if (a.type === "image") {
      return `<a class="artifact artifact-image" href="${url}" target="_blank" rel="noopener" title="${escapeHtml(a.prompt || label)}">
        <img src="${url}" alt="${label}" loading="lazy" /><span>${label}</span></a>`;
    }
    if (a.type === "video") {
      return `<div class="artifact artifact-video"><video src="${url}" controls preload="metadata"></video>
        <a href="${url}" download>${label}</a></div>`;
    }
    const href = a.type === "link" ? escapeHtml(a.url) : url;
    return `<a class="artifact artifact-link" href="${href}" target="_blank" rel="noopener" ${a.type === "file" ? "download" : ""}>${label}</a>`;
  });
  return `<div class="artifacts">${parts.join("")}</div>`;
}

function renderTools(tools) {
  if (!tools || !tools.length) return "";
  const rows = tools
    .map(
      (t) =>
        `<li><code>${escapeHtml(t.name)}</code> <span class="tool-args">${escapeHtml(
          JSON.stringify(t.args)
        )}</span>${
          t.result !== null && t.result !== undefined
            ? `<pre>${escapeHtml(typeof t.result === "string" ? t.result : JSON.stringify(t.result, null, 1))}</pre>`
            : ""
        }</li>`
    )
    .join("");
  return `<details class="tool-log"><summary>${tools.length} lần dùng tool</summary><ul>${rows}</ul></details>`;
}

function messageHtml(m) {
  const origin =
    m.origin && m.origin !== "user" ? `<span class="msg-origin">Giao từ ${escapeHtml(m.origin)}</span>` : "";
  const cls = m.role === "user" ? "msg msg-user" : `msg msg-agent${m.error ? " msg-error" : ""}`;
  const time = m.ts ? new Date(m.ts).toLocaleTimeString("vi-VN", { hour: "2-digit", minute: "2-digit" }) : "";
  return `<div class="${cls}">
    <div class="msg-head">${origin}<span class="msg-time">${time}</span></div>
    <div class="msg-body">${formatText(m.text || "")}</div>
    ${renderArtifacts(m.artifacts)}
    ${renderTools(m.tools)}
  </div>`;
}

function renderMessages(messages) {
  const box = $("#messages");
  if (!messages.length) {
    const a = currentAgent();
    box.innerHTML = `<p class="empty">Chưa có hội thoại với <strong>${escapeHtml(a ? a.name : "")}</strong>. Chọn một gợi ý bên dưới hoặc nhập yêu cầu.</p>`;
  } else {
    box.innerHTML = messages.map(messageHtml).join("");
  }
  box.scrollTop = box.scrollHeight;
}

function renderSuggestions() {
  const a = currentAgent();
  const wrap = $("#suggestions");
  wrap.innerHTML = "";
  (a?.suggestions || []).forEach((s) => {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "chip";
    b.textContent = s;
    b.addEventListener("click", () => {
      $("#chat-input").value = s;
      $("#chat-input").focus();
    });
    wrap.appendChild(b);
  });
}

async function loadThread() {
  const data = await api(`/api/agents/${encodeURIComponent(currentId)}/thread`);
  renderMessages(data.messages || []);
}

async function selectAgent(id) {
  currentId = id;
  history.replaceState(null, "", `#${id}`);
  const a = currentAgent();
  $("#chat-title").textContent = a ? a.name : id;
  $("#chat-role").textContent = a ? a.role : "";
  const st = $("#chat-status");
  st.textContent = a && !a.status.ok ? a.status.detail : "";
  st.className = `agent-status${a && !a.status.ok ? " warn" : ""}`;
  renderAgentList();
  renderSuggestions();
  await loadThread();
}

async function send(text) {
  if (busy || !text.trim()) return;
  busy = true;
  $("#btn-send").disabled = true;
  const box = $("#messages");
  box.querySelector(".empty")?.remove();
  box.insertAdjacentHTML("beforeend", messageHtml({ role: "user", text, ts: new Date().toISOString() }));
  box.insertAdjacentHTML(
    "beforeend",
    `<div class="msg msg-agent msg-pending"><div class="msg-body">Đang xử lý… (agent có thể gọi tool hoặc agent khác, mất 10–60 giây)</div></div>`
  );
  box.scrollTop = box.scrollHeight;
  try {
    await api(`/api/agents/${encodeURIComponent(currentId)}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
    });
  } catch (err) {
    box.querySelector(".msg-pending")?.remove();
    box.insertAdjacentHTML(
      "beforeend",
      messageHtml({ role: "model", text: `Lỗi: ${err.message || err}`, error: true })
    );
    busy = false;
    $("#btn-send").disabled = false;
    return;
  }
  busy = false;
  $("#btn-send").disabled = false;
  await loadThread();
}

$("#chat-form").addEventListener("submit", (e) => {
  e.preventDefault();
  const input = $("#chat-input");
  const text = input.value;
  input.value = "";
  send(text);
});

$("#chat-input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    $("#chat-form").requestSubmit();
  }
});

$("#btn-clear").addEventListener("click", async () => {
  const a = currentAgent();
  if (!confirm(`Xóa toàn bộ hội thoại với ${a ? a.name : currentId}?`)) return;
  await api(`/api/agents/${encodeURIComponent(currentId)}/thread`, { method: "DELETE" });
  await loadThread();
});

(async function init() {
  try {
    const data = await api("/api/agents");
    agents = data.agents || [];
    const fromHash = location.hash.slice(1);
    currentId = agents.some((a) => a.id === fromHash) ? fromHash : data.default || agents[0]?.id;
    await selectAgent(currentId);
  } catch (err) {
    $("#messages").innerHTML = `<p class="empty">Không tải được danh sách agent: ${escapeHtml(err.message || err)}</p>`;
  }
})();
