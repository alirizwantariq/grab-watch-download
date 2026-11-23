# MediaGrab Chrome extension

Find every video on the current page — with previews, file sizes and quality
labels — and send the ones you pick to MediaGrab (the `mgd` engine:
segmented, resumable downloads).

## What it does

- **Scans** the active tab, including embedded frames, for media:
  `<video>`/`<audio>`/`<source>` elements (poster = preview, `res` = quality),
  KVS `flashvars` player configs, media the page already loaded
  (resource timing, including HLS/DASH playlists), `og:video` meta tags and
  plain links to media files.
- **Names** each item from its own title or the page title (qualities of one
  video become "Title 720p", "Title 480p"); the app offers that name in its
  "Save as" sheet.
- **Probes file sizes** through the native host (ranged requests — nothing
  is downloaded during the scan). Stream playlists are labelled `STREAM`.
- **Hands the selection to MediaGrab** with the page as `Referer`.
- **Uses your site logins** ("Use my site logins", on by default): the
  cookies the browser itself would send to the download urls and their page
  go along, so members-only videos and CDNs that check a session work. The
  engine keeps them in a private file (`~/.config/mgd/cookies/`, readable
  only by you, removed after two weeks) and sends each cookie only to the
  site it belongs to, redirects included.
- **Send page**: on sites whose player has no direct file (YouTube, X, Vimeo
  …) the page link itself goes to MediaGrab, which resolves it with yt-dlp
  and shows its quality picker.
- **Right-click menu**: "Download … with MediaGrab" on links, videos, audio,
  images and the page.
- **Take over browser downloads** (optional toggle): downloads the browser
  starts are cancelled there and handed to MediaGrab. Files under 512 KB and
  web resources are left alone; if MediaGrab is unreachable the browser
  download just continues, and a file is never fetched twice.

## Install (one time)

From the app: *⋯ → Browser Extension…* shows the extension folder to load
and connects your browsers.

From this repository:

```sh
# 1. register the native-messaging host for every Chromium browser found
zsh cli/script/install_chrome_host.sh

# 2. load the extension
#    chrome://extensions → Developer mode → Load unpacked
#    → <repo>/chrome-extension
```

Quit the browser completely (⌘Q) and reopen it after connecting. The
extension id is fixed by the `key` in `manifest.json`
(`hgebmpanpoiebnbnmabafnpmkkmdgdmn`), so the host registration keeps working
across browser restarts. Brave / Edge / Chromium / Arc / Vivaldi use the same
registration.

## Files

| file | role |
|---|---|
| `scanner.js` | injected into every frame; returns the media it finds |
| `background.js` | service worker: native host bridge, context menus, download takeover |
| `popup.html/css/js` | the picker UI |
| `manifest.json` | MV3 manifest with the stable extension key |
| `test/e2e.js` | end-to-end test in a real Chromium |

MV3 stops the service worker when idle and only restarts it for listeners
registered synchronously at start-up, so `background.js` registers every
listener at the top level and reads its settings when an event fires.
Everything that comes from the page is treated as untrusted and reaches the
popup's DOM only as text.

## Native host protocol (`mgd native-host`)

Chrome native messaging (4-byte little-endian length + JSON):

```
{"command":"ping"}                               → {"ok":true,"app":"mgd","version":"1.3"}
{"command":"probe","urls":[…],"referer":…,"cookies":[…]?}
                                                 → {"ok":true,"sizes":{url:bytes}}
{"command":"download","urls":[…],"referer":?,
 "titles":{url:name}?,"kinds":{url:"file|stream|page"}?,
 "cookies":[chrome.cookies.Cookie…]?,
 "destination":?}                                → {"ok":true,"handoff":true,"spawned":n}
{"command":"query"}                              → {"ok":true,"items":[{id,state,done,size,…}]}
{"command":"open"}                               → {"ok":true}   (opens the app)
```

`download` writes the batch to `~/.config/mgd/handoff.json` and opens
`mediagrab://handoff`; a batch that arrives before the app consumed the
previous one is merged into it. With `mgd set-default open-app off` the host
starts headless `mgd grab` processes instead.

## Test

```sh
npm install                      # repo root: puppeteer-core
npx puppeteer browsers install chrome   # a Chromium that accepts --load-extension
node chrome-extension/test/e2e.js [--shots <dir>]
```

Loads the extension into a throwaway profile with a sandboxed native host
and checks the scan (frames, qualities, streams, names, untrusted HTML), the
hand-off, the context menu, download takeover across a service-worker
restart, and the "host not connected" state. It does not touch the real
browser profile, `~/.config/mgd` or `~/Downloads`.
