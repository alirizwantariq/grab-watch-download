// MediaGrab fetcher — popup: scan the page (all frames) → probe sizes via
// mgd → let the user pick items → hand the batch to mgd (native messaging).
//
// Everything that comes from the page (titles, urls, posters, quality
// labels) is untrusted: it is only ever put into the DOM through
// textContent / element properties, never through innerHTML.

let items = [];
const selected = new Set();
let hostOk = false;
let currentPageUrl = "";
let currentPageTitle = "";
let scanDone = false;

const $ = id => document.getElementById(id);

// Sites whose players stream through MediaSource: nothing to pick on the
// page itself, but MediaGrab resolves the page URL with yt-dlp.
const PLATFORM_RE = /(^|\.)(youtube\.com|youtu\.be|x\.com|twitter\.com|vimeo\.com|tiktok\.com|instagram\.com|facebook\.com|fb\.watch|twitch\.tv|dailymotion\.com|reddit\.com|soundcloud\.com|bilibili\.com|rumble\.com|streamable\.com)$/i;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text != null) node.textContent = text;
  return node;
}

function fmtSize(n) {
  if (n == null) return "";
  const u = [["GB", 1e9], ["MB", 1e6], ["KB", 1e3]];
  for (const [name, div] of u) if (n >= div) return (n / div).toFixed(1) + " " + name;
  return n + " B";
}

function native(payload) {
  return new Promise(resolve => {
    try {
      chrome.runtime.sendMessage({ type: "native", payload }, resp => {
        const err = chrome.runtime.lastError;
        resolve(err ? { ok: false, error: err.message } : (resp || { ok: false }));
      });
    } catch (e) {
      resolve({ ok: false, error: String((e && e.message) || e) });
    }
  });
}

function isHttp(u) { return /^https?:\/\//i.test(u || ""); }

function hostOf(u) {
  try { return new URL(u).hostname; } catch (e) { return ""; }
}

function fileNameOf(u) {
  try {
    const last = new URL(u).pathname.split("/").filter(Boolean).pop() || "";
    try { return decodeURIComponent(last); } catch (e) { return last; }
  } catch (e) { return ""; }
}

// A file name the app can offer in its "Save as" sheet (no extension: the
// engine keeps the real one).
function cleanName(s) {
  return String(s || "")
    .replace(/[\\/:*?"<>|\u0000-\u001f]+/g, " ")
    .replace(/\s+/g, " ")
    .trim()
    .replace(/^\.+/, "")
    .slice(0, 120)
    .trim();
}

function assignNames() {
  // items that would get the same name (qualities of one video, several
  // untitled videos on one page) are told apart by their quality label,
  // then by a counter
  const groups = new Map();
  for (const it of items) {
    it.name = cleanName(it.title || currentPageTitle);   // "" → app uses the url's file name
    if (!it.name) continue;
    const key = it.name.toLowerCase();
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(it);
  }
  for (const group of groups.values()) {
    if (group.length < 2) continue;
    const used = new Map();
    for (const it of group) {
      const label = it.quality || (it.kind === "stream" ? "stream" : "");
      const name = label ? `${it.name} ${label}` : it.name;
      const n = (used.get(name.toLowerCase()) || 0) + 1;
      used.set(name.toLowerCase(), n);
      it.name = n > 1 ? `${name} (${n})` : name;
    }
  }
}

// ── rendering ──────────────────────────────────────────────────────────────

function placeholder(title, text) {
  const box = el("div", "placeholder");
  if (title) box.appendChild(el("b", null, title));
  box.appendChild(document.createTextNode(text));
  return box;
}

function thumbFor(it) {
  if (it.poster && isHttp(it.poster)) {
    const img = el("img", "thumb");
    img.alt = "";
    img.loading = "lazy";
    img.referrerPolicy = "no-referrer";
    img.addEventListener("error", () => img.replaceWith(el("div", "thumb", "▶")), { once: true });
    img.src = it.poster;
    return img;
  }
  return el("div", "thumb", it.kind === "stream" ? "≋" : "▶");
}

function render() {
  const wrap = $("items");
  wrap.replaceChildren();
  if (!items.length) {
    wrap.appendChild(scanDone
      ? placeholder("No media files found on this page",
          isHttp(currentPageUrl)
            ? "Start playing the video and reopen this popup, or send the page itself to MediaGrab below."
            : "This kind of page cannot be scanned.")
      : placeholder(null, "Scanning this page…"));
    updateSummary();
    return;
  }
  for (const it of items) {
    const row = el("div", "item" + (selected.has(it.url) ? " on" : ""));
    row.tabIndex = 0;
    row.setAttribute("role", "checkbox");
    row.setAttribute("aria-checked", selected.has(it.url) ? "true" : "false");
    row.title = it.url;
    row.appendChild(thumbFor(it));

    const meta = el("div", "meta");
    meta.appendChild(el("div", "t", it.name || fileNameOf(it.url) || it.url));
    const sub = el("div", "s");
    if (it.kind === "stream") sub.appendChild(el("span", "q stream", "STREAM"));
    if (it.quality) sub.appendChild(el("span", "q", it.quality));
    const bits = [];
    if (it.kind === "stream") bits.push("HLS/DASH");
    else if (it.size != null) bits.push(fmtSize(it.size));
    else if (it.probed) bits.push("size unknown");
    bits.push(hostOf(it.url));
    const file = fileNameOf(it.url);
    if (file && it.name) bits.push(file.length > 34 ? file.slice(0, 33) + "…" : file);
    sub.appendChild(document.createTextNode(bits.filter(Boolean).join(" · ")));
    meta.appendChild(sub);
    row.appendChild(meta);
    row.appendChild(el("div", "box", "✓"));

    const toggle = () => {
      if (selected.has(it.url)) selected.delete(it.url);
      else selected.add(it.url);
      render();
    };
    row.addEventListener("click", toggle);
    row.addEventListener("keydown", e => {
      if (e.key === " " || e.key === "Enter") { e.preventDefault(); toggle(); }
    });
    wrap.appendChild(row);
  }
  updateSummary();
}

function updateSummary() {
  const chosen = items.filter(i => selected.has(i.url));
  const count = chosen.length;
  const bytes = chosen.reduce((a, i) => a + (i.size || 0), 0);
  $("summary").textContent = items.length
    ? `${count} of ${items.length} selected` + (bytes ? ` · ${fmtSize(bytes)}` : "")
    : "";
  $("all").disabled = !items.length;
  $("all").checked = items.length > 0 && count === items.length;
  $("all").indeterminate = count > 0 && count < items.length;
  const btn = $("download");
  if (!btn.classList.contains("done")) {
    btn.disabled = count === 0 || !hostOk;
    btn.textContent = count === 0 ? "Download" : `Download ${count}`;
  }
  // the page itself (yt-dlp sites) - always offered for web pages
  const showPage = scanDone && isHttp(currentPageUrl);
  $("pageRow").hidden = !showPage;
  $("sendPage").disabled = !hostOk;
  if (showPage) {
    const platform = PLATFORM_RE.test(hostOf(currentPageUrl));
    $("pageRowTitle").textContent = platform
      ? "Download this video with MediaGrab"
      : "Download this page's video";
    $("pageRowHint").textContent = platform
      ? "Opens the quality picker in the app"
      : "MediaGrab resolves the page with yt-dlp (1000+ sites)";
  }
}

// ── scan + probe ───────────────────────────────────────────────────────────

async function probeSizes() {
  const pending = items.filter(i => i.kind === "file" && i.size == null && !i.probed);
  if (!pending.length || !hostOk) return;
  const resp = await native({ command: "probe", urls: pending.map(i => i.url),
                              referer: currentPageUrl });
  for (const it of pending) {
    it.probed = true;
    if (resp && resp.ok && resp.sizes && resp.sizes[it.url] != null) {
      it.size = resp.sizes[it.url];
    }
  }
  render();
}

async function targetTab() {
  // ?tab=<id> scans a specific tab (pop-out window / automated tests)
  const wanted = Number(new URLSearchParams(location.search).get("tab"));
  if (wanted) {
    try { return await chrome.tabs.get(wanted); } catch (e) { return null; }
  }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  return tab || null;
}

async function scan() {
  const tab = await targetTab();
  currentPageUrl = (tab && tab.url) || "";
  currentPageTitle = (tab && tab.title) || "";
  if (!tab || !tab.id || !isHttp(currentPageUrl)) {
    scanDone = true;
    render();
    return;
  }
  let frames = [];
  try {
    frames = await chrome.scripting.executeScript({
      target: { tabId: tab.id, allFrames: true }, files: ["scanner.js"]
    });
  } catch (e) {
    try {   // some frame refused injection: the top frame alone still helps
      frames = await chrome.scripting.executeScript({
        target: { tabId: tab.id }, files: ["scanner.js"]
      });
    } catch (e2) {
      scanDone = true;
      $("items").replaceChildren(placeholder("Cannot scan this page",
        String((e2 && e2.message) || e2)));
      updateSummary();
      return;
    }
  }
  const seen = new Set();
  const found = [];
  // top frame first so its titles/posters win when a url shows up twice
  frames.sort((a, b) => (a.frameId || 0) - (b.frameId || 0));
  for (const frame of frames) {
    const res = frame && frame.result;
    if (!res || !Array.isArray(res.items)) continue;
    if (res.top && res.pageTitle) currentPageTitle = res.pageTitle;
    for (const i of res.items) {
      if (!i || !isHttp(i.url) || seen.has(i.url)) continue;
      seen.add(i.url);
      found.push({
        url: i.url, kind: i.kind === "stream" ? "stream" : "file",
        quality: String(i.quality || ""), poster: String(i.poster || ""),
        title: String(i.title || ""), size: null, probed: false, name: ""
      });
    }
  }
  items = found.slice(0, 100);
  assignNames();
  // a page that offers plain files AND their stream playlists: the files are
  // what people want, so streams start unticked
  const hasFiles = items.some(i => i.kind === "file");
  for (const it of items) {
    if (it.kind === "file" || !hasFiles) selected.add(it.url);
  }
  scanDone = true;
  render();
  probeSizes();
}

// ── sending ────────────────────────────────────────────────────────────────

async function send(urls, extra) {
  const payload = Object.assign({ command: "download", urls }, extra || {});
  const dest = $("dest").value.trim();
  if (dest) payload.destination = dest;
  // the source page as Referer - tiered CDNs 403 without it
  if (isHttp(currentPageUrl) && !(urls.length === 1 && urls[0] === currentPageUrl)) {
    payload.referer = currentPageUrl;
  }
  chrome.storage.local.set({ dest });
  return native(payload);
}

function report(btn, resp, total) {
  const status = $("hostStatus");
  if (resp && resp.ok) {
    const n = resp.spawned || 0;
    if (resp.handoff) {
      btn.textContent = "Sent ✓";
      btn.classList.add("done");
      status.className = "ok";
      status.textContent = `sent ${n} to MediaGrab — confirm name & folder there`;
      setTimeout(() => window.close(), 900);
      return;
    }
    if (n > 0) {
      btn.textContent = "Queued ✓";
      btn.classList.add("done");
      status.className = "ok";
      status.textContent = `queued ${n} of ${total} — watch them in the MediaGrab app`;
      setTimeout(() => window.close(), 900);
      return;
    }
  }
  btn.classList.add("failed");
  btn.textContent = "Failed — retry";
  btn.disabled = false;
  status.className = "bad";
  status.textContent = (resp && resp.error)
    ? "MediaGrab error: " + String(resp.error).slice(0, 120)
    : "MediaGrab could not start the download";
}

async function downloadSelected() {
  const chosen = items.filter(i => selected.has(i.url));
  if (!chosen.length) return;
  const btn = $("download");
  btn.disabled = true;
  btn.classList.remove("failed");
  btn.textContent = "Sending…";
  const titles = {};
  const kinds = {};
  for (const it of chosen) {
    if (it.name) titles[it.url] = it.name;
    kinds[it.url] = it.kind;
  }
  report(btn, await send(chosen.map(i => i.url), { titles, kinds }), chosen.length);
}

async function sendPage() {
  if (!isHttp(currentPageUrl)) return;
  const btn = $("sendPage");
  btn.disabled = true;
  btn.textContent = "Sending…";
  const name = cleanName(currentPageTitle);
  const resp = await send([currentPageUrl], {
    titles: name ? { [currentPageUrl]: name } : {},
    kinds: { [currentPageUrl]: "page" }
  });
  report(btn, resp, 1);
  if (!(resp && resp.ok)) btn.textContent = "Retry";
}

// ── host status ────────────────────────────────────────────────────────────

function showNotice(parts) {
  const box = $("notice");
  box.replaceChildren();
  for (const part of parts) {
    box.appendChild(typeof part === "string"
      ? document.createTextNode(part) : el("code", null, part.code));
  }
  box.hidden = false;
}

async function checkHost() {
  const resp = await native({ command: "ping" });
  const st = $("hostStatus");
  if (resp && resp.ok) {
    hostOk = true;
    st.textContent = "MediaGrab engine connected";
    st.className = "ok";
  } else {
    hostOk = false;
    const detail = resp && resp.error ? String(resp.error) : "";
    st.className = "bad";
    if (/not found|Invalid|forbidden/i.test(detail)) {
      st.textContent = /forbidden/i.test(detail)
        ? "MediaGrab is linked to a different extension id"
        : "MediaGrab is not linked to this browser yet";
      showNotice(["Open the MediaGrab app and choose ", { code: "Browser Extension ▸ Connect" },
                  " (or run ", { code: "zsh cli/script/install_chrome_host.sh" },
                  "), then quit the browser completely (⌘Q) and reopen it."]);
    } else {
      st.textContent = detail ? "MediaGrab error: " + detail.slice(0, 90)
                              : "MediaGrab is not responding";
      showNotice(["Quit the browser completely (⌘Q) and reopen it. If this keeps happening, see ",
                  { code: "~/.config/mgd/native-host.log" }, "."]);
    }
  }
  updateSummary();
}

// ── wiring ─────────────────────────────────────────────────────────────────

$("all").addEventListener("change", e => {
  if (e.target.checked) items.forEach(i => selected.add(i.url));
  else selected.clear();
  render();
});
$("download").addEventListener("click", downloadSelected);
$("sendPage").addEventListener("click", sendPage);
$("openApp").addEventListener("click", () => native({ command: "open" }));

chrome.storage.sync.get({ intercept: false, useLogins: true }, v => {
  $("intercept").checked = !!(v && v.intercept);
  $("useLogins").checked = !(v && v.useLogins === false);
});
$("useLogins").addEventListener("change", e => {
  chrome.storage.sync.set({ useLogins: e.target.checked });
});
chrome.storage.local.get({ dest: "" }, v => {
  if (v && v.dest) { $("dest").value = v.dest; $("destBox").open = true; }
});
$("intercept").addEventListener("change", async e => {
  const on = e.target.checked;
  await native({ command: "watch", on });
  $("activeInfo").textContent = on ? "browser downloads go to MediaGrab" : "";
});

async function loadActiveInfo() {
  if (!hostOk) return;
  const resp = await native({ command: "query" });
  if (!resp || !resp.ok) return;
  const list = resp.items || [];
  const running = list.filter(i => i.state === "running");
  if (running.length) {
    const done = running.reduce((a, i) => a + (i.done || 0), 0);
    const size = running.reduce((a, i) => a + (i.size || 0), 0);
    const pct = size ? ` · ${Math.round(done * 100 / size)}%` : "";
    $("activeInfo").textContent = `⇣ ${running.length} downloading${pct}`;
  } else if (list.length) {
    $("activeInfo").textContent = `${list.length} paused in MediaGrab`;
  }
}

checkHost().then(scan).then(loadActiveInfo);
