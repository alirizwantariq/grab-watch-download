/**
 * MediaGrab fetcher — page scanner.
 * Injected into the active tab by popup.js (chrome.scripting.executeScript).
 * Self-contained: finds every downloadable media URL on the page with the
 * best quality label + preview thumbnail it can find:
 *   1. <video>/<source> elements (poster attribute = preview, res attr = quality)
 *   2. KVS "flashvars" player configs (erome and most tube sites)
 *   3. resource-timing entries (blob-less direct media loads)
 *   4. og:video / twitter:player:stream meta tags
 */
function scanMedia() {
  const items = [];
  const seen = new Set();
  const MEDIA_RE = /\.(mp4|webm|m4v|m3u8|mp3|m4a|mov|mkv|flv)([?#\/]|$)/i;

  function qualityFromUrl(u) {
    const m = u.match(/(\d{3,4})p(?=[._?#\/]|$)/i);
    return m ? m[1] + "p" : "";
  }

  function abs(u) {
    try { return new URL(u, location.href).href; } catch (e) { return null; }
  }

  function add(u, quality, poster, title) {
    if (!u) return;
    const url = abs(u);
    if (!url || seen.has(url)) return;
    if (!MEDIA_RE.test(url) && !/\.mp4/i.test(url)) return;
    seen.add(url);
    items.push({
      url: url,
      quality: (quality || qualityFromUrl(url) || "").toString().slice(0, 10),
      poster: poster ? abs(poster) : "",
      title: (title || "").toString().slice(0, 90)
    });
  }

  // 1. video elements (with their posters)
  document.querySelectorAll("video").forEach(v => {
    const poster = v.getAttribute("poster") || v.getAttribute("data-poster") || "";
    const hq = v.videoHeight ? v.videoHeight + "p" : "";
    const title = v.getAttribute("title") || v.getAttribute("aria-label") || "";
    if (v.src) add(v.src, hq, poster, title);
    v.querySelectorAll("source").forEach(s => {
      const res = s.getAttribute("res");
      add(s.src || s.getAttribute("src"),
          res ? res + "p" : hq, poster, title);
    });
  });
  document.querySelectorAll("source").forEach(s => {
    if (!s.closest("video")) add(s.src || s.getAttribute("src"), "", "", "");
  });

  // 2. KVS flashvars configs
  const blockRe = /flashvars\s*=\s*\{(.{20,}?)\}\s*;/s;
  for (const script of document.querySelectorAll("script")) {
    const txt = script.textContent || "";
    const mm = txt.match(blockRe);
    if (!mm) continue;
    const pairs = {};
    const pairRe = /([A-Za-z_][\w]*)\s*:\s*'([^']*)'/g;
    let p;
    while ((p = pairRe.exec(mm[1]))) pairs[p[1]] = p[2];
    const title = pairs.video_title || "";
    const poster = pairs.preview_url || pairs.poster_url || "";
    for (const k of ["video_url", "video_alt_url",
                     "video_alt_url2", "video_alt_url3", "video_alt_url4"]) {
      const u = pairs[k];
      if (u && MEDIA_RE.test(u)) {
        add(u, pairs[k + "_text"] || "", poster, title);
      }
    }
  }

  // 3. resource timings (already-loaded media)
  performance.getEntriesByType("resource").forEach(r => add(r.name, "", "", ""));

  // 4. social meta
  document.querySelectorAll('meta[property^="og:video"], meta[name="twitter:player:stream"]')
    .forEach(m => add(m.getAttribute("content"), "", "", ""));

  // dedupe by final path, keep the poster/quality of the first occurrence
  return items.slice(0, 100);
}
window.__mgdScan = scanMedia;   // fallback handle for executeScript
scanMedia();
