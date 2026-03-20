const express = require("express");
const cors = require("cors");
const { execFile, spawn } = require("child_process");
const path = require("path");
const fs = require("fs");
const { randomUUID } = require("crypto");
const app = express();
const PORT = 3000;
const DOWNLOADS_DIR = path.join(__dirname, "downloads");
const COOKIES_FILE = path.join(__dirname, "cookies.txt");
const CLEANUP_MAX_AGE = 2 * 60 * 60 * 1000; // 2 hours
const MAX_CONCURRENT_DOWNLOADS = 5;
const STDERR_MAX_LENGTH = 10000; // 10KB cap for stderr buffer

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

// Browser cookie source — auto-detected when a cookie strategy succeeds in /api/info
let detectedBrowser = null;

// Build universal yt-dlp args that work across all sites
function buildBaseArgs(useImpersonate) {
  const args = [
    "--no-playlist",
    "--no-warnings",
    "--extractor-retries", "5",
    "--socket-timeout", "30",
    "--no-check-certificates",
  ];

  if (useImpersonate) {
    args.push("--impersonate", "chrome");
  } else {
    args.push(
      "--user-agent", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "--referer", ""
    );
  }

  if (hasCookiesFile()) {
    args.push("--cookies", COOKIES_FILE);
  } else if (detectedBrowser) {
    args.push("--cookies-from-browser", detectedBrowser);
  }

  return args;
}

function needsAuth(url) {
  const authDomains = ["x.com", "twitter.com", "instagram.com", "facebook.com", "fb.watch"];
  try {
    const hostname = new URL(url).hostname.replace(/^www\./, "");
    return authDomains.some((d) => hostname === d || hostname.endsWith("." + d));
  } catch { return false; }
}

// ─── Headless Chrome video scraper (last resort fallback) ────────────────────
const CHROME_PATH = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome";

async function scrapeVideoFromPage(pageUrl) {
  let puppeteer;
  try { puppeteer = require("puppeteer-core"); } catch { return null; }

  let browser;
  try {
    browser = await puppeteer.launch({
      executablePath: CHROME_PATH,
      headless: "new",
      args: ["--no-sandbox", "--disable-setuid-sandbox", "--disable-dev-shm-usage", "--disable-blink-features=AutomationControlled"],
    });

    const page = await browser.newPage();
    await page.setUserAgent("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36");

    // Capture video URLs from network requests
    const videoUrls = [];
    const videoExts = [".mp4", ".m3u8", ".webm", ".mpd", ".ts"];
    const videoMimes = ["video/", "application/x-mpegurl", "application/dash+xml", "application/vnd.apple.mpegurl"];

    page.on("response", (response) => {
      const reqUrl = response.url();
      const contentType = response.headers()["content-type"] || "";
      const isVideo = videoExts.some((e) => reqUrl.split("?")[0].toLowerCase().includes(e)) ||
        videoMimes.some((m) => contentType.toLowerCase().includes(m));
      if (isVideo && reqUrl.startsWith("http")) {
        const size = parseInt(response.headers()["content-length"] || "0");
        videoUrls.push({ url: reqUrl, contentType, size });
      }
    });

    await page.goto(pageUrl, { waitUntil: "networkidle2", timeout: 30000 });

    // Also scrape <video> and <source> elements from the DOM
    const domVideos = await page.evaluate(() => {
      const results = [];
      document.querySelectorAll("video, video source, source[type*='video']").forEach((el) => {
        const src = el.src || el.getAttribute("src");
        if (src && src.startsWith("http")) {
          results.push({ url: src, type: el.getAttribute("type") || "", from: "dom" });
        }
      });
      // Check og:video meta tags
      document.querySelectorAll("meta[property='og:video'], meta[property='og:video:url'], meta[name='twitter:player:stream']").forEach((el) => {
        const content = el.getAttribute("content");
        if (content && content.startsWith("http")) {
          results.push({ url: content, type: "meta", from: "meta" });
        }
      });
      return results;
    });

    // Get page title
    const title = await page.title();

    await browser.close();

    // Merge and deduplicate
    const allUrls = new Map();
    for (const v of [...videoUrls, ...domVideos]) {
      if (!allUrls.has(v.url)) allUrls.set(v.url, v);
    }

    const found = [...allUrls.values()];
    if (found.length === 0) return null;

    // Sort: prefer largest files and .mp4
    found.sort((a, b) => {
      const aIsMp4 = a.url.includes(".mp4") ? 1 : 0;
      const bIsMp4 = b.url.includes(".mp4") ? 1 : 0;
      if (bIsMp4 !== aIsMp4) return bIsMp4 - aIsMp4;
      return (b.size || 0) - (a.size || 0);
    });

    return {
      title: title || "Unknown",
      webpage_url: pageUrl,
      extractor: "browser-scrape",
      thumbnail: null,
      duration: 0,
      duration_string: "",
      uploader: "",
      view_count: null,
      upload_date: null,
      description: "",
      formats: found.map((v, i) => ({
        format_id: `scraped-${i}`,
        ext: v.url.split("?")[0].split(".").pop()?.slice(0, 5) || "mp4",
        resolution: "unknown",
        height: 0,
        width: 0,
        filesize: v.size || null,
        fps: null,
        vcodec: "unknown",
        acodec: "unknown",
        has_video: true,
        has_audio: true,
        tbr: null,
        format_note: v.from === "meta" ? "og:video" : (v.from === "dom" ? "page source" : "network capture"),
        url: v.url,
      })),
    };
  } catch (err) {
    console.error("Scraper error:", err.message);
    if (browser) try { await browser.close(); } catch {}
    return null;
  }
}

function buildYtDlpArgs(outputTemplate, formatId, quality, url, isContinue, useImpersonate) {
  const args = [
    ...buildBaseArgs(useImpersonate),
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

  if (isContinue) {
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
    // Parse speed: "at  5.23MiB/s" or "at ~  2.1MiB/s"
    const speedMatch = text.match(/at\s+~?\s*([\d.]+\s*\w+\/s)/);
    if (speedMatch) {
      downloadInfo.speed = speedMatch[1].trim();
    }
    // Parse ETA: "ETA 00:10" or "ETA 01:23:45"
    const etaMatch = text.match(/ETA\s+([\d:]+)/);
    if (etaMatch) {
      downloadInfo.eta = etaMatch[1];
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

// ─── API: Extract video info (multi-strategy pipeline) ───────────────────────
app.post("/api/info", (req, res) => {
  const { url } = req.body;
  if (!url || typeof url !== "string") {
    return res.status(400).json({ error: "A valid URL is required" });
  }

  const urlPattern = /^https?:\/\/.+/i;
  if (!urlPattern.test(url.trim())) {
    return res.status(400).json({ error: "Invalid URL format" });
  }

  const trimmedUrl = url.trim();
  let responded = false;

  function done(statusCode, body) {
    if (responded) return;
    responded = true;
    res.status(statusCode).json(body);
  }

  // Strategy runner: tries multiple yt-dlp arg combos, proxies, then headless scraper
  const strategies = [
    { name: "yt-dlp default", args: [...buildBaseArgs(false), "--dump-json", "--prefer-free-formats", trimmedUrl] },
    { name: "yt-dlp impersonate", args: [...buildBaseArgs(true), "--dump-json", "--prefer-free-formats", trimmedUrl] },
  ];

  // Add browser cookie strategies for auth sites
  if (needsAuth(trimmedUrl)) {
    for (const browser of ["chrome", "firefox", "brave"]) {
      strategies.push({
        name: `yt-dlp cookies-from-${browser}`,
        args: ["--no-playlist", "--no-warnings", "--extractor-retries", "3", "--socket-timeout", "30",
               "--impersonate", "chrome", "--cookies-from-browser", browser,
               "--dump-json", "--prefer-free-formats", trimmedUrl],
      });
    }
  }

  let strategyIdx = 0;

  function runNextStrategy() {
    if (responded) return;

    // If all yt-dlp strategies exhausted, try headless scraper
    if (strategyIdx >= strategies.length) {
      console.log(`All yt-dlp strategies failed for ${trimmedUrl}, trying headless scraper...`);
      scrapeVideoFromPage(trimmedUrl).then((scraped) => {
        if (scraped && scraped.formats && scraped.formats.length > 0) {
          console.log(`Scraper found ${scraped.formats.length} video(s) for ${trimmedUrl}`);
          return done(200, buildVideoResponse(scraped, trimmedUrl));
        }
        done(422, { error: "Could not find any video at this URL. All extraction methods were tried (yt-dlp, browser impersonation, headless scraper). The site may block automated access entirely." });
      }).catch((err) => {
        console.error("Scraper crashed:", err.message);
        done(422, { error: "Could not extract video info after trying all methods." });
      });
      return;
    }

    const strategy = strategies[strategyIdx];
    console.log(`[${strategyIdx + 1}/${strategies.length}] Trying: ${strategy.name}`);

    execFile("yt-dlp", strategy.args, { timeout: 60000, maxBuffer: 10 * 1024 * 1024 }, (err, stdout, stderr) => {
      if (err) {
        const errMsg = (stderr || err.message || "").slice(0, 200);
        console.log(`  -> Failed: ${errMsg}`);
        strategyIdx++;
        return runNextStrategy();
      }

      try {
        const data = JSON.parse(stdout);
        console.log(`  -> Success via ${strategy.name}`);

        // If browser cookies worked, remember the browser
        const cookieMatch = strategy.name.match(/cookies-from-(\w+)/);
        if (cookieMatch) {
          detectedBrowser = cookieMatch[1];
        }

        done(200, buildVideoResponse(data, trimmedUrl));
      } catch {
        console.log(`  -> Parse failed, trying next...`);
        strategyIdx++;
        runNextStrategy();
      }
    });
  }

  runNextStrategy();
});

// Build JSON response from yt-dlp data or scraper data
function buildVideoResponse(data, url) {
  // If it already has format objects with the right shape (scraper), pass through
  const rawFormats = data.formats || [];
  const formats = rawFormats[0]?.format_id
    ? rawFormats.map((f) => ({
        format_id: f.format_id,
        ext: f.ext || "mp4",
        resolution: f.resolution || (f.height ? `${f.width || "?"}x${f.height}` : "unknown"),
        height: f.height || 0,
        width: f.width || 0,
        filesize: f.filesize || f.filesize_approx || null,
        fps: f.fps || null,
        vcodec: f.vcodec && f.vcodec !== "none" ? f.vcodec : null,
        acodec: f.acodec && f.acodec !== "none" ? f.acodec : null,
        has_video: !!(f.has_video !== undefined ? f.has_video : (f.vcodec && f.vcodec !== "none")),
        has_audio: !!(f.has_audio !== undefined ? f.has_audio : (f.acodec && f.acodec !== "none")),
        tbr: f.tbr || null,
        format_note: f.format_note || "",
        url: f.url,
      }))
    : rawFormats;

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

  return {
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
  };
}

// ─── Direct URL download (for scraped video URLs) ────────────────────────────
function startDirectDownload(id, directUrl, title, res) {
  const ext = directUrl.split("?")[0].split(".").pop()?.slice(0, 5) || "mp4";
  const outputFile = path.join(DOWNLOADS_DIR, `${id}.${ext}`);

  const downloadInfo = {
    id,
    progress: 0,
    status: "downloading",
    filename: null,
    error: null,
    speed: null,
    eta: null,
    url: directUrl,
    format_id: null,
    quality: null,
    title: sanitizeFilename(title) || `video_${id.slice(0, 8)}`,
    outputTemplate: outputFile,
    proc: null,
    completedAt: null,
  };
  activeDownloads.set(id, downloadInfo);

  // Use curl for direct download — handles redirects, headers, and shows progress
  const proc = spawn("curl", [
    "-L",                         // follow redirects
    "-o", outputFile,             // output file
    "-#",                         // progress bar
    "--connect-timeout", "30",
    "--max-time", "7200",         // 2 hour max
    "-A", "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    directUrl,
  ]);

  downloadInfo.proc = proc;

  proc.stderr.on("data", (chunk) => {
    const text = chunk.toString();
    const match = text.match(/(\d+\.?\d*)%/);
    if (match) downloadInfo.progress = parseFloat(match[1]);
  });

  proc.on("close", (code, signal) => {
    if (signal === "SIGTERM" && downloadInfo.status === "paused") return;

    if (code !== 0) {
      downloadInfo.status = "error";
      downloadInfo.error = "Direct download failed";
      downloadInfo.proc = null;
      downloadInfo.completedAt = Date.now();
      return;
    }

    try {
      if (fs.existsSync(outputFile) && fs.statSync(outputFile).size > 0) {
        downloadInfo.status = "complete";
        downloadInfo.progress = 100;
        downloadInfo.filename = `${id}.${ext}`;
      } else {
        downloadInfo.status = "error";
        downloadInfo.error = "Downloaded file is empty";
      }
    } catch {
      downloadInfo.status = "error";
      downloadInfo.error = "Failed to verify downloaded file";
    }
    downloadInfo.proc = null;
    downloadInfo.completedAt = Date.now();
  });

  res.json({ id });
}

// ─── API: Download video ─────────────────────────────────────────────────────
app.post("/api/download", (req, res) => {
  const { url, format_id, quality, title, direct_url } = req.body;
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

  // If this is a direct URL from the headless scraper, download it with curl
  if (direct_url && urlPattern.test(direct_url)) {
    return startDirectDownload(id, direct_url, title, res);
  }

  const outputTemplate = path.join(DOWNLOADS_DIR, `${id}.%(ext)s`);
  // Try with impersonate first for yt-dlp downloads
  const args = buildYtDlpArgs(outputTemplate, format_id, quality, url, false, true);

  const downloadInfo = {
    id,
    progress: 0,
    status: "downloading",
    filename: null,
    error: null,
    speed: null,
    eta: null,
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

  const args = buildYtDlpArgs(info.outputTemplate, info.format_id, info.quality, info.url, true, true);
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
    speed: info.speed || null,
    eta: info.eta || null,
    title: info.title || null,
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
  res.download(filePath, downloadName, (err) => {
    if (!err) {
      // Auto-delete after successful save
      setTimeout(() => {
        fs.unlink(filePath, () => {});
        activeDownloads.delete(req.params.id);
      }, 5000); // 5s delay to ensure browser finishes writing
    }
  });
});

// ─── API: Stream/proxy video for player ──────────────────────────────────────
app.get("/api/stream", (req, res) => {
  const { url: videoUrl } = req.query;
  if (!videoUrl) return res.status(400).json({ error: "URL required" });

  const args = [...buildBaseArgs(false), "--get-url", "-f", "best[ext=mp4]/best", videoUrl];
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
