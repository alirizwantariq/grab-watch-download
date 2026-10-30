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
  (`mgd grab --json-progress`, `mgd list --json`, `mgd resume`, `mgd pause`,
  `mgd cancel`, `mgd doctor`), restoring `/opt/homebrew/bin` on `PATH` since
  GUI apps don't inherit your shell's PATH.
- Quality choices (`--quality`), category choice (`-t`) map 1:1 to CLI flags.
- Pause sends SIGTERM to the running `mgd` process; the engine saves exact
  byte positions and the paused job appears in the list; Resume runs
  `mgd resume <id>`.

Note: building under Command Line Tools only (no full Xcode), SwiftUI's
newer `@State` macros are avoided in favor of classic wrappers so `swift
build` works with `xcodebuild`-less setups. Open the same package in Xcode
(`File > Open`, pick `gui/Package.swift`) and build/run there if you prefer.