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
- **Several links at once.** Paste or drop text with several links (repeats
  and surrounding words are ignored), drop a `.txt` list, or use File ▸
  Import Links from File… (⇧⌘I): one sheet lists them all; file links keep
  their file name, pages are named after their video unless a name is typed.
- **Playlists and channels.** A YouTube playlist or channel link (also a
  SoundCloud set or Vimeo showcase) opens a picker with every video, its
  length and thumbnail: one quality for all (best, 1080p, 720p, 480p or
  MP3), saved together in a folder named after the playlist, optionally
  numbered in playlist order (`01 - Intro.mp4`). Videos already downloaded
  and live streams/premieres start unticked. A video opened from a playlist
  (`watch?v=…&list=…`) asks: *Just This Video* or *Whole Playlist…*
  (YouTube's endless Mix lists are not asked about).
- **Already downloaded?** Sending something whose file is still on disk
  asks first (*Download Again* / *Show in Finder*), also when the link looks
  different: YouTube links match by video id (watch, youtu.be, shorts),
  file links ignore their signed query (`?token=…`), pages ignore tracking
  parameters (`utm_*`, `si`, `fbclid`…). The name sheet says so too, and
  lists and the multi-video picker show *Already downloaded* and leave those
  unticked. The same link sent twice while it downloads stays one row.
- **Clipboard watch** (opt-in: ⋯ menu or Settings). A video, file,
  platform or magnet link copied in another app is offered in a banner
  (the Dock icon bounces once), or downloaded straight away if chosen. On
  macOS 15.4+ the app only checks *that* a link was copied and reads it
  when Download is clicked, so macOS asks at most once; allowing MediaGrab
  under Privacy & Security › Paste from Other Apps lets it show the link
  and skip non-media links.
- **yt-dlp stays current.** At launch the app asks GitHub for the newest
  yt-dlp release; when yours is older, a banner offers to update it the way
  it was installed (Homebrew, pip or yt-dlp's own updater). Diagnostics
  shows the version, its age and an Update button.
- **Mac-ready videos.** A finished video that QuickTime cannot open or that
  would play choppy (VP9/AV1 4K from YouTube, 10-bit H.264, .mkv/.webm…) is
  converted right away - only as much as needed, with the Mac's hardware
  encoder - while its row shows a purple progress bar ("Making it play
  smoothly on your Mac", percentage, time left, Skip Conversion). Settings
  choose Convert when needed / Only fix the format / Off; finished rows and
  the selection bar offer Make Mac-Ready for files downloaded earlier.
- **Menu bar.** An icon in the menu bar (filled while downloading, with
  the overall percentage) opens a panel: downloads in progress with pause /
  resume, the schedule and when-done countdown, the last few finished files
  (click to show in Finder), Paste Link, Pause/Resume All and Open
  MediaGrab. With the icon on, closing the window keeps MediaGrab running
  there; Settings can turn it off (then closing the window quits when
  nothing is downloading, as before).
- **Schedule and when done.** ⋯ ▸ Schedule Downloads… holds the queue
  until a time (e.g. 02:00, optionally pausing what runs now); rows say
  "Starts at 02:00" and a banner offers Start Now. ⋯ ▸ When Downloads
  Finish: Quit MediaGrab, Sleep or Shut Down once everything (conversions
  and planned retries included) is done, after a 60-second banner that can
  cancel it. It is one-shot and never kept across launches. The first Shut
  Down asks macOS for permission to control System Events.
- **Pause / resume / retry.** Pause signals the engine, which saves exact
  byte positions. Resume continues from them; a failed download keeps its
  partial data and offers *Try Again*. yt-dlp-backed downloads resume by
  re-running (yt-dlp continues its own partial files).
- **Expired links.** The engine refreshes a dead signed link from the
  video's page by itself. When it cannot, the row offers *Update Link…*:
  paste a fresh link or the page's address and it continues from the saved
  part after checking it is the same file. Sending the same video again from
  the browser does the same automatically (one row, no second download).
- **Connection drops and sleep.** While anything downloads, the Mac is kept
  from idle sleep (the display may still sleep; Settings can turn this
  off). A download that fails because the connection dropped (the engine
  reports `"kind": "network"` or `"server"`) is retried by itself after 15 s,
  30 s, 1, 2 and 5 minutes, straight away when the Mac is back online, and
  a few seconds after it wakes. The row shows the countdown.
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
