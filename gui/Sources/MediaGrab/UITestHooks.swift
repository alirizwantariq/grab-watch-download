#if DEBUG
import AppKit
import SwiftUI

/// Debug-build-only driver for automated UI checks (never compiled into the
/// release app). With MEDIAGRAB_UITEST_SCRIPT pointing at a JSON script the
/// app runs it step by step - real engine, real store, real views - writing
/// window snapshots and state dumps into MEDIAGRAB_UITEST_OUT, then quits.
///
///   [["seed"], ["snap", "list"], ["submit", "http://…"], ["waitphase", "clip", "done", 30],
///    ["dump", "after"], ["quit"]]
///
/// Run it against a sandbox: MEDIAGRAB_MGD, XDG_CONFIG_HOME, MGD_ROOT and
/// MEDIAGRAB_DATA_DIR keep it away from real downloads and settings. The
/// window is parked off-screen and the app never takes focus.
enum UITestHooks {
    static var active: Bool {
        ProcessInfo.processInfo.environment["MEDIAGRAB_UITEST_SCRIPT"] != nil
    }

    private static var outDir: URL {
        let path = ProcessInfo.processInfo.environment["MEDIAGRAB_UITEST_OUT"]
            ?? NSTemporaryDirectory()
        let url = URL(fileURLWithPath: path, isDirectory: true)
        try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }

    static func run() {
        guard let scriptPath = ProcessInfo.processInfo.environment["MEDIAGRAB_UITEST_SCRIPT"],
              let data = FileManager.default.contents(atPath: scriptPath),
              let steps = try? JSONSerialization.jsonObject(with: data) as? [[Any]] else {
            return
        }
        NSApp.setActivationPolicy(.accessory)      // no dock icon, no focus
        rememberWindowFrames()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.6) {
            parkWindows()
            Task { @MainActor in
                for step in steps {
                    await perform(step)
                }
                log("script finished")
                finish()
            }
        }
    }

    // AppKit remembers where each window was. Parking the window off-screen
    // must not become the remembered position of a normal run, so the saved
    // frames are put back exactly as they were before the script quits.
    private static var framesBefore: [String: Any] = [:]

    private static func frameKeys() -> [String] {
        UserDefaults.standard.dictionaryRepresentation().keys
            .filter { $0.hasPrefix("NSWindow Frame ") }
    }

    private static func rememberWindowFrames() {
        for key in frameKeys() {
            framesBefore[key] = UserDefaults.standard.object(forKey: key)
        }
    }

    private static func finish() -> Never {
        JobStore.shared.saveHistory()
        let defaults = UserDefaults.standard
        for key in frameKeys() where framesBefore[key] == nil {
            defaults.removeObject(forKey: key)
        }
        for (key, value) in framesBefore {
            defaults.set(value, forKey: key)
        }
        defaults.synchronize()
        Prefs.defaults.removePersistentDomain(forName: "mediagrab.uitest")
        exit(0)
    }

    private static var extraWindows: [NSWindow] = []

    private static func parkWindows() {
        for window in NSApp.windows {
            window.setFrame(NSRect(x: -6000, y: -6000, width: 980, height: 660), display: true)
        }
    }

    /// Diagnostic lines from app internals during scripted runs.
    static func trace(_ text: String) {
        guard active else { return }
        DispatchQueue.main.async { log("  trace: " + text) }
    }

    private static func log(_ text: String) {
        let line = text + "\n"
        let url = outDir.appendingPathComponent("uitest.log")
        if let handle = try? FileHandle(forWritingTo: url) {
            handle.seekToEndOfFile()
            handle.write(line.data(using: .utf8)!)
            try? handle.close()
        } else {
            try? line.write(to: url, atomically: true, encoding: .utf8)
        }
    }

    private static func sleep(_ seconds: Double) async {
        try? await Task.sleep(nanoseconds: UInt64(seconds * 1_000_000_000))
    }

    private static func job(_ needle: String) -> DownloadJob? {
        JobStore.shared.jobs.first { $0.title.localizedCaseInsensitiveContains(needle)
            || $0.url.localizedCaseInsensitiveContains(needle) }
    }

    @MainActor
    private static func perform(_ step: [Any]) async {
        guard let name = step.first as? String else { return }
        let arg = { (i: Int) -> String in step.count > i ? "\(step[i])" : "" }
        let num = { (i: Int) -> Double in
            step.count > i ? ((step[i] as? NSNumber)?.doubleValue ?? Double("\(step[i])") ?? 0) : 0
        }
        let store = JobStore.shared
        let session = Session.shared
        log("step: \(step)")
        switch name {
        case "wait": await sleep(num(1))
        case "ask": store.askBeforeDownload = arg(1) == "on"
        case "max": store.maxConcurrent = Int(num(1))
        case "submit": session.submit(arg(1))
        case "type": session.urlString = arg(1)
        case "category": session.category = arg(1)
        case "search": session.searchText = arg(1)
        case "pause": if let j = job(arg(1)) { store.pause(j) }
        case "resume": if let j = job(arg(1)) { store.resume(j) }
        case "cancel": if let j = job(arg(1)) { store.cancel(j) }
        case "remove": if let j = job(arg(1)) { store.remove(j) }
        case "select": if let j = job(arg(1)) { store.toggleSelection(j) }
        case "details": if let j = job(arg(1)) { j.showLog.toggle() }
        case "pauseall": store.pauseAll()
        case "resumeall": store.resumeAll()
        case "refresh": store.refresh()
        case "handoff": session.receiveHandoff()
        case "closesheet": session.sheet = nil
        case "alertconfirm":
            if let c = session.confirmation { session.confirmation = nil; c.action() }
        case "alertcancel": session.confirmation = nil
        case "alertother":
            if let c = session.confirmation, let other = c.otherAction {
                session.confirmation = nil
                other()
            }
        case "playlistopts":
            // ["playlistopts", quality, ownFolder on/off, numbered on/off]
            if case .batch(let batch) = session.sheet {
                batch.quality = arg(1)
                batch.ownFolder = arg(2) == "on"
                batch.numbered = arg(3) == "on"
            }
        case "identities":
            let links = Array(step.dropFirst(2)).compactMap { $0 as? String }
            let ids = links.map { Naming.identity(of: $0) ?? "" }
            if let data = try? JSONSerialization.data(withJSONObject: ids) {
                try? data.write(to: outDir.appendingPathComponent("\(arg(1)).json"))
            }
        case "confirm":
            // press the default button of whatever sheet is up
            switch session.sheet {
            case .rename(let pending): session.confirmRename(pending)
            case .handoff(let probe): session.startHandoff(probe)
            case .batch(let batch): session.startBatch(batch)
            case .torrent(let probe): session.startTorrent(probe)
            case .quality(let probe):
                session.begin(url: probe.url, quality: probe.options.first?.quality ?? "auto",
                              title: probe.title, cookies: probe.cookies)
            default: break
            }
        case "rename":
            if case .rename(let pending) = session.sheet { pending.name = arg(1) }
        case "renameitem":
            if case .handoff(let probe) = session.sheet {
                probe.items.first { $0.url.contains(arg(1)) }?.name = arg(2)
            }
        case "sheet": openSheet(arg(1))
        case "updateytdlp": _ = await store.updateYtdlp()
        case "macmode": store.setMacReadyMode(arg(1)); await sleep(1.0)
        case "macready": if let j = job(arg(1)) { store.makeMacReady([j]) }
        case "feed":
            // hand an engine output line to a download (progress parsing checks)
            job(arg(1))?.ingest(arg(2))
        case "waitconverting":
            let deadline = Date().addingTimeInterval(max(1, num(2)))
            while Date() < deadline, job(arg(1))?.converting != true { await sleep(0.05) }
            log("  \(arg(1)) converting -> \(job(arg(1))?.converting ?? false)")
        case "clipwatch": store.watchClipboard = arg(1) == "on"
        case "clipauto": store.autoDownloadClipboard = arg(1) == "on"
        case "clipcopy":
            let board = session.clipboard.pasteboard
            board.clearContents()
            board.setString(arg(1), forType: .string)
        case "clipaccept": session.acceptClipboardOffer()
        case "importfile": session.importLinks(from: URL(fileURLWithPath: arg(1)))
        case "netdown": store.networkChanged(false)
        case "netup": store.networkChanged(true)
        case "fetch":
            // visit a url (e.g. a page that hands out a new signed link)
            if let url = URL(string: arg(1)) { _ = try? await URLSession.shared.data(from: url) }
        case "relink":
            if let j = job(arg(1)) { store.updateLink(j, to: arg(2)) }
        case "updatelinksheet":
            if let j = job(arg(1)) { session.sheet = .updateLink(j) }
        case "settingsview":
            // the Settings scene's content, hosted in a plain off-screen window
            let host = NSHostingController(rootView: SettingsView().environmentObject(store))
            let window = NSWindow(contentViewController: host)
            window.title = "Settings"
            window.setFrameOrigin(NSPoint(x: -6000, y: -6000))
            window.orderFront(nil)
            extraWindows.append(window)
        case "seed": seed()
        case "waitphase":
            // ["waitphase", needle, phase, timeout]
            let deadline = Date().addingTimeInterval(max(1, num(3)))
            while Date() < deadline {
                if let j = job(arg(1)), j.phase.rawValue == arg(2) { break }
                await sleep(0.1)
            }
            log("  \(arg(1)) -> \(job(arg(1))?.phase.rawValue ?? "missing")")
            if let j = job(arg(1)), j.phase.rawValue != arg(2), let p = j.process {
                // timed out: what is the engine process doing?
                let pid = p.processIdentifier
                let ps = Process()
                ps.executableURL = URL(fileURLWithPath: "/bin/ps")
                ps.arguments = ["-o", "pid,stat,etime,command", "-p", "\(pid)"]
                let out = Pipe()
                ps.standardOutput = out
                try? ps.run()
                ps.waitUntilExit()
                let text = String(decoding: out.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
                log("  timeout: pid \(pid) isRunning=\(p.isRunning) ps=\(text.replacingOccurrences(of: "\n", with: " | "))")
                log("  last log: " + j.log.suffix(400).replacingOccurrences(of: "\n", with: " | "))
            }
        case "waitsheet":
            let deadline = Date().addingTimeInterval(max(1, num(2)))
            while Date() < deadline, sheetName(session.sheet) != arg(1) { await sleep(0.1) }
            log("  sheet -> \(sheetName(session.sheet))")
        case "waitgone":
            let deadline = Date().addingTimeInterval(max(1, num(2)))
            while Date() < deadline, job(arg(1)) != nil { await sleep(0.1) }
        case "snap":
            await sleep(0.5)       // let SwiftUI settle
            snapshot(arg(1))
        case "dump": dump(arg(1))
        case "quit":
            log("quit requested")
            finish()
        default:
            log("  unknown step")
        }
    }

    private static func sheetName(_ sheet: ActiveSheet?) -> String {
        switch sheet {
        case .none: return "none"
        case .quality: return "quality"
        case .rename: return "rename"
        case .batch: return "batch"
        case .torrent: return "torrent"
        case .handoff: return "handoff"
        case .diagnostics: return "diagnostics"
        case .updateLink: return "updatelink"
        case .extensionSetup: return "extension"
        }
    }

    // MARK: canned content for layout checks

    private static func seed() {
        let store = JobStore.shared
        func make(_ title: String, _ phase: JobPhase, done: Int64, total: Int64,
                  url: String, speed: Double = 0) -> DownloadJob {
            let job = DownloadJob(url: url, title: title)
            job.phase = phase
            job.done = done
            job.total = total
            job.bytesPerSecond = speed
            job.args = ["grab", url]
            return job
        }
        let running = make("Summer trip 2026 — beach day 1080p.mp4", .running,
                           done: 412_000_000, total: 1_480_000_000,
                           url: "https://cdn.example.com/v/beach_1080p.mp4?sig=abc", speed: 6_400_000)
        running.outPath = "/Users/me/Downloads/mgd/video/Summer trip 2026 — beach day 1080p.mp4"
        running.log = "> https://cdn.example.com/v/beach_1080p.mp4\n  saving to ~/Downloads/mgd/video\n"
        let adopted = make("ubuntu-26.04-desktop-arm64.iso", .running,
                           done: 2_100_000_000, total: 5_900_000_000,
                           url: "https://releases.example.org/ubuntu-26.04-desktop-arm64.iso", speed: 11_200_000)
        adopted.adopted = true
        let starting = make("Conference keynote", .running, done: 0, total: 0,
                            url: "https://www.youtube.com/watch?v=abcdefghijk")
        let queued = make("Podcast episode 214.mp3", .queued, done: 0, total: 0,
                          url: "https://media.example.fm/ep214.mp3")
        let paused = make("Mountain hike 4K.mkv", .paused, done: 3_300_000_000, total: 7_800_000_000,
                          url: "https://cdn.example.com/v/hike_2160p.mkv")
        let failed = make("Members-only stream", .failed, done: 0, total: 0,
                          url: "https://video.example.net/watch/9912")
        failed.errorMessage = "could not download this link (the site blocked the request or needs a login)"
        failed.log = "# yt-dlp strategy: chrome UA\nERROR: HTTP Error 403: Forbidden\n"
        var finished: [DownloadJob] = []
        let samples: [(String, Int64, Double)] = [
            ("Family dinner.mp4", 364_000_000, 180),
            ("Quarterly report.pdf", 2_400_000, 3_600),
            ("Field recording.flac", 48_000_000, 86_400),
            ("design-assets.zip", 913_000_000, 200_000),
        ]
        for (name, size, ago) in samples {
            let job = make(name, .done, done: size, total: size,
                           url: "https://files.example.com/\(name)")
            let folder = MediaGrabCategory.category(forPath: name).lowercased()
            job.finishedPath = NSHomeDirectory() + "/Downloads/mgd/\(folder)/\(name)"
            job.finishedSize = size
            job.finishedAt = Date().addingTimeInterval(-ago)
            finished.append(job)
        }
        store.jobs = [running, adopted, starting, queued, paused, failed] + finished
    }

    private static func openSheet(_ name: String) {
        let session = Session.shared
        switch name {
        case "quality":
            session.sheet = .quality(QualityProbe.parse(url: "https://www.youtube.com/watch?v=abcdefghijk", [
                "source": "ytdlp", "title": "Conference keynote — the next ten years",
                "items": [["label": "Best available (2160p, ~1.9 GB)", "quality": "best"],
                          ["label": "1080p · mp4 · ~620 MB", "quality": "1080"],
                          ["label": "720p · mp4 · ~310 MB", "quality": "720"],
                          ["label": "480p · mp4 · ~140 MB", "quality": "480"],
                          ["label": "Audio only (m4a) · ~58 MB", "quality": "audio"]]]))
        case "qualityerror":
            session.sheet = .quality(QualityProbe.parse(url: "https://www.youtube.com/watch?v=zzz", [
                "source": "ytdlp", "items": [],
                "error": "yt-dlp could not read formats for this link (bot check? open/update your preferred browser once, or pass --cookies)"]))
        case "rename":
            session.sheet = .rename(PendingDownload(
                url: "https://cdn.example.com/videos/-187859176/456239649/vid_480p.mp4?rs=480000&secure=XlMWA6AZ%3D%3D",
                quality: "auto", name: "Summer trip 2026 — beach day"))
        case "batch":
            let items: [[String: Any]] = (1...7).map { i in
                ["url": "https://cdn.example.com/album/clip_\(i)_720p.mp4", "title": "Album clip \(i)",
                 "quality": i % 2 == 0 ? "720p" : "1080p", "poster": "",
                 "size": NSNumber(value: Int64(i) * 83_000_000)]
            }
            session.sheet = .batch(BatchProbe(url: "https://example.com/a/summer-album",
                                              items: items.compactMap { BatchItem(dict: $0) }))
        case "torrent":
            let files: [[String: Any]] = [
                ["index": 1, "path": "Big Buck Bunny/Big.Buck.Bunny.1080p.mkv", "size": NSNumber(value: 4_200_000_000 as Int64), "video": true],
                ["index": 2, "path": "Big Buck Bunny/Big.Buck.Bunny.sample.mkv", "size": NSNumber(value: 58_000_000 as Int64), "video": true],
                ["index": 3, "path": "Big Buck Bunny/subtitles/english.srt", "size": NSNumber(value: 61_000 as Int64), "video": false],
                ["index": 4, "path": "Big Buck Bunny/README.txt", "size": NSNumber(value: 900 as Int64), "video": false],
            ]
            session.sheet = .torrent(TorrentProbe(url: "magnet:?xt=urn:btih:abc", name: "Big Buck Bunny (2008) 1080p",
                                                  files: files.compactMap { TorrentFile(dict: $0) }))
        case "diagnostics": session.sheet = .diagnostics
        case "extension": session.sheet = .extensionSetup
        case "missingtools":
            var report = JobStore.shared.report ?? EngineReport()
            report.reachable = true
            report.tools = report.tools.map {
                $0.id == "yt-dlp" || $0.id == "webtorrent"
                    ? EngineReport.Tool(id: $0.id, path: nil, purpose: $0.purpose, install: $0.install) : $0
            }
            JobStore.shared.report = report
        case "confirm":
            session.confirmation = Confirmation(
                title: "Delete 3 selected items?",
                message: "2 files will be moved to the Trash; 1 unfinished download will be stopped and the downloaded part deleted.",
                actionLabel: "Delete", action: {})
        default: break
        }
    }

    // MARK: output

    private static func snapshot(_ name: String) {
        var index = 0
        for window in NSApp.windows where window.isVisible || window.attachedSheet != nil {
            guard let view = window.contentView?.superview ?? window.contentView,
                  view.bounds.width > 50,
                  let rep = view.bitmapImageRepForCachingDisplay(in: view.bounds) else { continue }
            view.cacheDisplay(in: view.bounds, to: rep)
            guard let png = rep.representation(using: .png, properties: [:]) else { continue }
            let role = window.isSheet ? "sheet" : (index == 0 ? "main" : "win\(index)")
            index += window.isSheet ? 0 : 1
            let url = outDir.appendingPathComponent("\(name)-\(role).png")
            try? png.write(to: url)
            log("  wrote \(url.lastPathComponent) (\(Int(view.bounds.width))x\(Int(view.bounds.height)))")
        }
    }

    private static func handoffItems() -> [[String: String]] {
        guard case .handoff(let probe) = Session.shared.sheet else { return [] }
        return probe.items.map { ["url": $0.url, "name": $0.name, "kind": $0.kind,
                                  "referer": $0.referer ?? "",
                                  "included": $0.included ? "yes" : "no",
                                  "earlier": $0.earlier?.fileName ?? ""] }
    }

    /// Button titles of an alert on screen (alerts do not render into
    /// snapshots, so their buttons are checked this way).
    private static func alertButtons() -> [String] {
        func buttons(in view: NSView) -> [String] {
            var out: [String] = []
            if let b = view as? NSButton, !b.title.isEmpty { out.append(b.title) }
            for sub in view.subviews { out += buttons(in: sub) }
            return out
        }
        return NSApp.windows
            .filter { $0.isSheet || String(describing: type(of: $0)).contains("Alert") }
            .flatMap { w in w.contentView.map(buttons(in:)) ?? [] }
    }

    private static func pendingEarlier() -> String {
        guard case .rename(let pending) = Session.shared.sheet else { return "" }
        return pending.earlier?.fileName ?? ""
    }

    private static func batchItems() -> [[String: String]] {
        guard case .batch(let batch) = Session.shared.sheet else { return [] }
        return batch.items.map { ["url": $0.url, "title": $0.title,
                                  "selected": batch.selected.contains($0.id) ? "yes" : "no",
                                  "earlier": batch.earlier[$0.id]?.fileName ?? "",
                                  "live": $0.live ? "yes" : "no",
                                  "fileName": batch.fileName(for: $0) ?? ""] }
    }

    private static func dump(_ name: String) {
        let store = JobStore.shared
        let jobs: [[String: Any]] = store.jobs.map { j in
            ["title": j.title, "url": j.url, "phase": j.phase.rawValue,
             "done": j.done, "total": j.total,
             "engineID": j.engineID ?? "", "outPath": j.outPath ?? "",
             "finishedPath": j.finishedPath ?? "", "finishedSize": j.finishedSize,
             "error": j.errorMessage ?? "", "adopted": j.adopted,
             "owned": j.process != nil, "args": j.args,
             "errorKind": j.errorKind ?? "",
             "converting": j.converting, "convertProgress": j.convertProgress,
             "convertAction": j.convertAction ?? "",
             "retryIn": j.retryAt.map { $0.timeIntervalSinceNow } ?? -1,
             "autoRetries": j.autoRetries]
        }
        let state: [String: Any] = [
            "jobs": jobs,
            "sheet": sheetName(Session.shared.sheet),
            "confirmation": Session.shared.confirmation?.title ?? "",
            "alertButtons": alertButtons(),
            "pendingEarlier": pendingEarlier(),
            "batchItems": batchItems(),
            "playlistTitle": { () -> String in
                if case .batch(let b) = Session.shared.sheet { return b.playlist?.title ?? "" }
                return ""
            }(),
            "windows": NSApp.windows.filter { $0.canBecomeMain && !$0.isSheet }.count,
            "bundledEngine": Engine.bundledLauncher ?? "",
            "bundledExtension": Engine.bundledExtension?.path ?? "",
            "handoffItems": handoffItems(),
            "status": store.status,
            "keepingAwake": store.awakeActivity != nil,
            "ytdlpVersion": store.report?.ytdlpVersion ?? "",
            "ytdlpUpdateAvailable": store.report?.ytdlpUpdateAvailable ?? false,
            "clipboardOffer": Session.shared.clipboardOffer?.link ?? (Session.shared.clipboardOffer == nil ? "" : "(unread)"),
            "networkAvailable": store.networkAvailable,
            "engineReachable": store.engineReachable,
            "downloadRoot": store.downloadRoot,
            "launcher": Engine.launcher,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: state,
                                                  options: [.prettyPrinted, .sortedKeys]) {
            try? data.write(to: outDir.appendingPathComponent("\(name).json"))
        }
    }
}
#endif
