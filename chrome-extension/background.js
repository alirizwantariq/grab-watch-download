// MediaGrab fetcher — service worker: the only place allowed to talk to
// the native messaging host (mgd native-host).
//
// Handles three sources of downloads:
//   1. popup batch ("Download N items")
//   2. right-click context menu on links / videos / media elements / the page
//   3. optional interception of the browser's own downloads
//      (FDM-style "take over downloads" - opt-in from the popup)
//
// MV3 note: this worker is stopped after ~30s of idling and restarted for
// the next event. Chrome only wakes it for listeners that were registered
// synchronously while the script first ran, so every listener below is
// added at the top level and reads its settings when it fires.

const HOST = "com.alirizwantariq.mediagrab";

function sendNative(payload) {
  return new Promise(resolve => {
    try {
      chrome.runtime.sendNativeMessage(HOST, payload, resp => {
        const err = chrome.runtime.lastError;
        resolve(err ? { ok: false, error: err.message } : (resp || { ok: false }));
      });
    } catch (e) {
      resolve({ ok: false, error: String((e && e.message) || e) });
    }
  });
}

function isHttp(url) {
  return typeof url === "string" && /^https?:\/\//i.test(url);
}

// ── site logins ────────────────────────────────────────────────────────────
// Downloads that need the user to be signed in (members-only videos, CDNs
// that check a session) work when the cookies the browser itself would send
// to those urls go along. Only cookies for the download urls and the page
// they came from are read; MediaGrab sends each one only to its own site.
// Switchable in the popup ("Use my site logins").

function loginsEnabled() {
  return new Promise(resolve => {
    chrome.storage.sync.get({ useLogins: true }, v => resolve(!(v && v.useLogins === false)));
  });
}

function cookiesFor(url) {
  return new Promise(resolve => {
    try {
      chrome.cookies.getAll({ url }, list => {
        void chrome.runtime.lastError;
        resolve(Array.isArray(list) ? list : []);
      });
    } catch (e) { resolve([]); }
  });
}

async function siteLogins(payload) {
  const urls = [...new Set([...(payload.urls || []), payload.referer].filter(isHttp))].slice(0, 40);
  const seen = new Set();
  const out = [];
  for (const list of await Promise.all(urls.map(cookiesFor))) {
    for (const c of list) {
      const key = `${c.domain}|${c.path}|${c.name}`;
      if (seen.has(key)) continue;
      seen.add(key);
      out.push({ name: c.name, value: c.value, domain: c.domain, hostOnly: c.hostOnly,
                 path: c.path, secure: c.secure, httpOnly: c.httpOnly,
                 session: c.session, expirationDate: c.expirationDate });
      if (out.length >= 400) return out;
    }
  }
  return out;
}

async function native(payload) {
  if (payload && (payload.command === "download" || payload.command === "probe") &&
      await loginsEnabled()) {
    const cookies = await siteLogins(payload);
    if (cookies.length) payload = Object.assign({}, payload, { cookies });
  }
  return sendNative(payload);
}

// ── context menus ──────────────────────────────────────────────────────────

function installMenus() {
  chrome.contextMenus.removeAll(() => {
    chrome.contextMenus.create({
      id: "mgd-link",
      title: "Download link with MediaGrab",
      contexts: ["link"]
    });
    chrome.contextMenus.create({
      id: "mgd-media",
      title: "Download with MediaGrab",
      contexts: ["video", "audio", "image"]
    });
    chrome.contextMenus.create({
      id: "mgd-page",
      title: "Download this page's video with MediaGrab",
      contexts: ["page", "frame"]
    });
  });
}

chrome.runtime.onInstalled.addListener(installMenus);
chrome.runtime.onStartup.addListener(installMenus);

// Which URL a context-menu click means. Players that stream through
// MediaSource expose a blob: src that cannot be fetched from outside the
// page; the page URL is what MediaGrab (yt-dlp) can resolve instead.
function menuTarget(info, tab) {
  const page = (tab && tab.url) || info.pageUrl || "";
  if (info.menuItemId === "mgd-link") return info.linkUrl || "";
  if (info.menuItemId === "mgd-media") {
    return isHttp(info.srcUrl) ? info.srcUrl : (info.linkUrl || page);
  }
  return page;
}

async function onMenuClicked(info, tab) {
  const url = menuTarget(info, tab);
  if (!isHttp(url)) { flashBadge(false); return { ok: false, error: "no link" }; }
  const page = (tab && tab.url) || info.pageUrl || "";
  const payload = { command: "download", urls: [url] };
  if (isHttp(page) && page !== url) payload.referer = page;
  const resp = await native(payload);
  flashBadge(!!(resp && resp.ok));
  return resp;
}

chrome.contextMenus.onClicked.addListener(onMenuClicked);

// ── optional: intercept the browser's own downloads (FDM-style) ────────────
// Enabled from the popup toggle. When on, a download the browser starts is
// paused, cancelled in the browser and handed to mgd (same segmented
// engine). A file is never fetched twice and never lost: if MediaGrab is
// unreachable the browser download simply continues, and a download the
// browser already finished is left alone.

const MIN_FILE_BYTES = 512 * 1024;   // ignore tiny files
const SKIP_EXT = /\.(html?|css|js|json|xml|txt|png|jpe?g|gif|webp|svg|ico|bmp)$/i;

function interceptEnabled() {
  return new Promise(resolve => {
    chrome.storage.sync.get({ intercept: false }, v => resolve(!!(v && v.intercept)));
  });
}

function dl(method, ...args) {
  return new Promise(resolve => {
    try {
      chrome.downloads[method](...args, () => {
        void chrome.runtime.lastError;   // e.g. already finished
        resolve();
      });
    } catch (e) { resolve(); }
  });
}

function worthIntercepting(item) {
  if (!item || item.state === "complete" || item.state === "interrupted") return null;
  // downloads another extension started on purpose are theirs to manage
  if (item.byExtensionId) return null;
  const url = item.finalUrl || item.url;
  if (!isHttp(url)) return null;                       // blob:, data:, file:
  let path = "";
  try { path = new URL(url).pathname; } catch (e) { return null; }
  if (SKIP_EXT.test(path)) return null;
  if (/^(text\/html|image\/)/i.test(item.mime || "")) return null;
  const size = Math.max(item.fileSize || 0, item.totalBytes || 0);
  if (size > 0 && size < MIN_FILE_BYTES) return null;  // unknown size passes
  return url;
}

function currentState(id) {
  return new Promise(resolve => {
    try {
      chrome.downloads.search({ id }, found => {
        void chrome.runtime.lastError;
        resolve(found && found[0] ? found[0].state : null);
      });
    } catch (e) { resolve(null); }
  });
}

async function intercept(item) {
  const url = worthIntercepting(item);
  if (!url) return;
  if (!(await interceptEnabled())) return;
  await dl("pause", item.id);
  // a small or cached file can finish before the pause lands: the browser
  // already has it, so handing it over as well would download it twice
  if ((await currentState(item.id)) !== "in_progress") return;
  // is MediaGrab reachable at all? if not, the browser simply carries on
  const ping = await native({ command: "ping" });
  if (!(ping && ping.ok)) {
    await dl("resume", item.id);
    flashBadge(false);
    return;
  }
  // stop the browser's copy for good *before* handing over; if it slipped
  // through to completion in the meantime, it stays the browser's download
  await dl("cancel", item.id);
  if ((await currentState(item.id)) === "complete") return;
  const payload = { command: "download", urls: [url] };
  if (isHttp(item.referrer)) payload.referer = item.referrer;
  const name = String(item.filename || "").split(/[\\/]/).pop();
  if (name) payload.titles = { [url]: name };
  const resp = await native(payload);
  if (resp && resp.ok) {
    await dl("erase", { id: item.id });
    flashBadge(true);
  } else {
    // the hand-off failed after the browser's copy was cancelled: start it
    // again in the browser (downloads this extension starts are never
    // intercepted) so the file is not lost
    await dl("erase", { id: item.id });
    await dl("download", { url });
    flashBadge(false);
  }
}

chrome.downloads.onCreated.addListener(intercept);

// ── messages from the popup ────────────────────────────────────────────────

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "native" || !msg.payload) return false;
  if (msg.payload.command === "watch") {
    // popup toggle: persist the choice (the listener above reads it)
    const on = !!msg.payload.on;
    chrome.storage.sync.set({ intercept: on }, () => sendResponse({ ok: true, on }));
    return true;
  }
  native(msg.payload).then(sendResponse);
  return true; // async response
});

// ── badge feedback ─────────────────────────────────────────────────────────

let flashTimer = null;
function flashBadge(ok) {
  chrome.action.setBadgeText({ text: ok ? "↓" : "!" });
  chrome.action.setBadgeBackgroundColor({ color: ok ? "#2f6df0" : "#d43c3c" });
  chrome.action.setTitle({
    title: ok ? "MediaGrab fetcher — sent to MediaGrab"
              : "MediaGrab fetcher — could not reach MediaGrab (open the popup for details)"
  });
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => {
    chrome.action.setBadgeText({ text: "" });
    chrome.action.setTitle({ title: "MediaGrab fetcher" });
  }, ok ? 2500 : 8000);
}
