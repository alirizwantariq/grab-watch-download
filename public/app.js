// ─── State ────────────────────────────────────────────────────────────────────
let currentVideoUrl = "";
let currentVideoData = null;
let currentDownloadId = null;
let progressInterval = null;
let isDownloading = false;

// ─── DOM Elements (with safe access) ─────────────────────────────────────────
const $ = (id) => document.getElementById(id);
const app = $("app");
const searchForm = $("searchForm");
const videoUrlInput = $("videoUrl");
const submitBtn = $("submitBtn");
const loaderBar = $("loaderBar");
const loaderText = $("loaderText");
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
const progressSection = $("progressSection");
const progressBar = $("progressBar");
const progressPercent = $("progressPercent");
const progressStatus = $("progressStatus");
const saveBtn = $("saveBtn");
const pauseBtn = $("pauseBtn");
const resumeBtn = $("resumeBtn");
const progressControls = $("progressControls");
const refreshLinksBtn = $("refreshLinksBtn");
const quickDownloads = document.querySelector(".quick-downloads");
const noFormatsMsg = $("noFormatsMsg");

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
      try {
        data = await res.json();
      } catch {
        throw new Error("Invalid response from server");
      }

      if (!res.ok) {
        throw new Error(data.error || "Failed to extract video info");
      }

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
  if (app) app.classList.add("has-results");
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
      videoPlayer.onerror = () => {
        playerSection.style.display = "none";
      };
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
      <td><button class="format-dl-btn" data-format-id="${escapeHtml(f.format_id)}">Download</button></td>
    `;
    formatsBody.appendChild(tr);
  }
}

// ─── Download Handlers ───────────────────────────────────────────────────────
if (quickDownloads) {
  quickDownloads.addEventListener("click", (e) => {
    const btn = e.target.closest(".download-btn");
    if (!btn || !currentVideoUrl || isDownloading) return;
    startDownload(currentVideoUrl, null, btn.dataset.quality);
  });
}

if (formatsBody) {
  formatsBody.addEventListener("click", (e) => {
    const btn = e.target.closest(".format-dl-btn");
    if (!btn || !currentVideoUrl || isDownloading) return;
    startDownload(currentVideoUrl, btn.dataset.formatId, null);
  });
}

function setDownloadingState(active) {
  isDownloading = active;
  // Disable/enable all download buttons
  document.querySelectorAll(".download-btn, .format-dl-btn").forEach((btn) => {
    if (active) {
      btn.classList.add("downloading");
      btn.disabled = true;
    } else {
      btn.classList.remove("downloading");
      btn.disabled = false;
    }
  });
}

async function startDownload(url, formatId, quality) {
  setDownloadingState(true);

  if (progressSection) {
    progressSection.style.display = "block";
    progressSection.scrollIntoView({ behavior: "smooth", block: "center" });
  }
  if (progressBar) {
    progressBar.style.width = "0%";
    progressBar.classList.remove("paused");
  }
  if (progressPercent) progressPercent.textContent = "0%";
  if (progressStatus) progressStatus.textContent = "Starting download...";
  if (saveBtn) saveBtn.style.display = "none";
  if (pauseBtn) pauseBtn.style.display = "inline-flex";
  if (resumeBtn) resumeBtn.style.display = "none";
  if (progressControls) progressControls.style.display = "flex";

  stopPolling();

  try {
    const res = await fetch("/api/download", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ url, format_id: formatId, quality, title: currentVideoData?.title || null }),
    });

    let data;
    try {
      data = await res.json();
    } catch {
      throw new Error("Invalid response from server");
    }

    if (!res.ok) throw new Error(data.error || "Download failed");

    currentDownloadId = data.id;
    pollProgress(data.id);
  } catch (err) {
    if (progressStatus) progressStatus.textContent = `Error: ${err.message}`;
    if (progressPercent) progressPercent.textContent = "Failed";
    if (progressControls) progressControls.style.display = "none";
    setDownloadingState(false);
  }
}

function stopPolling() {
  if (progressInterval) {
    clearInterval(progressInterval);
    progressInterval = null;
  }
}

function pollProgress(downloadId) {
  stopPolling();

  progressInterval = setInterval(async () => {
    // Guard against stale polls from a previous download
    if (downloadId !== currentDownloadId) {
      stopPolling();
      return;
    }

    try {
      const res = await fetch(`/api/progress/${downloadId}`);

      let data;
      try {
        data = await res.json();
      } catch {
        // Non-JSON response — server may be down
        stopPolling();
        if (progressStatus) progressStatus.textContent = "Connection error — server may be restarting";
        setDownloadingState(false);
        return;
      }

      if (!res.ok) {
        stopPolling();
        if (progressStatus) progressStatus.textContent = "Download not found";
        if (progressControls) progressControls.style.display = "none";
        setDownloadingState(false);
        return;
      }

      if (progressBar) progressBar.style.width = `${data.progress}%`;
      if (progressPercent) progressPercent.textContent = `${Math.round(data.progress)}%`;

      if (data.status === "downloading") {
        if (progressStatus) progressStatus.textContent = "Downloading...";
        if (progressBar) progressBar.classList.remove("paused");
      } else if (data.status === "paused") {
        stopPolling();
        if (progressBar) progressBar.classList.add("paused");
        if (progressStatus) progressStatus.textContent = "Paused";
        if (pauseBtn) pauseBtn.style.display = "none";
        if (resumeBtn) resumeBtn.style.display = "inline-flex";
      } else if (data.status === "complete") {
        stopPolling();
        if (progressBar) {
          progressBar.style.width = "100%";
          progressBar.classList.remove("paused");
        }
        if (progressPercent) progressPercent.textContent = "100%";
        if (progressStatus) progressStatus.textContent = "Complete!";
        if (progressControls) progressControls.style.display = "none";
        if (saveBtn) {
          saveBtn.href = `/api/file/${downloadId}`;
          saveBtn.style.display = "inline-flex";
        }
        setDownloadingState(false);
      } else if (data.status === "error") {
        stopPolling();
        if (progressStatus) progressStatus.textContent = `Error: ${data.error || "Download failed"}`;
        if (progressPercent) progressPercent.textContent = "Failed";
        if (progressControls) progressControls.style.display = "none";
        setDownloadingState(false);
      }
    } catch {
      stopPolling();
      if (progressStatus) progressStatus.textContent = "Connection lost — check if server is running";
      if (progressControls) progressControls.style.display = "none";
      setDownloadingState(false);
    }
  }, 1000);
}

// ─── Pause Handler ───────────────────────────────────────────────────────────
if (pauseBtn) {
  pauseBtn.addEventListener("click", async () => {
    if (!currentDownloadId) return;
    pauseBtn.disabled = true;

    try {
      const res = await fetch(`/api/pause/${currentDownloadId}`, { method: "POST" });
      let data;
      try { data = await res.json(); } catch { throw new Error("Invalid response"); }
      if (!res.ok) throw new Error(data.error || "Failed to pause");

      stopPolling();
      if (progressBar) progressBar.classList.add("paused");
      if (progressStatus) progressStatus.textContent = "Paused";
      pauseBtn.style.display = "none";
      if (resumeBtn) resumeBtn.style.display = "inline-flex";
    } catch (err) {
      if (progressStatus) progressStatus.textContent = `Pause error: ${err.message}`;
    } finally {
      pauseBtn.disabled = false;
    }
  });
}

// ─── Resume Handler ──────────────────────────────────────────────────────────
if (resumeBtn) {
  resumeBtn.addEventListener("click", async () => {
    if (!currentDownloadId) return;
    resumeBtn.disabled = true;

    try {
      const res = await fetch(`/api/resume/${currentDownloadId}`, { method: "POST" });
      let data;
      try { data = await res.json(); } catch { throw new Error("Invalid response"); }
      if (!res.ok) throw new Error(data.error || "Failed to resume");

      if (progressBar) progressBar.classList.remove("paused");
      if (progressStatus) progressStatus.textContent = data.warning || "Resuming...";
      resumeBtn.style.display = "none";
      if (pauseBtn) pauseBtn.style.display = "inline-flex";

      pollProgress(currentDownloadId);
    } catch (err) {
      if (progressStatus) progressStatus.textContent = `Resume error: ${err.message}`;
    } finally {
      resumeBtn.disabled = false;
    }
  });
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
    } catch (err) {
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
  if (progressSection) progressSection.style.display = "none";
  if (app) app.classList.remove("has-results");
}

function formatBytes(bytes) {
  if (!bytes) return "\u2014";
  const units = ["B", "KB", "MB", "GB"];
  let i = 0;
  let size = bytes;
  while (size >= 1024 && i < units.length - 1) {
    size /= 1024;
    i++;
  }
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
