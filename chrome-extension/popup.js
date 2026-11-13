// MediaGrab fetcher — popup: scan page → probe sizes via mgd → let the user
// pick items → hand the batch to mgd (native messaging).

const NATIVE = { type: "native" };
let items = [];
const selected = new Set();
let hostOk = false;
let currentPageUrl = "";

const $ = id => document.getElementById(id);

function fmtSize(n) {
  if (n == null) return "size ?";
  const u = [["GB", 1e9], ["MB", 1e6], ["KB", 1e3]];
  for (const [name, div] of u) if (n >= div) return (n / div).toFixed(1) + " " + name;
  return n + " B";
}

function native(payload) {
  return new Promise(resolve => {
    chrome.runtime.sendMessage({ type: "native", payload }, resolve);
  });
}

function render() {
  const wrap = $("items");
  wrap.innerHTML = "";
  if (!items.length) {
    wrap.innerHTML = '<div class="placeholder">No downloadable media found on this page.<br>(play the video first if it lazy-loads)</div>';
    updateSummary();
    return;
  }
  for (const it of items) {
    const el = document.createElement("div");
    el.className = "item" + (selected.has(it.url) ? " on" : "");
    const thumb = it.poster
      ? `<img src="${it.poster}" loading="lazy" alt="">`
      : `<div class="nothumb">▶</div>`;
    const q = it.quality ? `<span class="q">${it.quality}</span>` : "";
    el.innerHTML = `
      ${thumb}
      <div class="meta">
        <div class="t">${escapeHtml(it.title || decodeURIComponent(it.url.split("/").pop()))}</div>
        <div class="s">${q}<span class="size">${fmtSize(it.size)}</span> ${escapeHtml(shortUrl(it.url))}</div>
      </div>
      <div class="box">✓</div>`;
    el.onclick = () => {
      if (selected.has(it.url)) selected.delete(it.url);
      else selected.add(it.url);
      render();
    };
    wrap.appendChild(el);
  }
  updateSummary();
}

function updateSummary() {
  const count = selected.size;
  $("summary").textContent = count + " of " + items.length + " selected";
  $("download").disabled = count === 0 || !hostOk;
  $("download").textContent = count === 0 ? "Download selected" : `Download ${count}`;
}

function escapeHtml(s) {
  return (s || "").replace(/[&<>"']/g, c => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
  }[c]));
}

function shortUrl(u) {
  try {
    const url = new URL(u);
    return url.hostname + (url.pathname.length > 28 ? url.pathname.slice(0, 28) + "…" : url.pathname);
  } catch (e) { return u.slice(0, 40); }
}

async function probeSizes() {
  const urls = items.filter(i => i.size == null).map(i => i.url);
  if (!urls.length || !hostOk) return;
  const resp = await native({ command: "probe", urls, referer: currentPageUrl })
    .catch(() => null);
  if (resp && resp.ok && resp.sizes) {
    for (const it of items) {
      if (it.size == null && resp.sizes[it.url] != null) it.size = resp.sizes[it.url];
    }
    render();
  }
}

async function scan() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  if (!tab || !tab.id) { render(); return; }
  currentPageUrl = tab.url || "";
  try {
    const [res] = await chrome.scripting.executeScript({
      target: { tabId: tab.id }, files: ["scanner.js"]
    });
    let found = res && res.result;
    if (!found) {
      const [res2] = await chrome.scripting.executeScript({
        target: { tabId: tab.id },
        func: () => window.__mgdScan ? window.__mgdScan() : []
      });
      found = res2 && res2.result;
    }
    items = (found || []).map(i => ({
      url: i.url, quality: i.quality, poster: i.poster,
      title: i.title, size: null
    }));
    items.forEach(i => selected.add(i.url));
  } catch (e) {
    document.querySelector("#items").innerHTML =
      `<div class="placeholder">Cannot scan this page: ${escapeHtml(String(e && e.message || e))}</div>`;
    return;
  }
  render();
  probeSizes();
}

async function downloadSelected() {
  const urls = [...selected];
  const dest = $("dest").value.trim();
  const payload = { command: "download", urls };
  if (dest) payload.destination = dest;
  const resp = await native(payload);
  const btn = $("download");
  if (resp && resp.ok) {
    btn.textContent = "queued ✓";
    btn.classList.add("done");
    $("hostStatus").textContent = `sent ${resp.spawned || urls.length} download${urls.length === 1 ? "" : "s"} to mgd`;
    setTimeout(() => window.close(), 700);
  } else {
    btn.textContent = "failed — retry";
    $("hostStatus").textContent = (resp && resp.error) || "native host error";
  }
}

async function checkHost() {
  const resp = await native({ command: "ping" }).catch(() => null);
  const st = $("hostStatus");
  if (resp && resp.ok) {
    hostOk = true;
    st.textContent = "mgd engine connected";
    st.className = "ok";
  } else {
    st.textContent = "native host not installed — run cli/script/install_chrome_host.sh";
    st.className = "bad";
  }
  updateSummary();
}

$("all").onchange = e => {
  if (e.target.checked) items.forEach(i => selected.add(i.url));
  else selected.clear();
  render();
};
$("download").onclick = downloadSelected;
$("openApp").onclick = async () => { await native({ command: "open" }); };

checkHost().then(scan);
