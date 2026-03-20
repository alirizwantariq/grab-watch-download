// ─── State ────────────────────────────────────────────────────────────────────
let currentVideoUrl = "";
let currentVideoData = null;

// Multiple downloads tracking: Map<id, { interval, element }>
const downloads = new Map();

// ─── DOM Elements (with safe access) ─────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const searchForm = $("searchForm");
const videoUrlInput = $("videoUrl");
const submitBtn = $("submitBtn");
const loaderBar = $("loaderBar");
const errorMessage = $("errorMessage");
const resultsSection = $("resultsSection");
const thumbnail = $("thumbnail");
const durationBadge = $("durationBadge");
const videoTitle = $("videoTitle");
const uploader = $("uploader");
const viewCount = $("viewCount");
const extractor = $("extractor");
const playerSection = $("playerSection");
const videoPlayer = $("videoPlayer");
const formatsBody = $("formatsBody");
const downloadsSection = $("downloadsSection");
const downloadsList = $("downloadsList");
const refreshLinksBtn = $("refreshLinksBtn");
const quickDownloads = document.querySelector(".quick-downloads");
const noFormatsMsg = $("noFormatsMsg");
const appEl = $("app");

// ─── Form Submit ─────────────────────────────────────────────────────────────
if (searchForm) {
  searchForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = videoUrlInput.value.trim();
    if (!url) return;

    currentVideoUrl = url;
    setLoading(true);
    hideError();
    hideResults();

    try {
      const res = await fetch("/api/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url }),
      });

      let data;
      try { data = await res.json(); } catch { throw new Error("Invalid response from server"); }
      if (!res.ok) throw new Error(data.error || "Failed to extract video info");

      currentVideoData = data;
      renderResults(data);
    } catch (err) {
      showError(err.message);
    } finally {
      setLoading(false);
    }
  });
}

// ─── Render Results ──────────────────────────────────────────────────────────
function renderResults(data) {
  if (thumbnail) {
    thumbnail.src = data.thumbnail || "";
    thumbnail.alt = data.title || "No thumbnail";
  }
  if (durationBadge) {
    durationBadge.textContent = data.duration_string || "";
    durationBadge.style.display = data.duration_string ? "block" : "none";
  }
  if (videoTitle) videoTitle.textContent = data.title || "";
  if (uploader) uploader.textContent = data.uploader || "";
  if (viewCount) viewCount.textContent = data.view_count ? formatNumber(data.view_count) + " views" : "";
  if (extractor) extractor.textContent = data.extractor ? `Source: ${data.extractor}` : "";

  setupPlayer(data);
  renderFormatsTable(data.formats);

  if (resultsSection) {
    resultsSection.style.display = "block";
    resultsSection.scrollIntoView({ behavior: "smooth", block: "start" });
  }
  if (appEl) appEl.classList.add("has-results");
}

// ─── Player Setup ────────────────────────────────────────────────────────────
function setupPlayer(data) {
  if (!videoPlayer || !playerSection) return;

  const playable = data.combined_formats.find((f) => f.ext === "mp4") ||
    data.combined_formats[0] ||
    data.video_formats.find((f) => f.ext === "mp4") ||
    data.video_formats[0];

  if (playable && playable.url) {
    videoPlayer.src = playable.url;
    playerSection.style.display = "block";
    videoPlayer.onerror = () => {
      videoPlayer.src = `/api/stream?url=${encodeURIComponent(currentVideoUrl)}`;
      videoPlayer.onerror = () => { playerSection.style.display = "none"; };
    };
  } else if (data.best_video) {
    videoPlayer.src = data.best_video;
    playerSection.style.display = "block";
  } else {
    playerSection.style.display = "none";
  }
}

// ─── Formats Table ───────────────────────────────────────────────────────────
function renderFormatsTable(formats) {
  if (!formatsBody) return;
  formatsBody.innerHTML = "";

  if (!formats || formats.length === 0) {
    if (noFormatsMsg) noFormatsMsg.style.display = "block";
    return;
  }
  if (noFormatsMsg) noFormatsMsg.style.display = "none";

  const sorted = [...formats].sort((a, b) => {
    const scoreA = (a.has_video ? 2 : 0) + (a.has_audio ? 1 : 0);
    const scoreB = (b.has_video ? 2 : 0) + (b.has_audio ? 1 : 0);
    if (scoreB !== scoreA) return scoreB - scoreA;
    return (b.height || 0) - (a.height || 0);
  });

  for (const f of sorted) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td>${escapeHtml(f.format_note || f.format_id)}</td>
      <td>${escapeHtml(f.resolution)}</td>
      <td>${escapeHtml(f.ext)}</td>
      <td>${f.filesize ? formatBytes(f.filesize) : "\u2014"}</td>
      <td><span class="badge ${f.has_video ? "badge-yes" : "badge-no"}">${f.has_video ? "\u2713" : "\u2014"}</span></td>
      <td><span class="badge ${f.has_audio ? "badge-yes" : "badge-no"}">${f.has_audio ? "\u2713" : "\u2014"}</span></td>
      <td><button class="format-dl-btn" data-format-id="${escapeHtml(f.format_id)}" data-direct-url="${f.format_id?.startsWith("scraped") ? escapeHtml(f.url) : ""}">Download</button></td>
    `;
    formatsBody.appendChild(tr);
  }
}

// ─── Download Handlers ───────────────────────────────────────────────────────
if (quickDownloads) {
  quickDownloads.addEventListener("click", (e) => {
    const btn = e.target.closest(".download-btn");
    if (!btn || !currentVideoUrl) return;

    let directUrl = null;
    if (currentVideoData?.extractor === "browser-scrape" && currentVideoData.formats?.length > 0) {
      directUrl = currentVideoData.formats[0].url;
    }
    startDownload(currentVideoUrl, null, btn.dataset.quality, directUrl);
  });
}

if (formatsBody) {
  formatsBody.addEventListener("click", (e) => {
    const btn = e.target.closest(".format-dl-btn");
    if (!btn || !currentVideoUrl) return;
    const directUrl = btn.dataset.directUrl || null;
    startDownload(currentVideoUrl, btn.dataset.formatId, null, directUrl);
  });
}

// ─── Create download item UI ─────────────────────────────────────────────────
function createDownloadItem(id, title, quality) {
  const item = document.createElement("div");
  item.className = "download-item";
  item.id = `dl-${id}`;
  item.innerHTML = `
    <div class="dl-item-header">
      <span class="dl-item-title">${escapeHtml(title || "Video")}</span>
      <span class="dl-item-quality">${escapeHtml(quality || "")}</span>
    </div>
    <div class="progress-bar-wrap">
      <div class="progress-bar" id="bar-${id}" style="width:0%"></div>
    </div>
    <div class="dl-item-info">
      <span class="dl-item-percent" id="pct-${id}">0%</span>
      <span class="dl-item-speed" id="speed-${id}"></span>
      <span class="dl-item-eta" id="eta-${id}"></span>
      <span class="dl-item-status" id="status-${id}">Starting...</span>
    </div>
    <div class="dl-item-actions">
      <button class="pause-btn" id="pause-${id}">
        <svg class="dl-icon" viewBox="0 0 24 24"><path d="M6 19h4V5H6v14zm8-14v14h4V5h-4z"/></svg>
        Pause
      </button>
      <button class="resume-btn" id="resume-${id}" style="display:none">
        <svg class="dl-icon" viewBox="0 0 24 24"><path d="M8 5v14l11-7z"/></svg>
        Resume
      </button>
      <a class="save-btn" id="save-${id}" style="display:none" href="#" download>
        <svg class="dl-icon" viewBox="0 0 24 24"><path d="M12 16l-5-5h3V4h4v7h3l-5 5zm-7 4h14v-2H5v2z"/></svg>
        Save File
      </a>
      <button class="dismiss-btn" id="dismiss-${id}" style="display:none">
        <svg class="dl-icon" viewBox="0 0 24 24"><path d="M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z"/></svg>
      </button>
    </div>
  `;

  // Bind pause
  item.querySelector(`#pause-${id}`).addEventListener("click", () => pauseDownload(id));
  // Bind resume
  item.querySelector(`#resume-${id}`).addEventListener("click", () => resumeDownload(id));
  // Bind save — after click, auto-delete from server
  const saveLink = item.querySelector(`#save-${id}`);
  saveLink.addEventListener("click", () => {
    setTimeout(() => dismissDownload(id), 3000);
  });
  // Bind dismiss
  item.querySelector(`#dismiss-${id}`).addEventListener("click", () => dismissDownload(id));

  return item;
}

async function startDownload(url, formatId, quality, directUrl) {
  if (downloadsSection) downloadsSection.style.display = "block";

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        url,
        format_id: formatId,
        quality,
        title: currentVideoData?.title || null,
        direct_url: directUrl || undefined,
      }),
    });

    let data;
    try { data = await res.json(); } catch { throw new Error("Invalid response from server"); }
    if (!res.ok) throw new Error(data.error || "Download failed");

    const qualityLabel = quality === "best" ? "Best Quality" : quality === "audio" ? "MP3" : quality ? `${quality}p` : formatId || "";
    const item = createDownloadItem(data.id, currentVideoData?.title, qualityLabel);
    if (downloadsList) downloadsList.prepend(item);

    pollProgress(data.id);
  } catch (err) {
    showError(err.message);
  }
}

// ─── Poll Progress ───────────────────────────────────────────────────────────
function pollProgress(downloadId) {
  const interval = setInterval(async () => {
    try {
      const res = await fetch(`/api/progress/${downloadId}`);
      let data;
      try { data = await res.json(); } catch {
        clearInterval(interval);
        downloads.delete(downloadId);
        return;
      }

      if (!res.ok) {
        clearInterval(interval);
        downloads.delete(downloadId);
        updateItemStatus(downloadId, "error", "Download not found");
        return;
      }

      const bar = $(`bar-${downloadId}`);
      const pct = $(`pct-${downloadId}`);
      const speed = $(`speed-${downloadId}`);
      const eta = $(`eta-${downloadId}`);
      const status = $(`status-${downloadId}`);

      if (bar) bar.style.width = `${data.progress}%`;
      if (pct) pct.textContent = `${Math.round(data.progress)}%`;
      if (speed) speed.textContent = data.speed ? `${data.speed}` : "";
      if (eta) eta.textContent = data.eta ? `ETA ${data.eta}` : "";

      if (data.status === "downloading") {
        if (status) status.textContent = "Downloading...";
        if (bar) bar.classList.remove("paused");
      } else if (data.status === "paused") {
        clearInterval(interval);
        downloads.delete(downloadId);
        if (bar) bar.classList.add("paused");
        if (status) status.textContent = "Paused";
        if (speed) speed.textContent = "";
        if (eta) eta.textContent = "";
        const pauseEl = $(`pause-${downloadId}`);
        const resumeEl = $(`resume-${downloadId}`);
        if (pauseEl) pauseEl.style.display = "none";
        if (resumeEl) resumeEl.style.display = "inline-flex";
      } else if (data.status === "complete") {
        clearInterval(interval);
        downloads.delete(downloadId);
        if (bar) { bar.style.width = "100%"; bar.classList.remove("paused"); }
        if (pct) pct.textContent = "100%";
        if (status) status.textContent = "Complete!";
        if (speed) speed.textContent = "";
        if (eta) eta.textContent = "";
        const pauseEl = $(`pause-${downloadId}`);
        const saveEl = $(`save-${downloadId}`);
        const dismissEl = $(`dismiss-${downloadId}`);
        if (pauseEl) pauseEl.style.display = "none";
        if (saveEl) {
          saveEl.href = `/api/file/${downloadId}`;
          saveEl.style.display = "inline-flex";
        }
        if (dismissEl) dismissEl.style.display = "inline-flex";
      } else if (data.status === "error") {
        clearInterval(interval);
        downloads.delete(downloadId);
        if (status) status.textContent = `Error: ${data.error || "Download failed"}`;
        if (pct) pct.textContent = "Failed";
        if (speed) speed.textContent = "";
        if (eta) eta.textContent = "";
        const pauseEl = $(`pause-${downloadId}`);
        const dismissEl = $(`dismiss-${downloadId}`);
        if (pauseEl) pauseEl.style.display = "none";
        if (dismissEl) dismissEl.style.display = "inline-flex";
      }
    } catch {
      clearInterval(interval);
      downloads.delete(downloadId);
    }
  }, 1000);

  downloads.set(downloadId, { interval });
}

// ─── Pause / Resume / Dismiss ────────────────────────────────────────────────
async function pauseDownload(id) {
  const pauseEl = $(`pause-${id}`);
  if (pauseEl) pauseEl.disabled = true;

  try {
    const res = await fetch(`/api/pause/${id}`, { method: "POST" });
    let data;
    try { data = await res.json(); } catch { throw new Error("Invalid response"); }
    if (!res.ok) throw new Error(data.error || "Failed to pause");

    // Stop polling
    const dl = downloads.get(id);
    if (dl) { clearInterval(dl.interval); downloads.delete(id); }

    const bar = $(`bar-${id}`);
    const status = $(`status-${id}`);
    const speed = $(`speed-${id}`);
    const eta = $(`eta-${id}`);
    const resumeEl = $(`resume-${id}`);
    if (bar) bar.classList.add("paused");
    if (status) status.textContent = "Paused";
    if (speed) speed.textContent = "";
    if (eta) eta.textContent = "";
    if (pauseEl) pauseEl.style.display = "none";
    if (resumeEl) resumeEl.style.display = "inline-flex";
  } catch (err) {
    const status = $(`status-${id}`);
    if (status) status.textContent = `Pause error: ${err.message}`;
  } finally {
    if (pauseEl) pauseEl.disabled = false;
  }
}

async function resumeDownload(id) {
  const resumeEl = $(`resume-${id}`);
  if (resumeEl) resumeEl.disabled = true;

  try {
    const res = await fetch(`/api/resume/${id}`, { method: "POST" });
    let data;
    try { data = await res.json(); } catch { throw new Error("Invalid response"); }
    if (!res.ok) throw new Error(data.error || "Failed to resume");

    const bar = $(`bar-${id}`);
    const status = $(`status-${id}`);
    const pauseEl = $(`pause-${id}`);
    if (bar) bar.classList.remove("paused");
    if (status) status.textContent = data.warning || "Resuming...";
    if (resumeEl) resumeEl.style.display = "none";
    if (pauseEl) pauseEl.style.display = "inline-flex";

    pollProgress(id);
  } catch (err) {
    const status = $(`status-${id}`);
    if (status) status.textContent = `Resume error: ${err.message}`;
  } finally {
    if (resumeEl) resumeEl.disabled = false;
  }
}

function dismissDownload(id) {
  const dl = downloads.get(id);
  if (dl) { clearInterval(dl.interval); downloads.delete(id); }
  const item = $(`dl-${id}`);
  if (item) {
    item.style.animation = "fadeOut 0.3s ease";
    setTimeout(() => item.remove(), 300);
  }
  // Hide section if no more downloads
  if (downloadsList && downloadsList.children.length <= 1) {
    setTimeout(() => {
      if (downloadsList.children.length === 0 && downloadsSection) {
        downloadsSection.style.display = "none";
      }
    }, 350);
  }
}

function updateItemStatus(id, status, message) {
  const statusEl = $(`status-${id}`);
  const pauseEl = $(`pause-${id}`);
  const dismissEl = $(`dismiss-${id}`);
  if (statusEl) statusEl.textContent = message;
  if (pauseEl) pauseEl.style.display = "none";
  if (dismissEl) dismissEl.style.display = "inline-flex";
}

// ─── Refresh Links Handler ───────────────────────────────────────────────────
if (refreshLinksBtn) {
  refreshLinksBtn.addEventListener("click", async () => {
    if (!currentVideoUrl) return;
    refreshLinksBtn.disabled = true;
    refreshLinksBtn.classList.add("spinning");

    try {
      const res = await fetch("/api/info", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ url: currentVideoUrl }),
      });

      let data;
      try { data = await res.json(); } catch { throw new Error("Invalid response"); }
      if (!res.ok) throw new Error(data.error || "Failed to refresh links");

      currentVideoData = data;
      renderFormatsTable(data.formats);
      setupPlayer(data);

      const origHTML = refreshLinksBtn.innerHTML;
      refreshLinksBtn.textContent = "Links Refreshed!";
      setTimeout(() => { refreshLinksBtn.innerHTML = origHTML; }, 2000);
    } catch {
      const origHTML = refreshLinksBtn.innerHTML;
      refreshLinksBtn.textContent = "Refresh failed";
      setTimeout(() => { refreshLinksBtn.innerHTML = origHTML; }, 2000);
    } finally {
      refreshLinksBtn.disabled = false;
      refreshLinksBtn.classList.remove("spinning");
    }
  });
}

// ─── Helpers ─────────────────────────────────────────────────────────────────
function setLoading(loading) {
  if (submitBtn) {
    submitBtn.disabled = loading;
    const btnText = submitBtn.querySelector(".btn-text");
    const btnLoader = submitBtn.querySelector(".btn-loader");
    if (btnText) btnText.style.display = loading ? "none" : "inline";
    if (btnLoader) btnLoader.style.display = loading ? "inline-block" : "none";
  }
  if (loaderBar) loaderBar.style.display = loading ? "block" : "none";
}

function showError(msg) {
  if (errorMessage) {
    errorMessage.textContent = msg;
    errorMessage.style.display = "block";
  }
}

function hideError() {
  if (errorMessage) errorMessage.style.display = "none";
}

function hideResults() {
  if (resultsSection) resultsSection.style.display = "none";
  if (playerSection) playerSection.style.display = "none";
  if (appEl) appEl.classList.remove("has-results");
}

function formatBytes(bytes) {
  if (!bytes) return "\u2014";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return `${size.toFixed(1)} ${units[i]}`;
}

function formatNumber(num) {
  if (num >= 1_000_000) return (num / 1_000_000).toFixed(1) + "M";
  if (num >= 1_000) return (num / 1_000).toFixed(1) + "K";
  return num.toString();
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

// ─── Restore from session ────────────────────────────────────────────────────
try {
  const savedUrl = sessionStorage.getItem("gnw_url");
  if (savedUrl && videoUrlInput) videoUrlInput.value = savedUrl;
} catch { /* sessionStorage blocked */ }

if (videoUrlInput) {
  videoUrlInput.addEventListener("input", () => {
    try { sessionStorage.setItem("gnw_url", videoUrlInput.value); } catch {}
  });
}

// ─── Paste from clipboard shortcut ───────────────────────────────────────────
document.addEventListener("keydown", (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === "v" && document.activeElement !== videoUrlInput) {
    if (videoUrlInput) videoUrlInput.focus();
  }
});
