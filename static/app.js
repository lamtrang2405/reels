let library = [];
let rows = [];
let selectedRowId = null;
let rowSeq = 1;
let TEXT_SAFETY_MAX = 50000;
let INNER_PAD_V = 20;
let ESTIMATE_DENSE_CHARS = 21000;
let CAPTION_FONT_PX = 38;

const $ = (sel) => document.querySelector(sel);

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
      if (!selectedRowId) {
        setStatus("Select a reels row first (click the row).");
        return;
      }
      const row = rows.find((r) => r.id === selectedRowId);
      if (row) {
        row.imageId = item.id;
        renderRows();
        setStatus(`Assigned image to row ${row.no}.`);
      }
    });

    card.appendChild(rm);
    card.appendChild(pick);
    root.appendChild(card);
  });
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
      selectedRowId = row.id;
      syncSelectedVisual();
      updateCharHints();
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
    ta.placeholder = "Caption / overlay text";
    ta.addEventListener("input", () => {
      row.text = ta.value;
      updateCharHints();
    });
    ta.addEventListener("paste", () => {
      queueMicrotask(() => {
        row.text = ta.value;
        updateCharHints();
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
}

async function refreshLibrary() {
  const r = await fetch(apiUrl("/api/library"));
  library = await r.json();
  renderLibrary();
  renderRows();
}

function addRow() {
  rows.push({ id: rowId(), no: nextRowNo(), text: "", imageId: "" });
  renderRows();
}

function setStatus(msg) {
  $("#gen-status").textContent = msg || "";
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
  setStatus(`Đang tải ${fileList.length} ảnh…`);

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
      setStatus(data.message || fallback || "Tải ảnh thất bại.");
      return;
    }

    if (items.length) {
      await refreshLibrary();
      const singleId = items.length === 1 ? items[0].id : null;
      if (singleId && selectedRowId) {
        const row = rows.find((r) => r.id === selectedRowId);
        if (row) row.imageId = singleId;
        renderRows();
      }
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

    setStatus(data.message || (items.length ? `Đã lưu ${items.length} ảnh.` : "Tải ảnh thất bại."));
  } catch (err) {
    const msg = err.message || String(err);
    showUploadNotice(
      "error",
      `<p><strong>Lỗi kết nối hoặc máy chủ</strong></p><p>${escapeHtml(msg)}</p><p class="upload-sub">Kiểm tra server đang chạy (uvicorn) và thử lại.</p>`
    );
    setStatus(`Tải ảnh thất bại: ${msg}`);
  }
}

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

  const dl = $("#downloads");
  dl.innerHTML = "";

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
    if (!res.ok) throw new Error(data.detail || res.statusText);

    data.items.forEach((it) => {
      const li = document.createElement("li");
      const a = document.createElement("a");
      a.href = apiUrl(it.url);
      a.download = it.filename;
      a.textContent = `Download reel ${String(it.no).padStart(2, "0")} (${it.filename})`;
      li.appendChild(a);
      dl.appendChild(li);
    });

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

(async function init() {
  await loadConfig();
  await refreshLibrary();
  if (!rows.length) addRow();
  if (!selectedRowId && rows[0]) selectedRowId = rows[0].id;
  renderRows();
  updateCharHints();
})();
