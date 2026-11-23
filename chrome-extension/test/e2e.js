#!/usr/bin/env node
/**
 * End-to-end test of the MediaGrab extension in a real Chromium:
 * loads the unpacked extension into a throwaway profile, registers a
 * sandboxed native host (its own config dir + download root, never opens
 * the app), serves test pages, and drives the popup, the context-menu
 * handler and download interception.
 *
 *   node chrome-extension/test/e2e.js [--shots <dir>]
 *
 * Needs puppeteer-core (repo root: npm install) and a Chromium that still
 * accepts --load-extension: "Chrome for Testing" (npx puppeteer browsers
 * install chrome) or Chromium. Point CHROME_BIN at it to override.
 * Nothing here touches the real browser profile, ~/.config/mgd or
 * ~/Downloads.
 */
"use strict";

const fs = require("fs");
const os = require("os");
const path = require("path");
const http = require("http");

const EXT_SOURCE = path.resolve(__dirname, "..");
const REPO = path.resolve(EXT_SOURCE, "..");
// The browser under test loads the extension and runs the engine from
// copies inside the temp sandbox (set up in the run below): macOS keeps
// apps it has not been told about out of ~/Desktop, ~/Documents and
// ~/Downloads, which is where a checkout often lives.
let EXT_DIR = EXT_SOURCE;
let MGD = path.join(REPO, "cli", "bin", "mgd");

function stageCopies(tmp) {
  EXT_DIR = path.join(tmp, "extension");
  fs.cpSync(EXT_SOURCE, EXT_DIR, {
    recursive: true,
    filter: src => path.basename(src) !== "test"
  });
  const engine = path.join(tmp, "engine");
  fs.cpSync(path.join(REPO, "cli", "bin"), path.join(engine, "bin"), { recursive: true });
  fs.cpSync(path.join(REPO, "cli", "mgd"), path.join(engine, "mgd"), {
    recursive: true,
    filter: src => path.basename(src) !== "__pycache__"
  });
  MGD = path.join(engine, "bin", "mgd");
}
const HOST = "com.alirizwantariq.mediagrab";
const EXT_ID = "hgebmpanpoiebnbnmabafnpmkkmdgdmn";

const argv = process.argv.slice(2);
const SHOTS = argv.includes("--shots") ? path.resolve(argv[argv.indexOf("--shots") + 1]) : null;

function findChrome() {
  if (process.env.CHROME_BIN) return process.env.CHROME_BIN;
  const cache = path.join(os.homedir(), ".cache", "puppeteer", "chrome");
  if (fs.existsSync(cache)) {
    for (const ver of fs.readdirSync(cache).sort().reverse()) {
      const hits = [
        "chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-mac-x64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
        "chrome-linux64/chrome"
      ].map(p => path.join(cache, ver, p)).filter(p => fs.existsSync(p));
      if (hits.length) return hits[0];
    }
  }
  const chromium = "/Applications/Chromium.app/Contents/MacOS/Chromium";
  return fs.existsSync(chromium) ? chromium : null;
}

// ── tiny assertion kit ─────────────────────────────────────────────────────
let passed = 0;
const failures = [];
function check(name, cond, detail) {
  if (cond) { passed++; console.log("PASS " + name); }
  else { failures.push(name); console.log("FAIL " + name + (detail ? "\n     " + detail : "")); }
}
const sleep = ms => new Promise(r => setTimeout(r, ms));
async function until(fn, ms = 10000, step = 100) {
  const end = Date.now() + ms;
  for (;;) {
    const v = await fn();
    if (v) return v;
    if (Date.now() > end) return v;
    await sleep(step);
  }
}

// ── test site ──────────────────────────────────────────────────────────────
const MEDIA = Buffer.alloc(3 * 1024 * 1024, 7);        // "video" bytes
const BIG = Buffer.alloc(2 * 1024 * 1024, 9);          // download to intercept

function serveBytes(req, res, body, type, extra) {
  const range = /bytes=(\d+)-(\d*)/.exec(req.headers.range || "");
  const headers = Object.assign({ "Content-Type": type, "Accept-Ranges": "bytes" }, extra || {});
  if (range) {
    const start = Number(range[1]);
    const end = range[2] ? Number(range[2]) : body.length - 1;
    res.writeHead(206, Object.assign(headers, {
      "Content-Range": `bytes ${start}-${end}/${body.length}`,
      "Content-Length": end - start + 1
    }));
    res.end(body.subarray(start, end + 1));
  } else {
    res.writeHead(200, Object.assign(headers, { "Content-Length": body.length }));
    res.end(body);
  }
}

function startSite() {
  const server = http.createServer((req, res) => {
    const url = new URL(req.url, "http://x");
    const port = server.address().port;
    const html = body => { res.writeHead(200, { "Content-Type": "text/html; charset=utf-8" }); res.end(body); };
    if (url.pathname === "/album") {
      // the site's login: members-only media needs this cookie
      res.setHeader("Set-Cookie", "session=letmein; Path=/; HttpOnly");
      // the frame is another origin (localhost vs 127.0.0.1), like embeds
      return html(`<!doctype html><html><head>
<title>Summer trip 2026 &lt;b&gt;album&lt;/b&gt; | TestTube</title>
<meta property="og:title" content="Summer trip 2026">
</head><body>
<video poster="/poster.png" title="&lt;img src=x onerror=window.pwned=1&gt; beach day">
  <source src="/media/beach_720p.mp4?sig=a%2Fb" res="720">
  <source src="/media/beach_480p.mp4?sig=c" res="480">
</video>
<script>var flashvars = { video_id: '77', video_title: 'Mountain hike', preview_url: '/poster.png', video_url: 'http://127.0.0.1:${port}/media/hike.mp4', video_url_text: '1080p', video_alt_url: 'http://127.0.0.1:${port}/media/hike_low.mp4', video_alt_url_text: '360p' };</script>
<a href="/media/song.mp3">soundtrack</a>
<video src="/members/vip_clip.mp4" title="Members only"></video>
<a href="/files/big.zip" id="dl">archive</a>
<a href="/files/tiny.zip" id="dlfast">small archive</a>
<iframe src="http://localhost:${port}/embed" width="300" height="200"></iframe>
<script>fetch('/hls/master.m3u8').catch(()=>{});</script>
</body></html>`);
    }
    if (url.pathname === "/embed") {
      return html(`<!doctype html><title>embed</title><video src="/media/embedded_clip.mp4"></video>`);
    }
    if (url.pathname === "/watch") {
      return html(`<!doctype html><title>A JS player page</title><div id="player"></div>`);
    }
    if (url.pathname === "/poster.png") {
      const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==", "base64");
      res.writeHead(200, { "Content-Type": "image/png", "Content-Length": png.length });
      return res.end(png);
    }
    if (url.pathname === "/hls/master.m3u8") {
      res.writeHead(200, { "Content-Type": "application/vnd.apple.mpegurl" });
      return res.end("#EXTM3U\n#EXT-X-VERSION:3\n");
    }
    if (url.pathname.startsWith("/media/")) return serveBytes(req, res, MEDIA, "video/mp4");
    if (url.pathname.startsWith("/members/")) {
      if (!/session=letmein/.test(req.headers.cookie || "")) { res.writeHead(403); return res.end(); }
      return serveBytes(req, res, MEDIA, "video/mp4");
    }
    if (url.pathname === "/files/big.zip") {
      // trickled, like a real remote file: still in progress when the
      // extension gets to it
      res.writeHead(200, { "Content-Type": "application/zip", "Content-Length": BIG.length,
                           "Content-Disposition": 'attachment; filename="big.zip"' });
      let sent = 0;
      const timer = setInterval(() => {
        if (sent >= BIG.length || res.destroyed) { clearInterval(timer); res.end(); return; }
        res.write(BIG.subarray(sent, sent + 64 * 1024));
        sent += 64 * 1024;
      }, 150);
      res.on("close", () => clearInterval(timer));
      return;
    }
    if (url.pathname === "/files/tiny.zip") {
      return serveBytes(req, res, BIG.subarray(0, 700 * 1024), "application/zip",
                        { "Content-Disposition": 'attachment; filename="tiny.zip"' });
    }
    res.writeHead(404); res.end("nope");
  });
  return new Promise(resolve => server.listen(0, "127.0.0.1", () => resolve(server)));
}

// ── sandboxed native host ──────────────────────────────────────────────────
function installHost(profile, sandbox) {
  const launcher = path.join(sandbox, "mgd-native-host");
  fs.writeFileSync(launcher, `#!/bin/sh
export XDG_CONFIG_HOME="${path.join(sandbox, "config")}"
export MGD_ROOT="${path.join(sandbox, "downloads")}"
export MGD_NO_OPEN=1
exec "${process.env.MGD_PYTHON || "python3"}" "${MGD}" native-host 2>>"${path.join(sandbox, "host-stderr.log")}"
`, { mode: 0o755 });
  const dir = path.join(profile, "NativeMessagingHosts");
  fs.mkdirSync(dir, { recursive: true });
  fs.writeFileSync(path.join(dir, HOST + ".json"), JSON.stringify({
    name: HOST, description: "MediaGrab test host", path: launcher, type: "stdio",
    allowed_origins: [`chrome-extension://${EXT_ID}/`]
  }));
}

function readHandoff(sandbox) {
  const p = path.join(sandbox, "config", "mgd", "handoff.json");
  try { return JSON.parse(fs.readFileSync(p, "utf8")); } catch (e) { return null; }
}
function clearHandoff(sandbox) {
  try { fs.unlinkSync(path.join(sandbox, "config", "mgd", "handoff.json")); } catch (e) { /* none */ }
}

// detached=true keeps the debugger off the extension's service worker: an
// inspected worker is never stopped/restarted the way a real one is.
async function launch(puppeteer, chrome, profile, detached) {
  return puppeteer.launch({
    executablePath: chrome,
    headless: true,
    userDataDir: profile,
    ignoreDefaultArgs: ["--disable-extensions"],
    targetFilter: detached ? (t => t.type() !== "service_worker") : undefined,
    args: [`--disable-extensions-except=${EXT_DIR}`, `--load-extension=${EXT_DIR}`,
           "--no-first-run", "--no-default-browser-check", "--window-size=900,700"]
  });
}

async function worker(browser) {
  const isOurs = t => t.type() === "service_worker" &&
    t.url().startsWith(`chrome-extension://${EXT_ID}/`);
  let target = await browser.waitForTarget(isOurs, { timeout: 8000 }).catch(() => null);
  if (!target) {
    // a fresh headless profile sometimes leaves the worker asleep after
    // install: an extension page messaging it wakes it up
    const poke = await browser.newPage();
    await poke.goto(`chrome-extension://${EXT_ID}/popup.html?tab=0`).catch(() => {});
    target = await browser.waitForTarget(isOurs, { timeout: 20000 });
    await poke.close().catch(() => {});
  }
  return target.worker();
}

async function openPopup(browser, tabId) {
  const page = await browser.newPage();
  await page.setViewport({ width: 440, height: 540, deviceScaleFactor: 2 });
  const errors = [];
  page.on("pageerror", e => errors.push(String(e)));
  page.on("console", m => { if (m.type() === "error") errors.push(m.text()); });
  await page.goto(`chrome-extension://${EXT_ID}/popup.html?tab=${tabId}`);
  page.mgdErrors = errors;
  return page;
}

async function shot(page, name) {
  if (!SHOTS) return;
  fs.mkdirSync(SHOTS, { recursive: true });
  await page.screenshot({ path: path.join(SHOTS, name + ".png") });
}

async function tabIdOf(sw, urlPart) {
  return sw.evaluate(async part => {
    const tabs = await chrome.tabs.query({});
    const t = tabs.find(x => (x.url || "").includes(part));
    return t ? t.id : null;
  }, urlPart);
}

// ── the run ────────────────────────────────────────────────────────────────
(async () => {
  const chrome = findChrome();
  let puppeteer;
  try { puppeteer = require("puppeteer-core"); } catch (e) {
    console.log("SKIP: puppeteer-core is not installed (run `npm install` in the repo root)");
    process.exit(0);
  }
  if (!chrome) {
    console.log("SKIP: no Chromium that accepts --load-extension (npx puppeteer browsers install chrome, or set CHROME_BIN)");
    process.exit(0);
  }
  const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "mgd-ext-e2e-"));
  const profile = path.join(tmp, "profile");
  const sandbox = path.join(tmp, "sandbox");
  fs.mkdirSync(profile, { recursive: true });
  fs.mkdirSync(sandbox, { recursive: true });
  stageCopies(tmp);
  installHost(profile, sandbox);
  const site = await startSite();
  const base = `http://127.0.0.1:${site.address().port}`;
  let browser;
  try {
    browser = await launch(puppeteer, chrome, profile);
    const sw = await worker(browser);
    check("extension loads with its fixed id", !!sw);

    // ── popup on a page full of media ─────────────────────────────────────
    const album = await browser.newPage();
    await album.goto(base + "/album", { waitUntil: "networkidle0" });
    const albumTab = await tabIdOf(sw, "/album");
    let popup = await openPopup(browser, albumTab);
    await until(() => popup.evaluate(() => document.querySelectorAll(".item").length > 0));
    const status = await until(() => popup.evaluate(() => {
      const s = document.getElementById("hostStatus");
      return s.className ? { cls: s.className, text: s.textContent } : null;
    }));
    check("popup reaches the native host", status && status.cls === "ok", JSON.stringify(status));
    // sizes arrive from the probe
    await until(() => popup.evaluate(() =>
      [...document.querySelectorAll(".item .s")].some(n => /MB/.test(n.textContent))), 15000);
    const rows = await popup.evaluate(() => [...document.querySelectorAll(".item")].map(r => ({
      title: r.querySelector(".t").textContent,
      sub: r.querySelector(".s").textContent,
      on: r.classList.contains("on"),
      url: r.title,
      chips: [...r.querySelectorAll(".q")].map(q => q.textContent),
      hasImg: !!r.querySelector("img.thumb")
    })));
    const byFile = name => rows.find(r => r.url.includes(name));
    check("finds <source> elements with their res labels",
          !!byFile("beach_720p") && byFile("beach_720p").chips.includes("720p") &&
          !!byFile("beach_480p") && byFile("beach_480p").chips.includes("480p"), JSON.stringify(rows));
    check("finds the KVS flashvars qualities with their title",
          !!byFile("/hike.mp4") && byFile("/hike.mp4").title === "Mountain hike 1080p" &&
          byFile("/hike.mp4").chips.includes("1080p") &&
          !!byFile("hike_low") && byFile("hike_low").title === "Mountain hike 360p",
          JSON.stringify(rows.map(r => [r.url, r.title, r.chips])));
    check("finds media inside a cross-origin iframe", !!byFile("embedded_clip"));
    check("finds plain links to media files", !!byFile("song.mp3"));
    check("lists the HLS playlist as a stream, unticked next to real files",
          !!byFile("master.m3u8") && byFile("master.m3u8").chips.includes("STREAM") &&
          byFile("master.m3u8").on === false && /HLS/.test(byFile("master.m3u8").sub),
          JSON.stringify(byFile("master.m3u8")));
    check("probed sizes are shown (3.1 MB)", /3\.1 MB/.test(byFile("beach_720p").sub), byFile("beach_720p").sub);
    check("members-only media is sized with the site login",
          !!byFile("vip_clip") && /3\.1 MB/.test(byFile("vip_clip").sub), JSON.stringify(byFile("vip_clip")));
    check("posters render as thumbnails", byFile("beach_720p").hasImg);
    check("names fall back to the page title, not the url's file name",
          byFile("embedded_clip").title === "Summer trip 2026" &&
          byFile("master.m3u8").title === "Summer trip 2026 stream",
          JSON.stringify([byFile("embedded_clip").title, byFile("master.m3u8").title]));
    const injected = await popup.evaluate(() => ({
      imgs: document.querySelectorAll(".item .t img, .item .s img").length,
      pwned: window.pwned === 1,
      text: [...document.querySelectorAll(".item .t")].map(n => n.textContent).find(t => t.includes("beach day")) || ""
    }));
    check("page-supplied HTML is shown as text, never parsed",
          injected.imgs === 0 && !injected.pwned && injected.text.includes("img src=x"),
          JSON.stringify(injected));
    const summary = await popup.evaluate(() => document.getElementById("summary").textContent);
    check("summary counts the selection and its total size", /^7 of 8 selected · \d+\.\d MB$/.test(summary), summary);
    check("popup has no script errors", popup.mgdErrors.length === 0, popup.mgdErrors.join(" | "));
    await shot(popup, "popup-album");

    // deselect one, download the rest
    await popup.evaluate(() => [...document.querySelectorAll(".item")]
      .find(r => r.title.includes("song.mp3")).click());
    await popup.click("#download");
    const handoff = await until(() => readHandoff(sandbox), 10000);
    check("Download hands the ticked items to the app",
          handoff && handoff.urls.length === 6 && !handoff.urls.some(u => u.includes("song.mp3")) &&
          !handoff.urls.some(u => u.includes("m3u8")), JSON.stringify(handoff && handoff.urls));
    const beach = handoff && handoff.items.find(i => i.url.includes("beach_720p"));
    check("each item carries referer, kind and a clean suggested name",
          beach && beach.referer === base + "/album" && beach.kind === "file" &&
          !/[\\/:<>]/.test(beach.name) && /beach day 720p$/.test(beach.name),
          JSON.stringify(beach));
    const vip = handoff && handoff.items.find(i => i.url.includes("vip_clip"));
    const cookieText = vip && vip.cookies && fs.existsSync(vip.cookies) ? fs.readFileSync(vip.cookies, "utf8") : "";
    check("the site login goes along as a private cookie file (never inside handoff.json)",
          /session\tletmein/.test(cookieText) &&
          (fs.statSync(vip.cookies).mode & 0o777) === 0o600 &&
          !JSON.stringify(handoff).includes("letmein"), JSON.stringify(vip));
    const btn = await popup.evaluate(() => document.getElementById("download").textContent).catch(() => "closed");
    check("the button confirms the hand-off", btn === "Sent ✓" || btn === "closed", btn);
    await popup.close().catch(() => {});
    clearHandoff(sandbox);

    // ── context menu (handler invoked the way Chrome would) ────────────────
    const menu = await sw.evaluate(async (pageUrl, link) => onMenuClicked(
      { menuItemId: "mgd-link", linkUrl: link, pageUrl }, { url: pageUrl }),
      base + "/album", base + "/media/from_menu.mp4");
    const h2 = readHandoff(sandbox);
    check("right-click › Download link sends the link with the page as referer",
          menu && menu.ok && h2 && h2.urls[0].endsWith("from_menu.mp4") && h2.items[0].referer === base + "/album",
          JSON.stringify([menu, h2]));
    clearHandoff(sandbox);
    await sw.evaluate(() => new Promise(r => chrome.storage.sync.set({ useLogins: false }, r)));
    await sw.evaluate(async (pageUrl, link) => onMenuClicked(
      { menuItemId: "mgd-link", linkUrl: link, pageUrl }, { url: pageUrl }),
      base + "/album", base + "/members/vip_clip.mp4");
    const noLogin = readHandoff(sandbox);
    check("with “Use my site logins” off, no cookies are handed over",
          noLogin && noLogin.items.length === 1 && noLogin.items[0].cookies === null, JSON.stringify(noLogin));
    await sw.evaluate(() => new Promise(r => chrome.storage.sync.set({ useLogins: true }, r)));
    clearHandoff(sandbox);
    await sw.evaluate(async (pageUrl, link) => onMenuClicked(
      { menuItemId: "mgd-link", linkUrl: link, pageUrl }, { url: pageUrl }),
      base + "/album", base + "/media/from_menu.mp4");
    const blob = await sw.evaluate(async pageUrl => onMenuClicked(
      { menuItemId: "mgd-media", srcUrl: "blob:https://x/123", pageUrl }, { url: pageUrl }),
      base + "/watch");
    const h3 = readHandoff(sandbox);
    check("a blob: video (MediaSource player) falls back to the page url, merged into the pending batch",
          blob && blob.ok && h3 && h3.urls.length === 2 && h3.urls[1] === base + "/watch", JSON.stringify(h3 && h3.urls));
    clearHandoff(sandbox);

    // ── a page with nothing to pick ────────────────────────────────────────
    const watch = await browser.newPage();
    await watch.goto(base + "/watch", { waitUntil: "networkidle0" });
    popup = await openPopup(browser, await tabIdOf(sw, "/watch"));
    await until(() => popup.evaluate(() => !document.getElementById("pageRow").hidden));
    const empty = await popup.evaluate(() => ({
      text: document.getElementById("items").textContent,
      pageRow: !document.getElementById("pageRow").hidden,
      dl: document.getElementById("download").disabled
    }));
    check("an empty scan explains itself and offers the page",
          /No media files found/.test(empty.text) && empty.pageRow && empty.dl, JSON.stringify(empty));
    await shot(popup, "popup-empty");
    await popup.click("#sendPage");
    const h4 = await until(() => readHandoff(sandbox), 10000);
    check("Send page hands the page url over as kind=page with the page title",
          h4 && h4.items.length === 1 && h4.items[0].url === base + "/watch" &&
          h4.items[0].kind === "page" && h4.items[0].name === "A JS player page" && h4.items[0].referer === null,
          JSON.stringify(h4));
    await popup.close().catch(() => {});
    clearHandoff(sandbox);

    await browser.close();
    browser = null;

    // ── download interception, across a service-worker restart ─────────────
    // Driven from an extension page (same chrome.* APIs) with no debugger on
    // the worker, so it really stops and has to be woken by the download.
    browser = await launch(puppeteer, chrome, profile, true);
    const site2 = await browser.newPage();
    await site2.goto(base + "/album", { waitUntil: "domcontentloaded" });
    const ctl = await browser.newPage();
    await ctl.goto(`chrome-extension://${EXT_ID}/popup.html?tab=0`);
    const cdp = await site2.createCDPSession();
    await cdp.send("Browser.setDownloadBehavior", {
      behavior: "allow", downloadPath: path.join(tmp, "browser-downloads") }).catch(() => {});
    const browserFiles = () => (fs.existsSync(path.join(tmp, "browser-downloads"))
      ? fs.readdirSync(path.join(tmp, "browser-downloads")).filter(f => !f.endsWith(".crdownload")) : []);
    const downloads = () => ctl.evaluate(() => new Promise(r =>
      chrome.downloads.search({}, d => r(d.map(x => ({ state: x.state, url: x.url }))))));
    const setIntercept = on => ctl.evaluate(v => new Promise(r =>
      chrome.storage.sync.set({ intercept: v }, r)), on);
    const startDownload = async id => { await site2.bringToFront(); await site2.click("#" + id); };

    await setIntercept(false);
    await startDownload("dl");
    await until(() => browserFiles().includes("big.zip"), 15000);
    check("with the toggle off, downloads are left to the browser",
          readHandoff(sandbox) === null && browserFiles().includes("big.zip"), browserFiles().join(","));
    fs.rmSync(path.join(tmp, "browser-downloads"), { recursive: true, force: true });
    await ctl.evaluate(() => new Promise(r => chrome.downloads.erase({}, r)));

    await setIntercept(true);
    await startDownload("dl");
    const h5 = await until(() => readHandoff(sandbox), 15000);
    check("a browser download is taken over when the toggle is on",
          h5 && h5.urls.length === 1 && h5.urls[0].endsWith("/files/big.zip") &&
          h5.items[0].referer === base + "/album", JSON.stringify(h5));
    check("the browser's own copy is cancelled and removed",
          (await until(async () => (await downloads()).length === 0 ? "gone" : null, 8000)) === "gone",
          JSON.stringify(await downloads()));
    await sleep(1500);
    check("nothing was saved by the browser itself", browserFiles().length === 0, browserFiles().join(","));
    clearHandoff(sandbox);

    // MV3 stops idle workers; the next download has to wake a fresh one and
    // still be intercepted (the listener must exist the moment it starts)
    const states = [];
    await cdp.send("ServiceWorker.enable");
    cdp.on("ServiceWorker.workerVersionUpdated", e => {
      for (const v of e.versions) if (v.scriptURL.includes(EXT_ID)) states.push(v.runningStatus);
    });
    await cdp.send("ServiceWorker.stopAllWorkers");
    const stopped = await until(() => states[states.length - 1] === "stopped", 8000);
    check("the service worker really stopped", !!stopped, states.join(">"));
    await startDownload("dl");
    const h6 = await until(() => readHandoff(sandbox), 15000);
    check("the first download after the worker went idle is still taken over",
          h6 && h6.urls.length === 1 && h6.urls[0].endsWith("/files/big.zip"),
          JSON.stringify(h6) + " worker: " + states.join(">"));
    await until(async () => (await downloads()).length === 0, 8000);
    await sleep(1200);
    check("...and the browser saved nothing that time either", browserFiles().length === 0, browserFiles().join(","));
    clearHandoff(sandbox);

    // a file that finishes before the extension can pause it stays with the
    // browser - handing it over as well would download it twice
    await startDownload("dlfast");
    await until(() => browserFiles().includes("tiny.zip"), 10000);
    await sleep(1500);
    check("a download the browser already finished is not downloaded twice",
          browserFiles().includes("tiny.zip") && readHandoff(sandbox) === null,
          JSON.stringify([browserFiles(), readHandoff(sandbox)]));
    await setIntercept(false);

    // ── no native host registered: the popup must say what to do ───────────
    const bare = path.join(tmp, "profile-no-host");
    fs.mkdirSync(bare, { recursive: true });
    browser = await launch(puppeteer, chrome, bare);
    const sw3 = await worker(browser);
    const page2 = await browser.newPage();
    await page2.goto(base + "/album", { waitUntil: "networkidle0" });
    popup = await openPopup(browser, await tabIdOf(sw3, "/album"));
    await until(() => popup.evaluate(() => document.getElementById("hostStatus").className === "bad"));
    await until(() => popup.evaluate(() => document.querySelectorAll(".item").length > 0));
    const offline = await popup.evaluate(() => ({
      status: document.getElementById("hostStatus").textContent,
      notice: document.getElementById("notice").hidden ? "" : document.getElementById("notice").textContent,
      items: document.querySelectorAll(".item").length,
      dl: document.getElementById("download").disabled
    }));
    check("without the host: clear status, setup instructions, scan still works, Download disabled",
          /not linked/.test(offline.status) && /Browser Extension/.test(offline.notice) &&
          offline.items === 8 && offline.dl, JSON.stringify(offline));
    await shot(popup, "popup-no-host");
  } catch (err) {
    failures.push("crashed: " + (err && err.stack || err));
    console.log("FAIL test run crashed\n" + (err && err.stack || err));
  } finally {
    if (browser) await browser.close().catch(() => {});
    site.close();
    const hostLog = path.join(sandbox, "host-stderr.log");
    if (failures.length && fs.existsSync(hostLog)) {
      const tail = fs.readFileSync(hostLog, "utf8").trim().split("\n").slice(-8).join("\n");
      if (tail) console.log("--- native host stderr ---\n" + tail);
    }
    fs.rmSync(tmp, { recursive: true, force: true });
  }
  console.log(`\n${passed} passed, ${failures.length} failed`);
  process.exit(failures.length ? 1 : 0);
})();
