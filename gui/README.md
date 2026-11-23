# MediaGrab (macOS app)

A native SwiftUI download manager on top of the `mgd` engine (`cli/`):
segmented, resumable downloads of files, video pages, YouTube/X, HLS/DASH
streams and torrents, with a queue, pause/resume, retry and a browser
extension that sends videos straight to the app.

```
gui/
├── Package.swift            SwiftPM (macOS 13+, Swift 5.9)
├── Sources/MediaGrab/
│   ├── App.swift            app, single window, menus, settings, quit rules
│   ├── Engine.swift         locating + running mgd, its JSON answers
│   ├── Store.swift          the download list: queue, pause/resume/cancel, history
│   ├── Session.swift        link → (quality) → (page scan) → (name) → download
│   ├── Models.swift         DownloadJob, file naming, formatting
│   ├── ContentView.swift    main window
│   ├── Rows.swift           download rows
│   ├── Sheets.swift         quality / name / batch / torrent / browser / diagnostics
│   ├── BrowserLink.swift    connects browsers to the engine (native messaging)
│   └── UITestHooks.swift    scripted runs (debug builds only)
├── script/build_app.sh      swift build -> dist/MediaGrab.app
├── script/create_dmg.sh     dist/MediaGrab.app -> drag-to-Applications DMG
├── tests/ui_flows.py        end-to-end checks of the real app in a sandbox
└── dist/                    build outputs (gitignored)
```

## Build

Requirements: Xcode Command Line Tools (`xcode-select --install`). To run,
the engine needs `python3` (3.10+ recommended) plus, for video sites and
streams, `yt-dlp` and `ffmpeg`; torrents need `aria2`:

```sh
brew install python yt-dlp ffmpeg aria2

cd gui
./script/build_app.sh          # dist/MediaGrab.app
./script/create_dmg.sh         # dist/MediaGrab-macos-arm64.dmg
```

The app bundle is self-contained: `build_app.sh` copies the engine
(`cli/bin/mgd` + the `mgd` package) and the browser extension into
`MediaGrab.app/Contents/Resources`, so the installed app does not depend on
this repository. The Diagnostics sheet (⋯ menu) shows which helper tools are
missing and the command that installs each.

## How it works

- The app does not download anything itself; it drives the engine
  (`mgd grab --json-progress`, `mgd list --json`, `mgd resume|pause|cancel <id>`,
  `mgd qualities`, `mgd page-media`, `mgd torrent-info`, `mgd doctor --json`).
- **One window, one list.** Links handed over by the browser open in the
  existing window. Closing the window quits the app unless downloads are
  running; quitting with running downloads asks first and pauses them.
- **Stable ids.** Every engine-backed download is addressed by the id the
  engine reports when it starts (`mgd list` row numbers shift as other
  downloads start and finish, so they are never used).
- **Queue.** At most *N* downloads run at once (Settings, default 3); the
  rest wait and start by themselves, oldest first.
- **Adding a link.** Paste / drop / ⇧⌘V → the engine is asked what the link
  offers: a quality picker for YouTube/X, a batch picker for pages with
  several videos, a file picker for torrents, then a name + folder sheet
  (can be switched off in Settings).
- **Pause / resume / retry.** Pause signals the engine, which saves exact
  byte positions. Resume continues from them; a failed download keeps its
  partial data and offers *Try Again*. yt-dlp-backed downloads resume by
  re-running (yt-dlp continues its own partial files).
- **Downloads started elsewhere** (a terminal, the extension's headless
  mode, a previous app session) are picked up through `mgd list` every few
  seconds and can be paused, resumed and cancelled like the others.
- **History** lives in `~/Library/Application Support/MediaGrab/history.json`
  (with a one-generation `.bak`).

## Browser extension

*⋯ → Browser Extension…* does the setup: it shows the bundled extension
folder to load in the browser and connects every installed Chromium browser
to the engine inside the app (native messaging host). See
`chrome-extension/README.md`.

## Tests

```sh
python3 gui/tests/ui_flows.py           # all flows
python3 gui/tests/ui_flows.py queue     # one flow
python3 gui/tests/ui_flows.py --keep    # keep snapshots + state dumps
```

Each flow launches the debug build with a script (`UITestHooks.swift`) that
drives the real store and views against the real engine and a local test
site: naming + pause + resume to an identical file, the queue, cancel,
adopting a download started in a terminal, browser hand-offs, failure and
retry, pages, relaunch persistence, and a render of every sheet. Everything
runs in a throwaway sandbox (own config dir, download root, history and
preferences); the window is parked off-screen and never takes focus.

Note: with the Command Line Tools only (no full Xcode), SwiftUI's `@State`
macro is unavailable, so view-local state uses small `ObservableObject`
models. Open the package in Xcode (`File > Open`, pick `gui/Package.swift`)
if you prefer building there.
