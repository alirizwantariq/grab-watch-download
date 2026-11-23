import Foundation

// MARK: - locating and running the mgd engine

/// The app does not download anything itself: it drives the `mgd` CLI
/// (`mgd grab --json-progress`, `mgd list --json`, `mgd resume|pause|cancel
/// <id>` …). A copy of the engine ships inside the app bundle, so the app
/// works wherever it is installed.
enum Engine {
    /// Where the engine launcher lives, first match wins:
    ///   1. `MEDIAGRAB_MGD` (development / tests)
    ///   2. the copy bundled in `MediaGrab.app/Contents/Resources/engine`
    ///   3. an `mgd` installed on the machine
    static let launcher: String = {
        let fm = FileManager.default
        if let override = ProcessInfo.processInfo.environment["MEDIAGRAB_MGD"],
           fm.fileExists(atPath: override) {
            return override
        }
        if let bundled = bundledLauncher {
            return bundled
        }
        for candidate in MGD.candidates where fm.fileExists(atPath: candidate) {
            return candidate
        }
        return "mgd"   // resolved through PATH by /usr/bin/env
    }()

    static var bundledLauncher: String? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let path = res.appendingPathComponent("engine/bin/mgd").path
        return FileManager.default.fileExists(atPath: path) ? path : nil
    }

    /// The unpacked browser extension shipped with the app (if any).
    static var bundledExtension: URL? {
        guard let res = Bundle.main.resourceURL else { return nil }
        let url = res.appendingPathComponent("chrome-extension", isDirectory: true)
        return FileManager.default.fileExists(
            atPath: url.appendingPathComponent("manifest.json").path) ? url : nil
    }

    /// GUI apps do not inherit the shell's PATH; restore the usual homes of
    /// `python3`, `yt-dlp`, `ffmpeg`, `aria2c`.
    static func environment() -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let usual = ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin",
                     "/usr/sbin", "/sbin"]
        let inherited = (env["PATH"] ?? "").split(separator: ":").map(String.init)
        // MEDIAGRAB_EXTRA_PATH goes first (automated tests: fake tools)
        let extra = (env["MEDIAGRAB_EXTRA_PATH"] ?? "").split(separator: ":").map(String.init)
        var seen = Set<String>()
        env["PATH"] = (extra + usual + inherited).filter { seen.insert($0).inserted }
            .joined(separator: ":")
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        return env
    }

    /// mgd's config directory (`$XDG_CONFIG_HOME/mgd`, default `~/.config/mgd`).
    static var configDirectory: URL {
        let env = ProcessInfo.processInfo.environment
        let base = env["XDG_CONFIG_HOME"].flatMap { $0.isEmpty ? nil : $0 }
            ?? (NSHomeDirectory() as NSString).appendingPathComponent(".config")
        return URL(fileURLWithPath: base, isDirectory: true)
            .appendingPathComponent("mgd", isDirectory: true)
    }

    private static func makeProcess(_ args: [String]) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = [launcher] + args
        p.environment = environment()
        return p
    }

    /// Blocking one-shot call (stdout + stderr merged). Run off the main thread.
    static func runBlocking(_ args: [String]) -> String {
        let p = makeProcess(args)
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            return String(decoding: data, as: UTF8.self)
        } catch {
            return "error: could not run the mgd engine (\(error.localizedDescription))"
        }
    }

    /// Blocking one-shot call returning stdout only (for JSON answers:
    /// warnings on stderr must not corrupt the document).
    static func runJSONBlocking(_ args: [String]) -> [String: Any]? {
        let p = makeProcess(args)
        let out = Pipe()
        p.standardOutput = out
        p.standardError = FileHandle.nullDevice
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
            let data = out.fileHandleForReading.readDataToEndOfFile()
            p.waitUntilExit()
            return (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        } catch {
            return nil
        }
    }

    static func run(_ args: [String]) async -> String {
        await Task.detached(priority: .userInitiated) { runBlocking(args) }.value
    }

    static func runJSON(_ args: [String]) async -> [String: Any]? {
        await Task.detached(priority: .userInitiated) { runJSONBlocking(args) }.value
    }

    /// Long-running call: `onLine` gets every output line and `onEnd` the exit
    /// status, both on the main queue, every line before the end.
    ///
    /// stdout and stderr share one pipe (lines arrive in the order they were
    /// written) and one reader thread does blocking-free reads with poll()
    /// until the pipe closes. Nothing else is involved: no readability
    /// callbacks to cancel, no shared queue that could hold the "ended"
    /// signal behind something stuck. If a helper that outlives the engine
    /// keeps the pipe open, the end is reported two quiet seconds after the
    /// engine itself exited.
    @discardableResult
    static func spawn(_ args: [String],
                      onLine: @escaping (String) -> Void,
                      onEnd: @escaping (Int32) -> Void) -> Process? {
        let p = makeProcess(args)
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        p.standardInput = FileHandle.nullDevice
        do {
            try p.run()
        } catch {
            DispatchQueue.main.async {
                onLine("error: could not start the mgd engine (\(error.localizedDescription))")
                onEnd(127)
            }
            return nil
        }
        let reader = pipe.fileHandleForReading
        let fd = reader.fileDescriptor
        let thread = Thread {
            var pending: [UInt8] = []
            var chunk = [UInt8](repeating: 0, count: 64 * 1024)
            var quietSince: Date?

            func emit(flush: Bool) {
                while let nl = pending.firstIndex(of: 0x0A) {
                    let line = String(decoding: pending[..<nl], as: UTF8.self)
                    pending.removeSubrange(...nl)
                    DispatchQueue.main.async { onLine(line) }
                }
                if flush, !pending.isEmpty {
                    let line = String(decoding: pending, as: UTF8.self)
                    pending.removeAll()
                    DispatchQueue.main.async { onLine(line) }
                }
            }

            var lines = 0
            var how = "eof"
            #if DEBUG
            UITestHooks.trace("reader start pid \(p.processIdentifier) args \(args.prefix(3).joined(separator: " "))")
            #endif
            reading: while true {
                var pfd = pollfd(fd: fd, events: Int16(POLLIN), revents: 0)
                let ready = poll(&pfd, 1, 250)
                if ready > 0 {
                    let n = chunk.withUnsafeMutableBytes { read(fd, $0.baseAddress, $0.count) }
                    if n > 0 {
                        pending.append(contentsOf: chunk[0..<n])
                        lines += chunk[0..<n].reduce(0) { $1 == 0x0A ? $0 + 1 : $0 }
                        emit(flush: false)
                        quietSince = nil
                    } else if n == 0 {
                        break reading                      // EOF: every writer is gone
                    } else if errno != EINTR && errno != EAGAIN {
                        how = "read error \(errno)"
                        break reading
                    }
                } else if ready == 0, !p.isRunning {
                    // the engine exited but something still holds the pipe
                    let since = quietSince ?? Date()
                    quietSince = since
                    if Date().timeIntervalSince(since) > 2 { how = "quiet after exit"; break reading }
                } else if ready < 0, errno != EINTR {
                    how = "poll error \(errno)"
                    break reading
                }
            }
            #if DEBUG
            UITestHooks.trace("reader end pid \(p.processIdentifier): \(how) after \(lines) lines")
            #else
            _ = (lines, how)
            #endif
            emit(flush: true)
            p.waitUntilExit()
            let status = p.terminationStatus
            #if DEBUG
            UITestHooks.trace("exit pid \(p.processIdentifier) status \(status)")
            #endif
            try? reader.close()
            // queued after every line above: main runs them in order
            DispatchQueue.main.async { onEnd(status) }
        }
        thread.name = "mediagrab.engine.reader"
        thread.start()
        return p
    }
}

// MARK: - mgd JSON payloads

/// `--cookies <file>` when a site login came with the link.
func cookieArgs(_ path: String?) -> [String] {
    guard let path, FileManager.default.fileExists(atPath: path) else { return [] }
    return ["--cookies", path]
}

/// One unfinished download as `mgd list --json` reports it.
struct ListEntry: Identifiable, Equatable {
    /// Stable id (survives other downloads starting/finishing around it).
    let id: String
    let url: String
    let filename: String
    let done: Int64
    let size: Int64
    let running: Bool
    let outPath: String
    let isTorrent: Bool

    static func parse(_ object: [String: Any]?) -> [ListEntry]? {
        guard let arr = object?["items"] as? [[String: Any]] else { return nil }
        return arr.compactMap { it in
            guard let id = it["id"] as? String, !id.isEmpty else { return nil }
            return ListEntry(id: id,
                             url: it["url"] as? String ?? "",
                             filename: it["filename"] as? String ?? "",
                             done: (it["done"] as? NSNumber)?.int64Value ?? 0,
                             size: (it["size"] as? NSNumber)?.int64Value ?? 0,
                             running: (it["state"] as? String) == "running",
                             outPath: it["out_path"] as? String ?? "",
                             isTorrent: (it["kind"] as? String) == "torrent")
        }
    }

    var displayName: String {
        if !outPath.isEmpty, !isTorrent {
            return (outPath as NSString).lastPathComponent
        }
        return filename.isEmpty ? url : filename
    }
}

struct QualityOption: Identifiable {
    let id = UUID()
    let label: String
    let quality: String    // value passed to `mgd grab --quality`
}

struct QualityProbe: Identifiable {
    let id = UUID()
    let url: String
    let title: String?
    let source: String?
    let options: [QualityOption]
    let error: String?
    /// the site login the link came with (browser extension), if any
    var cookies: String? = nil

    static func parse(url: String, _ object: [String: Any]?) -> QualityProbe {
        guard let obj = object else {
            return QualityProbe(url: url, title: nil, source: nil, options: [],
                                error: "Could not read the available qualities for this link.")
        }
        let items = (obj["items"] as? [[String: Any]] ?? []).map {
            QualityOption(label: $0["label"] as? String ?? "?",
                          quality: $0["quality"] as? String ?? "best")
        }
        // "best" is duplicated near the top of a resolution ladder; collapse it
        var collapsed: [QualityOption] = []
        for opt in items where !(opt.quality == "best" && !collapsed.isEmpty) {
            collapsed.append(opt)
        }
        return QualityProbe(url: url,
                            title: obj["title"] as? String,
                            source: obj["source"] as? String,
                            options: collapsed,
                            error: obj["error"] as? String)
    }
}

/// `mgd doctor --json`: which helper tools the engine can use.
struct EngineReport {
    struct Tool: Identifiable {
        let id: String          // "yt-dlp"
        let path: String?
        let purpose: String
        let install: String
    }

    var python: String = ""
    var root: String = ""
    var config: String = ""
    var unfinished: Int = 0
    /// yt-dlp: installed version, its age, how it was installed, newest
    var ytdlpVersion: String?
    var ytdlpAgeDays: Int?
    var ytdlpInstall: String?
    var ytdlpLatest: String?
    var ytdlpUpdateAvailable = false
    var tools: [Tool] = []
    var reachable = false

    static func parse(_ object: [String: Any]?) -> EngineReport {
        var report = EngineReport()
        guard let obj = object else { return report }
        report.reachable = true
        report.python = obj["python"] as? String ?? ""
        report.root = obj["root"] as? String ?? ""
        report.config = obj["config"] as? String ?? ""
        report.unfinished = (obj["unfinished"] as? NSNumber)?.intValue ?? 0
        if let y = obj["ytdlp"] as? [String: Any] {
            report.ytdlpVersion = y["version"] as? String
            report.ytdlpAgeDays = (y["age_days"] as? NSNumber)?.intValue
            report.ytdlpInstall = y["install"] as? String
            report.ytdlpLatest = y["latest"] as? String
            report.ytdlpUpdateAvailable = (y["update_available"] as? Bool) ?? false
        }
        let found = obj["tools"] as? [String: Any] ?? [:]
        let catalog: [(String, String, String)] = [
            ("yt-dlp", "YouTube, X and 1000+ video sites, HLS/DASH streams", "brew install yt-dlp"),
            ("ffmpeg", "merging video + audio, stream assembly", "brew install ffmpeg"),
            ("aria2c", "torrents and magnet links", "brew install aria2"),
            ("webtorrent", "streaming a torrent while it downloads", "brew install webtorrent-cli"),
        ]
        report.tools = catalog.map { name, purpose, install in
            Tool(id: name, path: found[name] as? String, purpose: purpose, install: install)
        }
        return report
    }

    var missingEssentials: [Tool] {
        tools.filter { $0.path == nil && ["yt-dlp", "ffmpeg"].contains($0.id) }
    }
}
