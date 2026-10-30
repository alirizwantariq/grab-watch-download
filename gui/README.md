# MediaGrab GUI (macOS)

A native SwiftUI front-end for the `mgd` download engine (same engine as
`cli/`): segmented, resumable downloads, YouTube/X quality picking, pause /
resume / cancel, live logs.

```
gui/
├── Package.swift           SwiftPM (macOS 13+, Swift 5.9)
├── Sources/MediaGrab/      app source
├── script/build_app.sh     swift build -c release -> dist/MediaGrab.app
├── script/create_dmg.sh    dist/MediaGrab.app -> drag-to-Applications DMG
└── dist/                   build outputs (gitignored)
```

## Build locally

Requirements: Xcode Command Line Tools (`xcode-select --install`), plus the
mgd engine needs `python3`, `yt-dlp`, `ffmpeg` (`brew install yt-dlp ffmpeg`).

```sh
cd gui
./script/build_app.sh          # dist/MediaGrab.app
open dist/MediaGrab.app
```

## Make the DMG

```sh
./script/create_dmg.sh         # dist/MediaGrab-macos-arm64.dmg
```

## How it works

- The GUI does not re-implement downloading; it drives the mgd CLI
  (`mgd grab --json-progress`, `mgd qualities`, `mgd list --json`,
  `mgd resume`, `mgd pause`, `mgd cancel`, `mgd doctor`), restoring
  `/opt/homebrew/bin` on `PATH` since GUI apps don't inherit your shell's PATH.
- **Quality chooser:** pressing Grab first asks mgd what the link offers
  (`mgd qualities`). YouTube links show the yt-dlp resolution ladder, X/Twitter
  links show the resolver bridge ladder (e.g. 270p/360p/480p), both plus
  "audio only"; single-file links skip straight to downloading.
- **Pause/Resume:** each active row has a Pause button (SIGTERM → mgd saves
  exact byte positions); a paused row shows Resume, which runs `mgd resume <id>`
  with NDJSON progress. Downloads paused in another terminal (or before the app
  launched) show up in the "Paused" section with Resume/Cancel buttons — the
  list refreshes every few seconds.
- **Completed rows** show a file-type icon (film/music/photo/…) — click it (or
  "Show") to open the folder containing the file, selected in Finder.

Note: building under Command Line Tools only (no full Xcode), SwiftUI's
newer `@State` macros are avoided in favor of classic wrappers so `swift
build` works with `xcodebuild`-less setups. Open the same package in Xcode
(`File > Open`, pick `gui/Package.swift`) and build/run there if you prefer.