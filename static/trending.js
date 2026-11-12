const $ = (sel) => document.querySelector(sel);

const POLL_MS = 2000;
let lastResult = null;
let pollTimer = null;

function apiUrl(path) {
  const base = (window.REELS_API_BASE || "").replace(/\/$/, "");
  return `${base}${path}`;
}

function escapeHtml(s) {
  return String(s ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

async function api(path, options = {}) {
  const token = localStorage.getItem("agentsToken") || "";
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (token) headers.Authorization = `Bearer ${token}`;
  const res = await fetch(apiUrl(path), { ...options, headers });
  if (res.status === 401) {
    const t = window.prompt("Nhập AGENTS_TOKEN:");
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

const compact = new Intl.NumberFormat("vi-VN", { notation: "compact", maximumFractionDigits: 1 });

function num(v) {
  return v ? compact.format(v) : "–";
}

function age(hours) {
  if (hours == null) return "?";
  if (hours < 1) return `${Math.max(1, Math.round(hours * 60))} phút`;
  if (hours < 48) return `${Math.round(hours)} giờ`;
  return `${Math.round(hours / 24)} ngày`;
}

function when(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("vi-VN", { dateStyle: "short", timeStyle: "short" }) : "";
}

function levelClass(ratio) {
  if (ratio >= 2) return "great";
  if (ratio >= 1) return "good";
  if (ratio >= 0.5) return "avg";
  return "low";
}

function pageName(url) {
  const m = /facebook\.com\/(?:profile\.php\?id=)?([^/?&]+)/.exec(url || "");
  return m ? m[1] : url;
}

/* ---------- progress ---------- */

function showProgress(job) {
  $("#progress").hidden = false;
  const titles = {
    running: "Đang phân tích…",
    done: "Đã phân tích xong",
    blocked: "Facebook chặn giữa chừng — đây là kết quả lấy được",
    error: "Lỗi khi phân tích",
  };
  $("#progress-title").textContent = titles[job.status] || job.status;
  $("#progress-time").textContent = `${job.elapsed_s}s · ${job.pages.length} page`;
  const log = $("#progress-log");
  log.textContent = job.log.join("\n") + (job.error ? `\n${job.error}` : "");
  log.scrollTop = log.scrollHeight;
}

function setBusy(busy) {
  $("#btn-analyze").disabled = busy;
  $("#btn-reload").disabled = busy;
  $("#btn-analyze").textContent = busy ? "Đang chạy…" : "Analyze";
}

async function poll(jobId) {
  clearTimeout(pollTimer);
  try {
    const job = await api(`/api/trending/jobs/${jobId}`);
    showProgress(job);
    if (job.status === "running") {
      pollTimer = setTimeout(() => poll(jobId), POLL_MS);
      return;
    }
    localStorage.removeItem("trendingJob");
    setBusy(false);
    if (job.result) renderResult(job.result);
  } catch (err) {
    localStorage.removeItem("trendingJob");
    setBusy(false);
    $("#progress").hidden = true;
    if (err.message.startsWith("Không có job")) reload();
    else alert(err.message);
  }
}

async function analyze() {
  const links = $("#links").value.trim();
  if (!links) {
    alert("Dán ít nhất một link page Facebook.");
    return;
  }
  setBusy(true);
  try {
    const res = await api("/api/trending/analyze", {
      method: "POST",
      body: JSON.stringify({ links, period_hours: Number($("#period").value), reels: $("#reels").checked }),
    });
    showInvalid(res.invalid);
    localStorage.setItem("trendingJob", res.job_id);
    poll(res.job_id);
  } catch (err) {
    setBusy(false);
    alert(err.message);
  }
}

async function reload() {
  const links = $("#links").value.trim();
  if (!links) return;
  try {
    renderResult(await api("/api/trending/results", {
      method: "POST",
      body: JSON.stringify({ links, period_hours: Number($("#period").value) }),
    }));
  } catch (err) {
    alert(err.message);
  }
}

function showInvalid(invalid) {
  const box = $("#link-warning");
  box.hidden = !(invalid && invalid.length);
  if (!box.hidden) box.textContent = `Bỏ qua link không phải page: ${invalid.join(", ")}`;
}

/* ---------- results ---------- */

function renderResult(result) {
  lastResult = result;
  $("#results").hidden = false;
  const mode = result.last_crawl_mode === "guest" ? "chế độ khách" : result.last_crawl_mode ? "có cookie" : "";
  $("#results-meta").textContent = result.last_crawl_at
    ? `Crawl gần nhất ${when(result.last_crawl_at)}${mode ? ` · ${mode}` : ""}`
    : "Chưa có dữ liệu crawl";

  $("#notes").innerHTML = (result.notes || []).map((n) => `<li>${escapeHtml(n)}</li>`).join("");

  $("#page-summary").innerHTML = result.pages
    .map((p) => {
      let status = `${p.good}/${p.posts} post tốt`;
      if (!p.posts && p.crawled) status = `không có post mới trong khoảng này${p.latest_post_at ? ` (gần nhất ${when(p.latest_post_at)})` : ""}`;
      else if (!p.posts) status = "chưa lấy được post";
      return `
      <div class="trend-page-card${p.posts ? "" : " empty"}">
        <a href="${escapeHtml(p.page_url)}" target="_blank" rel="noopener">${escapeHtml(p.name || pageName(p.page_url))}</a>
        <span>${escapeHtml(status)}</span>
      </div>`;
    })
    .join("");
  renderRows();
}

function sortedItems() {
  const minRatio = Number($("#f-level").value);
  const kind = $("#f-kind").value;
  const sort = $("#f-sort").value;
  const items = lastResult.items.filter(
    (i) => i.perf_ratio >= minRatio && (kind === "all" || i.kind === kind),
  );
  const keys = {
    perf: (i) => i.perf_ratio,
    score: (i) => i.score,
    engagement: (i) => i.engagement,
    new: (i) => i.created_at || 0,
  };
  return items.sort((a, b) => keys[sort](b) - keys[sort](a));
}

function renderRows() {
  if (!lastResult) return;
  const items = sortedItems();
  $("#f-count").textContent = `${items.length}/${lastResult.items.length} post`;
  if (!items.length) {
    $("#rows").innerHTML = `<tr><td colspan="9" class="trend-empty">Không có post nào khớp bộ lọc.</td></tr>`;
    return;
  }
  $("#rows").innerHTML = items
    .map((i, idx) => {
      const level = levelClass(i.perf_ratio);
      const text = i.caption || i.hook || "(không có chữ)";
      const growth = i.delta_window
        ? `+${num(i.delta_window)}${i.rising ? ' <span class="trend-up" title="Tốc độ đang tăng">↑</span>' : ""}`
        : "–";
      return `
        <tr>
          <td class="muted">${idx + 1}</td>
          <td class="trend-post">
            <div class="trend-post-head">
              <span class="trend-kind ${i.kind}">${i.kind === "reel" ? "Reel" : "Post"}</span>
              <strong>${escapeHtml(i.page || "")}</strong>
            </div>
            <a href="${escapeHtml(i.url)}" target="_blank" rel="noopener" class="trend-caption">${escapeHtml(text)}</a>
          </td>
          <td title="${escapeHtml(when(i.created_at))}">${age(i.age_hours)}</td>
          <td class="num">${num(i.reactions)}</td>
          <td class="num">${num(i.comments)}</td>
          <td class="num">${num(i.shares)}</td>
          <td class="num">${num(i.views)}</td>
          <td><span class="trend-badge ${level}" title="Post điển hình của page: ${num(i.page_typical)} tương tác">
            ${escapeHtml(i.perf_label)} ×${i.perf_ratio}</span></td>
          <td class="num" title="${i.estimated ? "Ước lượng (chưa có số liệu đủ 24h trước)" : ""}">${growth}</td>
        </tr>`;
    })
    .join("");
}

/* ---------- init ---------- */

async function init() {
  $("#btn-analyze").addEventListener("click", analyze);
  $("#btn-reload").addEventListener("click", reload);
  ["#f-level", "#f-kind", "#f-sort"].forEach((sel) => $(sel).addEventListener("change", renderRows));
  $("#period").addEventListener("change", () => lastResult && reload());

  try {
    const info = await api("/api/trending/pages");
    if (!$("#links").value.trim()) $("#links").value = info.pages.join("\n");
    $("#mode").textContent = info.mode === "guest" ? "Chế độ khách (không đăng nhập)" : "Dùng cookie tài khoản";
    $("#mode").title = info.mode === "guest"
      ? "Mỗi page thấy ~6 post / reels mới nhất trên feed mỗi lần crawl."
      : "";
    // Logged out, reels come from the feed only; the reels-tab switch has no effect.
    $("#reels").closest("label").hidden = info.mode === "guest";
    const jobId = info.running || localStorage.getItem("trendingJob");
    if (jobId) {
      setBusy(true);
      poll(jobId);
    } else if (info.pages.length) {
      reload();
    }
  } catch (err) {
    $("#mode").textContent = err.message;
  }
}

init();
