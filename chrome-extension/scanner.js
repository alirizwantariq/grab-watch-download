/**
 * MediaGrab fetcher — page scanner.
 * Injected into every frame of the active tab by popup.js
 * (chrome.scripting.executeScript, allFrames). Self-contained: returns every
 * downloadable media URL in this frame with the best quality label, preview
 * thumbnail and title it can find:
 *   1. <video>/<audio>/<source> elements (poster = preview, res attr = quality)
 *   2. KVS "flashvars" player configs (erome and most tube sites)
 *   3. resource-timing entries (media the page already started loading,
 *      including HLS/DASH playlists)
 *   4. og:video / twitter:player:stream meta tags
 *   5. links that point straight at a media file
 *
 * Result: { pageTitle, pageUrl, items: [{url, kind, quality, poster, title}] }
 * kind is "file" for a plain media file and "stream" for an HLS/DASH playlist
 * (MediaGrab assembles those with yt-dlp).
 */
(function () {
  function scanMedia() {
    const items = [];
    const seen = new Set();
    const FILE_RE = /\.(mp4|webm|m4v|mov|mkv|flv|avi|mp3|m4a|aac|flac|wav|ogg|opus)$/i;
    const STREAM_RE = /\.(m3u8|mpd)$/i;

    function pathOf(url) {
      try { return new URL(url).pathname; } catch (e) { return ""; }
    }

    function kindOf(url) {
      const path = pathOf(url);
      if (STREAM_RE.test(path)) return "stream";
      if (FILE_RE.test(path)) return "file";
      // signed CDN paths like /video.mp4/chunk or ?file=a.mp4
      if (/\.mp4([/?#&]|$)/i.test(url)) return "file";
      return "";
    }

    function qualityFromUrl(u) {
      const m = u.match(/(\d{3,4})p(?=[._?#\/-]|$)/i);
      return m ? m[1] + "p" : "";
    }

    function abs(u) {
      try {
        const url = new URL(u, location.href);
        return /^https?:$/.test(url.protocol) ? url.href : null;
      } catch (e) { return null; }
    }

    function add(u, quality, poster, title) {
      if (!u) return;
      const url = abs(u);
      if (!url || seen.has(url)) return;
      const kind = kindOf(url);
      if (!kind) return;
      seen.add(url);
      items.push({
        url: url,
        kind: kind,
        quality: String(quality || qualityFromUrl(url) || "").slice(0, 10),
        poster: poster ? (abs(poster) || "") : "",
        title: String(title || "").trim().slice(0, 120)
      });
    }

    // 1. media elements (with their posters)
    document.querySelectorAll("video, audio").forEach(v => {
      const poster = v.getAttribute("poster") || v.getAttribute("data-poster") || "";
      const hq = v.videoHeight ? v.videoHeight + "p" : "";
      const title = v.getAttribute("title") || v.getAttribute("aria-label") || "";
      add(v.currentSrc || v.src, hq, poster, title);
      v.querySelectorAll("source").forEach(s => {
        const res = s.getAttribute("res") || s.getAttribute("size") ||
                    s.getAttribute("label") || "";
        const q = /^\d{3,4}$/.test(res) ? res + "p" : res;
        add(s.src || s.getAttribute("src"), q || hq, poster, title);
      });
    });
    document.querySelectorAll("source").forEach(s => {
      if (!s.closest("video, audio")) add(s.src || s.getAttribute("src"), "", "", "");
    });

    // 2. KVS flashvars configs
    const blockRe = /flashvars\s*=\s*\{(.{20,}?)\}\s*;/s;
    for (const script of document.querySelectorAll("script:not([src])")) {
      const txt = script.textContent || "";
      if (txt.length > 400000 || txt.indexOf("flashvars") === -1) continue;
      const mm = txt.match(blockRe);
      if (!mm) continue;
      const pairs = {};
      const pairRe = /([A-Za-z_][\w]*)\s*:\s*'([^']*)'/g;
      let p;
      while ((p = pairRe.exec(mm[1]))) pairs[p[1]] = p[2];
      const title = pairs.video_title || "";
      const poster = pairs.preview_url || pairs.poster_url || "";
      for (const k of ["video_url", "video_alt_url", "video_alt_url2",
                       "video_alt_url3", "video_alt_url4"]) {
        const u = pairs[k];
        const label = pairs[k + "_text"] || "";
        if (u) add(u, /^(-|none)?$/i.test(label) ? "" : label, poster, title);
      }
    }

    // 3. resource timings (already-loaded media, HLS/DASH playlists)
    try {
      performance.getEntriesByType("resource").forEach(r => add(r.name, "", "", ""));
    } catch (e) { /* not available in this frame */ }

    // 4. social meta
    document.querySelectorAll(
      'meta[property^="og:video"], meta[name="twitter:player:stream"]'
    ).forEach(m => add(m.getAttribute("content"), "", "", ""));

    // 5. plain links to media files
    document.querySelectorAll("a[href]").forEach(a => {
      const href = a.getAttribute("href") || "";
      if (!/\.(mp4|webm|m4v|mov|mkv|mp3|m4a|flac)([?#]|$)/i.test(href)) return;
      add(href, "", "", (a.getAttribute("download") || a.textContent || "").trim());
    });

    let pageTitle = "";
    try {
      const og = document.querySelector('meta[property="og:title"]');
      pageTitle = ((og && og.getAttribute("content")) || document.title || "").trim();
    } catch (e) { /* ignore */ }

    return {
      pageTitle: pageTitle.slice(0, 160),
      pageUrl: location.href,
      top: window === window.top,
      items: items.slice(0, 100)
    };
  }

  window.__mgdScan = scanMedia;   // fallback handle for executeScript
  return scanMedia();
})();
