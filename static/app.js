let library = [];
let rows = [];
let selectedRowId = null;
let rowSeq = 1;
let TEXT_SAFETY_MAX = 50000;
let INNER_PAD_V = 20;
let ESTIMATE_DENSE_CHARS = 21000;
let CAPTION_FONT_PX = 38;
let AI_ENABLED = false;
/** Output files of this session (videos + images), keyed by filename. */
let outputs = new Map();

const $ = (sel) => document.querySelector(sel);

function errorDetail(data, res) {
  const d = data && data.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d)) return d.map((x) => x.msg || JSON.stringify(x)).join("; ");
  return (data && data.message) || res.statusText || "Lỗi không xác định";
}

async function postJson(path, payload) {
  const res = await fetch(apiUrl(path), {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(errorDetail(data, res));
  return data;
}

function selectedAspects() {
  return [...document.querySelectorAll("#aspects input[type=checkbox]:checked")].map(
    (el) => el.value
  );
}

function aiStyle() {
  return ($("#ai-style")?.value || "").trim();
}

function applyAiState() {
  const notice = $("#ai-notice");
  if (notice) {
    notice.hidden = AI_ENABLED;
    notice.textContent = AI_ENABLED
      ? ""
      : "AI tạo ảnh đang tắt: server chưa có GEMINI_API_KEY. Vẫn tạo video và xuất ảnh từ ảnh import được.";
  }
  document.querySelectorAll("[data-ai]").forEach((el) => {
    el.disabled = !AI_ENABLED;
    el.title = AI_ENABLED ? el.dataset.aiTitle || "" : "Cần GEMINI_API_KEY trên server";
  });
}

function addOutputs(items, kind) {
  items.forEach((it) => {
    const label =
      kind === "video"
        ? `Video ${String(it.no).padStart(2, "0")} (${it.filename})`
        : `Ảnh ${String(it.no).padStart(2, "0")} · ${it.aspect} (${it.filename})`;
    outputs.set(it.filename, { ...it, kind, label });
  });
  renderDownloads();
}

function renderDownloads() {
  const dl = $("#downloads");
  if (!dl) return;
  dl.innerHTML = "";
  const aspectOrder = { "9:16": 0, "4:5": 1, "1:1": 2 };
  const list = [...outputs.values()].sort(
    (a, b) =>
      a.no - b.no ||
      b.kind.localeCompare(a.kind) ||
      (aspectOrder[a.aspect] ?? 9) - (aspectOrder[b.aspect] ?? 9)
  );
  list.forEach((it) => {
    const li = document.createElement("li");
    const a = document.createElement("a");
    a.href = apiUrl(it.url);
    a.download = it.filename;
    a.textContent = `Tải ${it.label}`;
    li.appendChild(a);
    dl.appendChild(li);
  });
  const zipBtn = $("#btn-zip");
  if (zipBtn) zipBtn.disabled = list.length === 0;
}

/** API path: same origin locally, or REELS_API_BASE on GitHub Pages. */
function apiUrl(path) {
  const p = path.startsWith("/") ? path : `/${path}`;
  const base = (window.REELS_API_BASE || "").replace(/\/$/, "");
  return `${base}${p}`;
}

async function loadConfig() {
  try {
    const r = await fetch(apiUrl("/api/config"));
    const c = await r.json();
    TEXT_SAFETY_MAX =
      c.text_safety_max_chars ?? c.text_hard_max_chars ?? TEXT_SAFETY_MAX;
    INNER_PAD_V = c.caption_inner_pad_vertical_px ?? INNER_PAD_V;
    ESTIMATE_DENSE_CHARS = c.estimated_chars_dense_layout ?? ESTIMATE_DENSE_CHARS;
    CAPTION_FONT_PX = c.caption_font_size_px ?? CAPTION_FONT_PX;
    AI_ENABLED = Boolean(c.ai_enabled);
    const note = $("#text-limit-note");
    if (note && c.text_note) note.textContent = c.text_note;
    const bgSel = $("#text-bg");
    if (bgSel && Array.isArray(c.text_bg_options)) {
      bgSel.innerHTML = "";
      c.text_bg_options.forEach((opt) => {
        const o = document.createElement("option");
        o.value = opt.id;
        o.textContent = opt.label;
        bgSel.appendChild(o);
      });
      if (c.text_bg_default) bgSel.value = c.text_bg_default;
    }
  } catch {
    const note = $("#text-limit-note");
    if (note) {
      note.textContent =
        "Nội dung trong ô được giữ nguyên khi dán; chữ trên video thu nhỏ để vừa khung. Giới hạn rất cao chỉ để bảo vệ server.";
    }
  }
}

function totalCharCount() {
  return rows.reduce((sum, r) => sum + (r.text?.length ?? 0), 0);
}

function flashPasteNotice(rowNo, countInCell) {
  const el = $("#paste-notice");
  if (!el) return;
  el.textContent = `Đã dán: ${countInCell} ký tự trong ô của dòng STT ${rowNo}. Nội dung được giữ nguyên trong ô (không cắt khi dán).`;
  el.hidden = false;
  clearTimeout(flashPasteNotice._t);
  flashPasteNotice._t = setTimeout(() => {
    el.hidden = true;
  }, 5000);
}

function updateCharHints() {
  const totalEl = $("#total-char-hint");
  const total = totalCharCount();
  if (totalEl) {
    totalEl.textContent = `Tổng ký tự đã nhập (tất cả các dòng): ${total}`;
  }

  const el = $("#char-hint");
  if (!el) return;
  const row = selectedRowId ? rows.find((r) => r.id === selectedRowId) : null;
  if (!row) {
    el.textContent =
      `Chọn một dòng. Video: ${CAPTION_FONT_PX}px, căn trái, nền đen từng dòng. Enter 2× = đoạn mới. ~${ESTIMATE_DENSE_CHARS} ký tự ≈ đầy chiều cao.`;
    return;
  }
  const n = row.text.length;
  let warn = "";
  if (n > TEXT_SAFETY_MAX) {
    warn = ` Cảnh báo: vượt ~${TEXT_SAFETY_MAX} ký tự — có thể lỗi khi tạo video hoặc bị cắt ở server.`;
  } else if (n > ESTIMATE_DENSE_CHARS) {
    warn = ` Trên ~${ESTIMATE_DENSE_CHARS} ký tự có thể tràn dọc (cỡ chữ cố định ~${CAPTION_FONT_PX}px) — phần cuối có thể bị … trên video.`;
  }
  el.textContent =
    `STT ${row.no}: ${n} ký tự — ${CAPTION_FONT_PX}px, căn trái, giữ đoạn/xuống dòng.${warn}`;
}

async function removeLibraryItem(id) {
  if (
    !confirm(
      "Remove this image from the library? Any row using it will lose the image assignment."
    )
  ) {
    return;
  }
  setStatus("Removing…");
  try {
    const res = await fetch(apiUrl(`/api/library/${encodeURIComponent(id)}`), {
      method: "DELETE",
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || res.statusText);
    rows.forEach((r) => {
      if (r.imageId === id) r.imageId = "";
    });
    await refreshLibrary();
    setStatus("Image removed from library.");
  } catch (err) {
    setStatus(`Remove failed: ${err.message || err}`);
  }
}

function nextRowNo() {
  const used = new Set(rows.map((r) => r.no));
  let n = 1;
  while (used.has(n)) n += 1;
  return n;
}

function rowId() {
  return `r_${rowSeq++}`;
}

function syncSelectedVisual() {
  document.querySelectorAll(".grid tbody tr").forEach((tr) => {
    tr.classList.toggle("row-selected", tr.dataset.rowId === selectedRowId);
  });
}

function renderLibrary() {
  const root = $("#library");
  root.innerHTML = "";
  library.forEach((item) => {
    const card = document.createElement("div");
    card.className = "lib-card";

    const rm = document.createElement("button");
    rm.type = "button";
    rm.className = "lib-remove";
    rm.title = "Remove from library";
    rm.textContent = "×";
    rm.addEventListener("click", (ev) => {
      ev.stopPropagation();
      removeLibraryItem(item.id);
    });

    const pick = document.createElement("button");
    pick.type = "button";
    pick.className = "lib-pick";
    pick.dataset.id = item.id;
    const img = document.createElement("img");
    img.src = `${apiUrl(item.url)}?t=${Date.now()}`;
    img.alt = item.original_name || item.id;
    img.loading = "lazy";
    pick.appendChild(img);
    pick.addEventListener("click", () => {
      document.querySelectorAll(".lib-pick").forEach((el) => el.classList.remove("active"));
      pick.classList.add("active");
      const row = assignToSelectedRow(item.id);
      setImgStatus(`Đã gán ảnh vào dòng STT ${row.no}.`);
    });

    const regen = document.createElement("button");
    regen.type = "button";
    regen.className = "lib-regen";
    regen.textContent = "↻";
    regen.dataset.ai = "1";
    regen.dataset.aiTitle = "AI regenerate ảnh này";
    regen.addEventListener("click", (ev) => {
      ev.stopPropagation();
      regenerateLibraryItem(item, regen);
    });

    card.appendChild(rm);
    card.appendChild(regen);
    if (item.source && item.source !== "upload") {
      const badge = document.createElement("span");
      badge.className = "lib-badge";
      badge.textContent = "AI";
      if (item.prompt) badge.title = item.prompt;
      card.appendChild(badge);
    }
    card.appendChild(pick);
    root.appendChild(card);
  });
  applyAiState();
}

async function regenerateLibraryItem(item, btn) {
  const instruction = window.prompt(
    "Hướng dẫn cho AI (để trống = làm đẹp hơn, giữ chủ thể):",
    ""
  );
  if (instruction === null) return;
  const aspects = selectedAspects();
  btn.disabled = true;
  btn.classList.add("busy");
  setImgStatus(`AI đang regenerate ảnh (${aspects.join(", ") || "9:16"})…`);
  try {
    const data = await postJson("/api/ai/regenerate", {
      image_id: item.id,
      instruction,
      aspects,
    });
    await refreshLibrary();
    const row = selectedRowId ? rows.find((r) => r.id === selectedRowId) : null;
    if (row && row.imageId === item.id) {
      row.imageId = data.item.id;
      renderRows();
    }
    const warn = data.warnings?.length ? ` Lưu ý: ${data.warnings.join("; ")}` : "";
    setImgStatus(`Đã tạo ảnh AI mới từ ảnh gốc.${warn}`);
  } catch (err) {
    setImgStatus(`Regenerate thất bại: ${err.message || err}`);
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
  }
}

function selectedRow() {
  return selectedRowId ? rows.find((r) => r.id === selectedRowId) || null : null;
}

let summaryRowId = null;

/** Step 1 textarea edits the selected row's text (summary = caption on video). */
function syncSummaryFromRow() {
  const row = selectedRow();
  const ta = $("#summary");
  const rowChanged = summaryRowId !== (row ? row.id : null);
  if (ta && (rowChanged || document.activeElement !== ta)) ta.value = row ? row.text : "";
  summaryRowId = row ? row.id : null;
  const label = $("#summary-row-label");
  if (label) label.textContent = row ? `Đang soạn dòng STT ${row.no}` : "";
}

function selectRow(id) {
  if (selectedRowId === id) return;
  selectedRowId = id;
  syncSelectedVisual();
  updateCharHints();
  syncSummaryFromRow();
}

/** Assign a new image to the selected row (creating one if needed); returns the row. */
function assignToSelectedRow(imageId) {
  const row = selectedRow() || addRow();
  row.imageId = imageId;
  renderRows();
  return row;
}

async function generateFromSummary() {
  const row = selectedRow();
  const summary = (row?.text || "").trim();
  if (!summary) {
    setImgStatus("Nhập nội dung tóm tắt ở bước 1 trước khi gen ảnh AI.");
    $("#summary")?.focus();
    return;
  }
  const aspects = selectedAspects();
  const count = parseInt($("#ai-count")?.value || "1", 10) || 1;
  const btn = $("#btn-ai-gen");
  btn.disabled = true;
  btn.textContent = "Đang gen…";
  setImgStatus(
    `AI đang đọc tóm tắt và tạo ${count} ảnh (${aspects.join(", ") || "9:16"})… thường mất 10–30 giây.`
  );
  try {
    const data = await postJson("/api/ai/from-summary", {
      summary,
      style: aiStyle(),
      aspects,
      count,
      label: `AI · ${summary.slice(0, 40)}`,
    });
    const preview = $("#prompt-preview");
    if (preview && data.prompt) {
      preview.hidden = false;
      preview.textContent = `Prompt AI đã dùng: ${data.prompt}`;
    }
    await refreshLibrary();
    let assigned = "";
    if (data.items?.[0]) {
      row.imageId = data.items[0].id;
      renderRows();
      assigned = ` Ảnh đầu tiên đã gán vào dòng STT ${row.no}.`;
    }
    const warn = data.warnings?.length ? ` Lưu ý: ${data.warnings.join("; ")}` : "";
    const errs = data.errors?.length ? ` Lỗi: ${data.errors.join("; ")}` : "";
    setImgStatus(`Đã tạo ${data.items?.length || 0} ảnh AI.${assigned}${warn}${errs}`);
  } catch (err) {
    setImgStatus(`Gen ảnh AI thất bại: ${err.message || err}`);
  } finally {
    btn.textContent = "Gen ảnh AI";
    btn.disabled = !AI_ENABLED;
  }
}

function renderRows() {
  const body = $("#rows-body");
  body.innerHTML = "";
  const sorted = [...rows].sort((a, b) => a.no - b.no);

  sorted.forEach((row) => {
    const tr = document.createElement("tr");
    tr.dataset.rowId = row.id;
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button, textarea, input, select")) return;
      selectRow(row.id);
    });

    const tdNo = document.createElement("td");
    const inpNo = document.createElement("input");
    inpNo.type = "number";
    inpNo.min = 1;
    inpNo.value = row.no;
    inpNo.addEventListener("change", () => {
      const v = parseInt(inpNo.value, 10);
      row.no = Number.isFinite(v) && v > 0 ? v : row.no;
      inpNo.value = row.no;
      renderRows();
    });
    tdNo.appendChild(inpNo);

    const tdText = document.createElement("td");
    const ta = document.createElement("textarea");
    ta.value = row.text;
    ta.placeholder = "Nội dung tóm tắt / chữ trên video";
    ta.addEventListener("focus", () => selectRow(row.id));
    ta.addEventListener("input", () => {
      row.text = ta.value;
      selectRow(row.id);
      updateCharHints();
      syncSummaryFromRow();
    });
    ta.addEventListener("paste", () => {
      queueMicrotask(() => {
        row.text = ta.value;
        selectRow(row.id);
        updateCharHints();
        syncSummaryFromRow();
        flashPasteNotice(row.no, row.text.length);
      });
    });
    tdText.appendChild(ta);

    const tdImg = document.createElement("td");
    const wrap = document.createElement("div");
    wrap.className = "cell-img";

    const sel = document.createElement("select");
    const opt0 = document.createElement("option");
    opt0.value = "";
    opt0.textContent = "— pick from library —";
    sel.appendChild(opt0);
    library.forEach((item) => {
      const opt = document.createElement("option");
      opt.value = item.id;
      opt.textContent = item.original_name || item.id.slice(0, 8);
      sel.appendChild(opt);
    });
    sel.value = row.imageId || "";
    sel.addEventListener("change", () => {
      row.imageId = sel.value;
      renderRows();
    });

    const thumb = document.createElement("img");
    thumb.className = "thumb";
    const picked = library.find((x) => x.id === row.imageId);
    if (picked) {
      thumb.src = `${apiUrl(picked.url)}?t=${Date.now()}`;
      thumb.alt = "";
    } else {
      thumb.removeAttribute("src");
      thumb.alt = "No image";
      thumb.style.opacity = "0.35";
    }

    wrap.appendChild(sel);
    wrap.appendChild(thumb);
    tdImg.appendChild(wrap);

    const tdDel = document.createElement("td");
    const del = document.createElement("button");
    del.type = "button";
    del.className = "icon-del";
    del.title = "Remove row";
    del.textContent = "×";
    del.addEventListener("click", () => {
      rows = rows.filter((r) => r.id !== row.id);
      if (selectedRowId === row.id) selectedRowId = null;
      renderRows();
    });
    tdDel.appendChild(del);

    tr.appendChild(tdNo);
    tr.appendChild(tdText);
    tr.appendChild(tdImg);
    tr.appendChild(tdDel);
    body.appendChild(tr);
  });

  syncSelectedVisual();
  updateCharHints();
  syncSummaryFromRow();
}

async function refreshLibrary() {
  const r = await fetch(apiUrl("/api/library"));
  library = await r.json();
  renderLibrary();
  renderRows();
}

function addRow() {
  const row = { id: rowId(), no: nextRowNo(), text: "", imageId: "" };
  rows.push(row);
  selectedRowId = row.id;
  renderRows();
  return row;
}

function setStatus(msg) {
  $("#gen-status").textContent = msg || "";
}

function setImgStatus(msg) {
  const el = $("#img-status");
  if (el) el.textContent = msg || "";
}

function showUploadNotice(kind, html) {
  const el = $("#upload-notice");
  if (!el) return;
  el.hidden = false;
  el.className = `upload-notice upload-notice--${kind}`;
  el.innerHTML = html;
}

function hideUploadNotice() {
  const el = $("#upload-notice");
  if (!el) return;
  el.hidden = true;
  el.innerHTML = "";
}

function formatUploadErrors(errors) {
  if (!errors?.length) return "";
  const lines = errors.map((e) => {
    const name = e.name ? `<strong>${escapeHtml(e.name)}</strong>: ` : "";
    return `<li>${name}${escapeHtml(e.detail || "Lỗi không xác định")}</li>`;
  });
  return `<ul class="upload-error-list">${lines.join("")}</ul>`;
}

function escapeHtml(s) {
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

$("#btn-add-row").addEventListener("click", () => addRow());

async function uploadFiles(fileList) {
  if (!fileList || !fileList.length) return;

  const fd = new FormData();
  for (let i = 0; i < fileList.length; i++) {
    const f = fileList[i];
    fd.append("files", f, f.name || `image-${i}.jpg`);
    if (fileList.length === 1) {
      fd.append("file", f, f.name || "image.jpg");
    }
  }

  hideUploadNotice();
  setImgStatus(`Đang tải ${fileList.length} ảnh…`);

  try {
    const res = await fetch(apiUrl("/api/library/upload"), {
      method: "POST",
      body: fd,
    });
    const data = await res.json().catch(() => ({}));
    const items = data.items || [];
    const errors = data.errors || [];

    if (!res.ok && !items.length) {
      const d = data.detail;
      const fallback =
        typeof d === "string"
          ? d
          : Array.isArray(d)
            ? d.map((x) => x.msg || JSON.stringify(x)).join("; ")
            : data.message || res.statusText;
      const errList =
        errors.length > 0
          ? formatUploadErrors(errors)
          : `<p>${escapeHtml(fallback)}</p>`;
      showUploadNotice("error", `<p><strong>Không tải được ảnh</strong></p>${errList}`);
      setImgStatus(data.message || fallback || "Tải ảnh thất bại.");
      return;
    }

    let assigned = "";
    if (items.length) {
      await refreshLibrary();
      const row = assignToSelectedRow(items[0].id);
      assigned = ` Ảnh đầu tiên đã gán vào dòng STT ${row.no}.`;
    }

    const cropped = items.filter((x) => x.normalized_916);
    let successHtml = `<p>${escapeHtml(data.message || `Đã lưu ${items.length} ảnh.`)}</p>`;
    if (cropped.length) {
      successHtml += `<p class="upload-sub">Đã scale + crop về 9:16 (1080×1920): ${cropped
        .map((x) => escapeHtml(x.original_name))
        .join(", ")}</p>`;
    }

    if (errors.length) {
      showUploadNotice(
        "warn",
        `${successHtml}<p><strong>Một số file lỗi:</strong></p>${formatUploadErrors(errors)}`
      );
    } else if (items.length) {
      showUploadNotice("success", successHtml);
    }

    setImgStatus(
      (data.message || (items.length ? `Đã lưu ${items.length} ảnh.` : "Tải ảnh thất bại.")) +
        assigned
    );
  } catch (err) {
    const msg = err.message || String(err);
    showUploadNotice(
      "error",
      `<p><strong>Lỗi kết nối hoặc máy chủ</strong></p><p>${escapeHtml(msg)}</p><p class="upload-sub">Kiểm tra server đang chạy (uvicorn) và thử lại.</p>`
    );
    setImgStatus(`Tải ảnh thất bại: ${msg}`);
  }
}

$("#btn-ai-gen").addEventListener("click", () => generateFromSummary());

$("#summary").addEventListener("input", (e) => {
  const row = selectedRow() || addRow();
  row.text = e.target.value;
  const rowTa = document.querySelector(`tr[data-row-id="${row.id}"] textarea`);
  if (rowTa) rowTa.value = row.text;
  updateCharHints();
  syncSummaryFromRow();
});

const libUploadInput = $("#lib-upload");
const btnLibUpload = $("#btn-lib-upload");

if (btnLibUpload && libUploadInput) {
  btnLibUpload.addEventListener("click", () => libUploadInput.click());
  libUploadInput.addEventListener("change", async (e) => {
    const fl = e.target.files;
    await uploadFiles(fl);
    e.target.value = "";
  });
} else {
  console.error("Upload controls missing: #lib-upload or #btn-lib-upload");
  setStatus("Lỗi UI: không tìm thấy nút tải ảnh. Tải lại trang (Ctrl+Shift+R).");
}

$("#btn-generate").addEventListener("click", async () => {
  const duration = parseFloat($("#duration").value) || 5;
  const textBgEl = $("#text-bg");
  const text_bg = textBgEl ? textBgEl.value : "black";
  const payload = {
    duration,
    text_bg,
    rows: rows
      .filter((r) => r.imageId)
      .map((r) => ({
        no: r.no,
        text: r.text || "",
        image_id: r.imageId,
      })),
  };

  if (!payload.rows.length) {
    setStatus("Thêm ít nhất một dòng và chọn ảnh.");
    return;
  }

  const bgLabel = text_bg === "white" ? "nền trắng" : "nền đen";
  setStatus(`Đang tạo Reels (${bgLabel})…`);
  $("#btn-generate").disabled = true;

  try {
    const res = await fetch(apiUrl("/api/generate"), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(errorDetail(data, res));

    addOutputs(data.items || [], "video");

    const errTxt =
      data.errors && data.errors.length
        ? ` Errors: ${data.errors.map((e) => `#${e.no}: ${e.error}`).join("; ")}`
        : "";
    setStatus(
      data.ok
        ? `Done. ${data.items.length} file(s).${errTxt}`
        : `Finished with issues.${errTxt}`
    );
  } catch (err) {
    setStatus(`Generate failed: ${err.message || err}`);
  } finally {
    $("#btn-generate").disabled = false;
  }
});

$("#btn-export-images").addEventListener("click", async () => {
  const aspects = selectedAspects();
  const payload = {
    aspects,
    rows: rows.filter((r) => r.imageId).map((r) => ({ no: r.no, image_id: r.imageId })),
  };
  if (!payload.rows.length) {
    setStatus("Thêm ít nhất một dòng và chọn ảnh.");
    return;
  }
  if (!aspects.length) {
    setStatus("Chọn ít nhất một tỉ lệ ảnh (9:16, 4:5 hoặc 1:1).");
    return;
  }
  const btn = $("#btn-export-images");
  btn.disabled = true;
  setStatus(`Đang xuất ảnh (${aspects.join(", ")})…`);
  try {
    const data = await postJson("/api/export/images", payload);
    addOutputs(data.items || [], "image");
    const errTxt = data.errors?.length
      ? ` Lỗi: ${data.errors.map((e) => `#${e.no}${e.aspect ? ` ${e.aspect}` : ""}: ${e.error}`).join("; ")}`
      : "";
    setStatus(`Đã xuất ${data.items?.length || 0} ảnh.${errTxt}`);
  } catch (err) {
    setStatus(`Xuất ảnh thất bại: ${err.message || err}`);
  } finally {
    btn.disabled = false;
  }
});

$("#btn-zip").addEventListener("click", async () => {
  const filenames = [...outputs.keys()];
  if (!filenames.length) return;
  const btn = $("#btn-zip");
  btn.disabled = true;
  setStatus(`Đang nén ${filenames.length} file…`);
  try {
    const data = await postJson("/api/export/zip", { filenames });
    const a = document.createElement("a");
    a.href = apiUrl(data.url);
    a.download = data.filename;
    document.body.appendChild(a);
    a.click();
    a.remove();
    const miss = data.missing?.length
      ? ` Thiếu ${data.missing.length} file (server có thể đã restart, hãy tạo lại).`
      : "";
    setStatus(`Đã nén ${data.count} file vào ${data.filename}.${miss}`);
  } catch (err) {
    setStatus(`Nén ZIP thất bại: ${err.message || err}`);
  } finally {
    btn.disabled = outputs.size === 0;
  }
});

(async function init() {
  await loadConfig();
  applyAiState();
  await refreshLibrary();
  if (!rows.length) addRow();
  if (!selectedRowId && rows[0]) selectedRowId = rows[0].id;
  renderRows();
  updateCharHints();
})();
