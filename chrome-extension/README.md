# MediaGrab Chrome extension

Find every video on the current page — with previews, file sizes and quality
labels — and download the ones you pick with the `mgd` engine (segmented,
resumable, optimal-speed).

```
┌──────────────────────────────────────────────┐
│ [▶] MediaGrab fetcher     mgd engine connected │
│ [dest: ~/Downloads/mgd                     ] │
│ [thumb] vidA_720p  [720p] 363.3 MB  v9.x/…   ✓ │
│ [thumb] vidB_720p  [720p] 1.6 MB   v9.x/…    ✓ │
│ …                                            │
│ ☑ all   7 of 7 selected     [Download 7]     │
└──────────────────────────────────────────────┘
```

## What it does

- **Scans** the active tab for media:
  `<video>`/`<source>` elements (poster = preview, `res`/`videoHeight` =
  quality), KVS `flashvars` player configs (erome and most tube sites),
  resource-timing entries, and `og:video` meta tags.
- **Probes file sizes** through the native host (parallel ranged GETs —
  nothing is downloaded during the scan).
- **Hands the selected batch to mgd**, which downloads each file with its
  segmented engine — same behavior as the CLI and the MediaGrab app.

## Install (one time)

```sh
# 1. register the native-messaging host for every Chromium browser found
zsh cli/script/install_chrome_host.sh

# 2. load the extension
#    chrome://extensions → Developer mode → Load unpacked
#    → <repo>/chrome-extension
```

The extension id is baked into `manifest.json` (`key`), so it stays
`hgebmpanpoiebnbnmabafnpmkkmdgdmn` and the native host keeps working across
browser restarts. Brave / Edge / Chromium / Arc / Vivaldi use the same host
registration.

## Files

| file | role |
|---|---|
| `scanner.js` | injected into the page; returns every media URL + quality + poster |
| `background.js` | service worker; the only bridge to the native host |
| `popup.html/css/js` | the picker UI (checkboxes, sizes, preview thumbnails) |
| `manifest.json` | MV3 manifest with the stable extension key |

## Native host protocol (`mgd native-host`)

Chrome native messaging (4-byte little-endian length + JSON):

```
{"command":"ping"}                                  → {"ok":true,"app":"mgd",…}
{"command":"probe","urls":[…],"referer":…}          → {"ok":true,"sizes":{…}}
{"command":"download","urls":[…],"destination":?}  → {"ok":true,"spawned":n}
{"command":"open"}                                  → {"ok":true}   (opens the app)
```
