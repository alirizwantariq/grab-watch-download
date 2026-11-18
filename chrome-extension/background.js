// MediaGrab fetcher — service worker: the only place allowed to talk to
// the native messaging host (mgd native-host).
//
// Handles three sources of downloads:
//   1. popup batch ("Download N items")
//   2. right-click context menu on links / videos / media elements
//   3. optional interception of the browser's own downloads
//      (FDM-style "take over downloads" - opt-in from the popup)

const HOST = "com.alirizwantariq.mediagrab";

function native(payload) {
  return new Promise(resolve => {
    chrome.runtime.sendNativeMessage(HOST, payload, resp => {
      const err = chrome.runtime.lastError;
      resolve(err ? { ok: false, error: err.message } : (resp || { ok: false }));
    });
  });
}

// ── context menus ──────────────────────────────────────────────────────────

chrome.runtime.onInstalled.addListener(() => {
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
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  const url = info.srcUrl || info.linkUrl || info.pageUrl;
  if (!url) return;
  await native({
    command: "download",
    urls: [url],
    referer: (tab && tab.url) || info.pageUrl || ""
  });
  flashBadge();
});

// ── optional: intercept the browser's own downloads (FDM-style) ────────────
// Enabled from the popup toggle. When on, every download the browser starts
// is cancelled and re-routed to mgd (same segmented engine).

const MIN_FILE_BYTES = 512 * 1024;   // ignore tiny files (pdfs/pages etc.)
const SKIP_EXT = /\.(html?|css|js|png|jpe?g|gif|webp|svg|ico)$/i;

chrome.storage.sync.get({ intercept: false }, ({ intercept }) => {
  if (intercept) watchDownloads(true);
});

chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  if (!msg || msg.type !== "native") return false;
  if (msg.payload && msg.payload.command === "watch") {
    // popup toggle: start/stop interception + persist the choice
    chrome.storage.sync.set({ intercept: msg.payload.on });
    watchDownloads(msg.payload.on);
    sendResponse({ ok: true, on: msg.payload.on });
    return true;
  }
  chrome.runtime.sendNativeMessage(HOST, msg.payload, resp => {
    const err = chrome.runtime.lastError;
    sendResponse(err ? { ok: false, error: err.message } : (resp || { ok: false }));
  });
  return true; // async response
});

let intercepting = false;

function watchDownloads(on) {
  if (on && !intercepting) {
    intercepting = true;
    chrome.downloads.onCreated.addListener(intercept);
  } else if (!on) {
    intercepting = false;
    chrome.downloads.onCreated.removeListener(intercept);
  }
}

async function intercept(item) {
  if (!intercepting || !item || !item.url) return;
  const url = item.url;
  if (!/^https?:/.test(url) || SKIP_EXT.test(url)) return;
  try {
    // total bytes unknown yet; skip when the header says it is tiny
    if (item.fileSize && item.fileSize < MIN_FILE_BYTES) return;
  } catch (e) { /* fileSize optional */ }
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  const resp = await native({
    command: "download",
    urls: [url],
    referer: (tab && tab.url) || item.referrer || ""
  });
  if (resp && resp.ok) {
    chrome.downloads.cancel(item.id);     // browser download stops
    chrome.downloads.erase({ id: item.id });
    flashBadge();
  }
}

// ── badge feedback ─────────────────────────────────────────────────────────

let flashTimer = null;
function flashBadge() {
  chrome.action.setBadgeText({ text: "↓" });
  chrome.action.setBadgeBackgroundColor({ color: "#2f6df0" });
  if (flashTimer) clearTimeout(flashTimer);
  flashTimer = setTimeout(() => chrome.action.setBadgeText({ text: "" }), 2500);
}
