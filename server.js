const express = require("express");
const cors = require("cors");
const { execFile, spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const { randomUUID } = require("crypto");
const os = require("os");

const app = express();
const PORT = 3000;
const DOWNLOADS_DIR = path.join(__dirname, "downloads");
const COOKIES_FILE = path.join(__dirname, "cookies.txt");
const CLEANUP_MAX_AGE = 2 * 60 * 60 * 1000; // 2 hours
const MAX_CONCURRENT_DOWNLOADS = 5;
const STDERR_MAX_LENGTH = 10000; // 10KB cap for stderr buffer
const MAX_INFO_RETRIES = 2;

// Ensure downloads directory exists
fs.mkdirSync(DOWNLOADS_DIR, { recursive: true });

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, "public")));
app.use("/downloads", express.static(DOWNLOADS_DIR));

// Track active downloads for progress
const activeDownloads = new Map();

// ─── Cleanup: remove old files + stale map entries ───────────────────────────
setInterval(() => {
  // Clean stale map entries (complete/error older than cleanup age)
  for (const [id, info] of activeDownloads) {
    if (info.status === "complete" || info.status === "error") {
      if (info.completedAt && Date.now() - info.completedAt > CLEANUP_MAX_AGE) {
        activeDownloads.delete(id);
      }
    }
  }

  // Clean old files
  fs.readdir(DOWNLOADS_DIR, (err, files) => {
    if (err) return;
    const now = Date.now();
    const protectedIds = new Set();
    for (const [id, info] of activeDownloads) {
      if (info.status === "downloading" || info.status === "paused") {
        protectedIds.add(id);
      }
    }
    for (const file of files) {
      const fileId = file.split(".")[0];
      if (protectedIds.has(fileId)) continue;
      const fp = path.join(DOWNLOADS_DIR, file);
      fs.stat(fp, (statErr, stat) => {
        if (!statErr && now - stat.mtimeMs > CLEANUP_MAX_AGE) {
          fs.unlink(fp, () => {});
        }
      });
    }
  });
}, 30 * 60 * 1000);

// ─── Helpers ─────────────────────────────────────────────────────────────────

function sanitizeFilename(name) {
  if (!name) return "video";
  return name
    .replace(/[<>:"/\\|?*\x00-\x1f]/g, "")
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 200)
    || "video";
}

function hasCookiesFile() {
  try { return fs.existsSync(COOKIES_FILE) && fs.statSync(COOKIES_FILE).size > 0; } catch { return false; }
}

function getActiveDownloadCount() {
  let count = 0;
  for (const [, info] of activeDownloads) {
    if (info.status === "downloading") count++;
  }
  return count;
}

function safeBasename(filepath) {
  const base = path.basename(filepath);
  if (base.includes("..") || path.isAbsolute(base)) return null;
  return base;
}

// Detect which browser to use for cookies (cached on startup)
let detectedBrowser = null;
function detectCookieBrowser() {
  // Prefer cookies.txt file first
  if (hasCookiesFile()) return null;
  // Try common browsers
  const browsers = ["chrome", "firefox", "edge", "brave", "opera", "safari"];
  for (const browser of browsers) {
    try {
      const { execFileSync } = require("child_process");
      execFileSync("yt-dlp", ["--cookies-from-browser", browser, "--dump-json", "--no-download", "https://example.com"], {
        timeout: 10000, stdio: "pipe",
      });
      return browser;
    } catch {
      // Browser not available or cookies not accessible
    }
  }
  return null;
}

// Build universal yt-dlp args that work across all sites
function buildBaseArgs() {
  const args = [
    "--no-playlist",
    "--no-warnings",
    // ─── Universal compatibility ───
    "--extractor-retries", "5",
    "--socket-timeout", "30",
    "--no-check-certificates",
    // ─── Impersonate a real browser to bypass bot detection ───
    "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "--referer", "",
  ];

  // Cookies: prefer cookies.txt > browser cookies
  if (hasCookiesFile()) {
    args.push("--cookies", COOKIES_FILE);
  } else if (detectedBrowser) {
    args.push("--cookies-from-browser", detectedBrowser);
  }

  return args;
}

// Sites that need special handling
function needsAuth(url) {
  const authDomains = ["x.com", "twitter.com", "instagram.com", "facebook.com", "fb.watch"];
  try {
    const hostname = new URL(url).hostname.replace(/^www\./, "");
    return authDomains.some((d) => hostname === d || hostname.endsWith("." + d));
  } catch { return false; }
}

function buildYtDlpArgs(outputTemplate, formatId, quality, url, isContinue) {
  const args = [
    ...buildBaseArgs(),
    "--newline",
    "--progress",
    // ─── Speed optimizations ───
    "--concurrent-fragments", "16",
    "--buffer-size", "16K",
    "--http-chunk-size", "10M",
    "--retries", "10",
    "--fragment-retries", "10",
    "-o", outputTemplate,
  ];

  if (!isContinue) {
    args.push("--no-part");
  } else {
    args.push("--continue");
  }

  if (formatId) {
    args.push("-f", formatId);
  } else if (quality) {
    if (quality === "best") {
      args.push("-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best");
      args.push("--merge-output-format", "mp4");
    } else if (quality === "audio") {
      args.push("-f", "bestaudio/best");
      args.push("--extract-audio");
      args.push("--audio-format", "mp3");
    } else {
      const h = parseInt(quality);
      if (isNaN(h)) {
        args.push("-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best");
        args.push("--merge-output-format", "mp4");
      } else {
        args.push("-f", `bestvideo[height<=${h}][ext=mp4]+bestaudio[ext=m4a]/bestvideo[height<=${h}]+bestaudio/best[height<=${h}]/best`);
        args.push("--merge-output-format", "mp4");
      }
    }
  } else {
    args.push("-f", "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best");
    args.push("--merge-output-format", "mp4");
  }

  args.push(url.trim());
  return args;
}

function attachProcessHandlers(proc, downloadInfo) {
  let stderrData = "";

  proc.stdout.on("data", (chunk) => {
    const text = chunk.toString();
    const match = text.match(/(\d+\.?\d*)%/);
    if (match) {
      downloadInfo.progress = parseFloat(match[1]);
    }
    const destMatch = text.match(/\[(?:Merger|download)\].*?Destination:\s*(.+)/);
    if (destMatch) {
      downloadInfo.filename = safeBasename(destMatch[1].trim()) || downloadInfo.filename;
    }
    const mergeMatch = text.match(/\[Merger\]\s+Merging formats into "(.+?)"/);
    if (mergeMatch) {
      downloadInfo.filename = safeBasename(mergeMatch[1].trim()) || downloadInfo.filename;
    }
  });

  proc.stderr.on("data", (chunk) => {
    if (stderrData.length < STDERR_MAX_LENGTH) {
      stderrData += chunk.toString();
      if (stderrData.length > STDERR_MAX_LENGTH) {
        stderrData = stderrData.slice(0, STDERR_MAX_LENGTH);
      }
    }
  });

  proc.on("close", (code, signal) => {
    if (signal === "SIGTERM" && downloadInfo.status === "paused") return;

    if (code !== 0) {
      downloadInfo.status = "error";
      downloadInfo.error = stderrData || "Download failed";
      downloadInfo.proc = null;
      downloadInfo.completedAt = Date.now();
      return;
    }

    try {
      const files = fs.readdirSync(DOWNLOADS_DIR).filter((f) => f.startsWith(downloadInfo.id));
      const completedFiles = files.filter((f) => !f.endsWith(".part"));
      if (completedFiles.length > 0) {
        downloadInfo.status = "complete";
        downloadInfo.progress = 100;
        downloadInfo.filename = completedFiles[0];
      } else {
        downloadInfo.status = "error";
        downloadInfo.error = "File not found after download";
      }
    } catch (fsErr) {
      downloadInfo.status = "error";
      downloadInfo.error = "Failed to locate downloaded file";
    }
    downloadInfo.proc = null;
    downloadInfo.completedAt = Date.now();
  });
}

// ─── Shared: build video response from yt-dlp JSON ───────────────────────────
function respondWithVideoData(data, url, res) {
  const formats = (data.formats || [])
    .filter((f) => f.url)
    .map((f) => ({
      format_id: f.format_id,
      ext: f.ext,
      resolution: f.resolution || (f.height ? `${f.width || "?"}x${f.height}` : "audio only"),
      height: f.height || 0,
      width: f.width || 0,
      filesize: f.filesize || f.filesize_approx || null,
      fps: f.fps || null,
      vcodec: f.vcodec !== "none" ? f.vcodec : null,
      acodec: f.acodec !== "none" ? f.acodec : null,
      has_video: !!(f.vcodec && f.vcodec !== "none"),
      has_audio: !!(f.acodec && f.acodec !== "none"),
      tbr: f.tbr || null,
      format_note: f.format_note || "",
      url: f.url,
    }));

  const videoFormats = formats.filter((f) => f.has_video);
  const audioFormats = formats.filter((f) => f.has_audio && !f.has_video);
  const combinedFormats = formats.filter((f) => f.has_video && f.has_audio);

  const qualityMap = new Map();
  for (const f of videoFormats) {
    const key = f.height || f.resolution;
    if (!qualityMap.has(key) || (f.tbr || 0) > (qualityMap.get(key).tbr || 0)) {
      qualityMap.set(key, f);
    }
  }

  res.json({
    title: data.title || "Unknown",
    description: data.description || "",
    thumbnail: data.thumbnail || data.thumbnails?.[data.thumbnails.length - 1]?.url || null,
    duration: data.duration || 0,
    duration_string: data.duration_string || formatDuration(data.duration || 0),
    uploader: data.uploader || data.channel || "Unknown",
    view_count: data.view_count || null,
    upload_date: data.upload_date || null,
    webpage_url: data.webpage_url || url,
    extractor: data.extractor || "unknown",
    formats: formats,
    video_formats: [...qualityMap.values()].sort((a, b) => (b.height || 0) - (a.height || 0)),
    audio_formats: audioFormats.sort((a, b) => (b.tbr || 0) - (a.tbr || 0)),
    combined_formats: combinedFormats.sort((a, b) => (b.height || 0) - (a.height || 0)),
    best_video: data.url || null,
  });
}

// ─── API: Extract video info (with retry) ────────────────────────────────────
app.post("/api/info", (req, res) => {
  const { url } = req.body;
  if (!url || typeof url !== "string") {
    return res.status(400).json({ error: "A valid URL is required" });
  }

  const urlPattern = /^https?:\/\/.+/i;
  if (!urlPattern.test(url.trim())) {
    return res.status(400).json({ error: "Invalid URL format" });
  }

  function tryExtract(attempt, extraArgs) {
    const args = [
      ...buildBaseArgs(),
      ...(extraArgs || []),
      "--dump-json",
      "--prefer-free-formats",
      url.trim(),
    ];

    execFile("yt-dlp", args, { timeout: 90000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        const errorMsg = stderr || err.message || "";

        if (attempt < MAX_INFO_RETRIES) {
          console.log(`Retry ${attempt + 1} for ${url}`);
          return setTimeout(() => tryExtract(attempt + 1, extraArgs), 1000 * (attempt + 1));
        }

        // If auth-required site failed and we haven't tried browser cookies yet, try each browser
        if (needsAuth(url.trim()) && !extraArgs) {
          const browsers = ["chrome", "firefox", "brave", "edge", "opera"];
          console.log(`Auth site detected, trying browser cookies for ${url}`);
          return tryWithBrowserCookies(browsers, 0);
        }
        console.error("yt-dlp error:", errorMsg);
        let userMessage = "Could not extract video info. Make sure the URL is valid and the video is accessible.";
        if (errorMsg.includes("No video could be found")) {
          userMessage = "No video found at this URL. Make sure the post/tweet contains a video.";
          if (needsAuth(url.trim())) {
            userMessage += " For Twitter/X, try exporting your cookies: run 'yt-dlp --cookies-from-browser chrome --dump-json \"" + url.trim() + "\"' in terminal first to grant keychain access, then restart the server.";
          }
        } else if (errorMsg.includes("Login") || errorMsg.includes("login") || errorMsg.includes("cookie") || errorMsg.includes("authenticate")) {
          userMessage = "This video requires authentication. Export your browser cookies to cookies.txt in the project root, or run the server once from terminal to grant keychain access for browser cookie extraction.";
        } else if (errorMsg.includes("Unsupported URL")) {
          userMessage = "This URL is not supported. Try pasting the direct video page URL.";
        } else if (errorMsg.includes("Private") || errorMsg.includes("private")) {
          userMessage = "This video is private and cannot be accessed.";
        } else if (errorMsg.includes("geo") || errorMsg.includes("country")) {
          userMessage = "This video is geo-restricted and not available in your region.";
        } else if (errorMsg.includes("HTTP Error 403") || errorMsg.includes("Forbidden")) {
          userMessage = "Access forbidden. The site may require authentication — try adding cookies.txt.";
        } else if (errorMsg.includes("HTTP Error 429") || errorMsg.includes("Too Many")) {
          userMessage = "Rate limited by the site. Please wait a moment and try again.";
        }
        return res.status(422).json({ error: userMessage });
      }

      try {
        const data = JSON.parse(stdout);
        respondWithVideoData(data, url, res);
      } catch (parseErr) {
        console.error("Parse error:", parseErr);
        res.status(500).json({ error: "Failed to parse video information" });
      }
    });
  }

  // Fallback: try each browser's cookies for auth-required sites
  function tryWithBrowserCookies(browsers, idx) {
    if (idx >= browsers.length) {
      // All browsers exhausted — return final error
      const isAuthSite = needsAuth(url.trim());
      const msg = isAuthSite
        ? "Could not access this video. For Twitter/X, Instagram, etc: (1) Run 'yt-dlp --cookies-from-browser chrome --dump-json \"YOUR_URL\"' in terminal once to grant keychain access, then restart the server. Or (2) Export cookies to cookies.txt in the project root."
        : "Could not extract video info. Make sure the URL is valid and the video is accessible.";
      return res.status(422).json({ error: msg });
    }
    const browser = browsers[idx];
    console.log(`Trying cookies from ${browser} for ${url}`);

    const args = [
      "--no-playlist", "--no-warnings",
      "--extractor-retries", "3",
      "--socket-timeout", "30",
      "--cookies-from-browser", browser,
      "--dump-json",
      "--prefer-free-formats",
      url.trim(),
    ];

    execFile("yt-dlp", args, { timeout: 60000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        console.log(`Browser ${browser} failed: ${(stderr || err.message).slice(0, 100)}`);
        return tryWithBrowserCookies(browsers, idx + 1);
      }
      // Success — parse and respond (same as tryExtract success path)
      try {
        const data = JSON.parse(stdout);
        // If this browser worked, save it for future use
        detectedBrowser = browser;
        console.log(`Browser ${browser} worked! Saving for future requests.`);
        respondWithVideoData(data, url, res);
      } catch (parseErr) {
        console.error("Parse error:", parseErr);
        return tryWithBrowserCookies(browsers, idx + 1);
      }
    });
  }

  tryExtract(0, null);
});

// ─── API: Download video ─────────────────────────────────────────────────────
app.post("/api/download", (req, res) => {
  const { url, format_id, quality, title } = req.body;
  if (!url || typeof url !== "string") {
    return res.status(400).json({ error: "URL is required" });
  }

  const urlPattern = /^https?:\/\/.+/i;
  if (!urlPattern.test(url.trim())) {
    return res.status(400).json({ error: "Invalid URL format" });
  }

  if (getActiveDownloadCount() >= MAX_CONCURRENT_DOWNLOADS) {
    return res.status(429).json({ error: `Too many concurrent downloads (max ${MAX_CONCURRENT_DOWNLOADS}). Please wait for one to finish.` });
  }

  let id;
  do { id = randomUUID(); } while (activeDownloads.has(id));

  const outputTemplate = path.join(DOWNLOADS_DIR, `${id}.%(ext)s`);
  const args = buildYtDlpArgs(outputTemplate, format_id, quality, url, false);

  const downloadInfo = {
    id,
    progress: 0,
    status: "downloading",
    filename: null,
    error: null,
    url: url.trim(),
    format_id: format_id || null,
    quality: quality || null,
    title: sanitizeFilename(title) || `video_${id.slice(0, 8)}`,
    outputTemplate,
    proc: null,
    completedAt: null,
  };
  activeDownloads.set(id, downloadInfo);

  const proc = spawn("yt-dlp", args);
  downloadInfo.proc = proc;
  attachProcessHandlers(proc, downloadInfo);

  res.json({ id });
});

// ─── API: Pause download ────────────────────────────────────────────────────
app.post("/api/pause/:id", (req, res) => {
  const info = activeDownloads.get(req.params.id);
  if (!info) return res.status(404).json({ error: "Download not found" });
  if (info.status !== "downloading") {
    return res.status(400).json({ error: `Cannot pause: status is "${info.status}"` });
  }

  info.status = "paused";
  if (info.proc) {
    info.proc.kill("SIGTERM");
    info.proc = null;
  }

  res.json({ id: info.id, status: "paused", progress: info.progress });
});

// ─── API: Resume download ───────────────────────────────────────────────────
app.post("/api/resume/:id", (req, res) => {
  const info = activeDownloads.get(req.params.id);
  if (!info) return res.status(404).json({ error: "Download not found" });
  if (info.status !== "paused") {
    return res.status(400).json({ error: `Cannot resume: status is "${info.status}"` });
  }

  let warning = null;
  try {
    const partialFiles = fs.readdirSync(DOWNLOADS_DIR).filter((f) => f.startsWith(info.id));
    if (partialFiles.length === 0) {
      warning = "Partial file expired, restarting from beginning";
      info.progress = 0;
    }
  } catch {
    warning = "Could not check partial files, restarting";
    info.progress = 0;
  }

  const args = buildYtDlpArgs(info.outputTemplate, info.format_id, info.quality, info.url, true);
  const proc = spawn("yt-dlp", args);
  info.proc = proc;
  info.status = "downloading";
  info.error = null;
  attachProcessHandlers(proc, info);

  const response = { id: info.id, status: "downloading", progress: info.progress };
  if (warning) response.warning = warning;
  res.json(response);
});

// ─── API: Check download progress ───────────────────────────────────────────
app.get("/api/progress/:id", (req, res) => {
  const info = activeDownloads.get(req.params.id);
  if (!info) return res.status(404).json({ error: "Download not found" });
  res.json({
    id: info.id,
    progress: info.progress,
    status: info.status,
    filename: info.filename,
    error: info.error,
  });
});

// ─── API: Serve downloaded file ──────────────────────────────────────────────
app.get("/api/file/:id", (req, res) => {
  const info = activeDownloads.get(req.params.id);
  if (!info || info.status !== "complete" || !info.filename) {
    return res.status(404).json({ error: "File not ready" });
  }

  // Path traversal protection
  const safeName = safeBasename(info.filename);
  if (!safeName) {
    return res.status(400).json({ error: "Invalid filename" });
  }

  const filePath = path.join(DOWNLOADS_DIR, safeName);
  // Ensure resolved path is still within DOWNLOADS_DIR
  const resolved = path.resolve(filePath);
  if (!resolved.startsWith(path.resolve(DOWNLOADS_DIR))) {
    return res.status(400).json({ error: "Invalid file path" });
  }

  try {
    if (!fs.existsSync(filePath)) {
      return res.status(404).json({ error: "File not found — it may have been cleaned up. Try downloading again." });
    }
  } catch {
    return res.status(404).json({ error: "File not found" });
  }

  const ext = path.extname(safeName);
  const downloadName = `${info.title || "video"}${ext}`;
  res.download(filePath, downloadName);
});

// ─── API: Stream/proxy video for player ──────────────────────────────────────
app.get("/api/stream", (req, res) => {
  const { url: videoUrl } = req.query;
  if (!videoUrl) return res.status(400).json({ error: "URL required" });

  const args = [...buildBaseArgs(), "--get-url", "-f", "best[ext=mp4]/best", videoUrl];
  execFile("yt-dlp", args, { timeout: 30000 }, (err, stdout) => {
    if (err) {
      return res.status(422).json({ error: "Could not get stream URL" });
    }
    const directUrl = stdout.trim().split("\n")[0];
    if (!directUrl) return res.status(422).json({ error: "No stream URL found" });
    res.redirect(directUrl);
  });
});

// ─── Global error handler ────────────────────────────────────────────────────
app.use((err, req, res, _next) => {
  console.error("Unhandled error:", err);
  res.status(500).json({ error: "Internal server error" });
});

process.on("uncaughtException", (err) => {
  console.error("Uncaught exception:", err);
});

process.on("unhandledRejection", (reason) => {
  console.error("Unhandled rejection:", reason);
});

function formatDuration(seconds) {
  if (!seconds) return "0:00";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  const s = Math.floor(seconds % 60);
  if (h > 0) return `${h}:${m.toString().padStart(2, "0")}:${s.toString().padStart(2, "0")}`;
  return `${m}:${s.toString().padStart(2, "0")}`;
}

app.listen(PORT, () => {
  console.log(`GrabNWatch server running at http://localhost:${PORT}`);
  console.log(`Cookies file: ${hasCookiesFile() ? "found" : "not found (some sites like Twitter/X may need it)"}`);
});
