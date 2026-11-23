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
        var seen = Set<String>()
        env["PATH"] = (usual + inherited).filter { seen.insert($0).inserted }
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
    /// status, both on the main queue, lines always before the end.
    @discardableResult
    static func spawn(_ args: [String],
                      onLine: @escaping (String) -> Void,
                      onEnd: @escaping (Int32) -> Void) -> Process? {
        let p = makeProcess(args)
        let outPipe = Pipe()
        let errPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = errPipe
        p.standardInput = FileHandle.nullDevice

        // One serial queue orders the two pipes' chunks and the end of the
        // process. The end is only reported once both pipes hit EOF, so the
        // last lines (the "done" event with the file path!) can never be
        // lost to a race between "process exited" and "pipe still has data".
        let queue = DispatchQueue(label: "mediagrab.engine.lines")
        final class Stream {
            var bytes: [UInt8] = []
            var eof = false
        }
        final class Ending {
            var status: Int32?
            var reported = false
        }
        let outBuf = Stream(), errBuf = Stream()
        let ending = Ending()

        func emitLines(_ stream: Stream, flush: Bool) {
            while let nl = stream.bytes.firstIndex(of: 0x0A) {
                let line = String(decoding: stream.bytes[..<nl], as: UTF8.self)
                stream.bytes.removeSubrange(...nl)
                DispatchQueue.main.async { onLine(line) }
            }
            if flush, !stream.bytes.isEmpty {
                let line = String(decoding: stream.bytes, as: UTF8.self)
                stream.bytes.removeAll()
                DispatchQueue.main.async { onLine(line) }
            }
        }
        func finishIfReady(force: Bool) {       // on `queue`
            guard !ending.reported, let status = ending.status,
                  force || (outBuf.eof && errBuf.eof) else { return }
            ending.reported = true
            outPipe.fileHandleForReading.readabilityHandler = nil
            errPipe.fileHandleForReading.readabilityHandler = nil
            emitLines(outBuf, flush: true)
            emitLines(errBuf, flush: true)
            DispatchQueue.main.async { onEnd(status) }
        }
        func drain(_ handle: FileHandle, into stream: Stream) {
            handle.readabilityHandler = { fh in
                let data = fh.availableData
                queue.async {
                    if data.isEmpty {
                        stream.eof = true
                        fh.readabilityHandler = nil
                        finishIfReady(force: false)
                    } else {
                        stream.bytes += data
                        emitLines(stream, flush: false)
                    }
                }
            }
        }
        drain(outPipe.fileHandleForReading, into: outBuf)
        drain(errPipe.fileHandleForReading, into: errBuf)

        p.terminationHandler = { proc in
            queue.async {
                ending.status = proc.terminationStatus
                finishIfReady(force: false)
            }
            // a helper that outlives the engine can keep a pipe open: do not
            // wait for its EOF forever
            queue.asyncAfter(deadline: .now() + 2.0) { finishIfReady(force: true) }
        }
        do {
            try p.run()
            return p
        } catch {
            DispatchQueue.main.async {
                onLine("error: could not start the mgd engine (\(error.localizedDescription))")
                onEnd(127)
            }
            return nil
        }
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
