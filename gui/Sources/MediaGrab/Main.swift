import SwiftUI
import Combine

// MARK: - mgd process plumbing

struct MGDPaths {
    static var mgd: String {
        FileManager.default.fileExists(atPath: MGD.binary) ? MGD.binary : MGD.fallback
    }

    /// GUI apps do not inherit the user's shell PATH; restore common homes
    /// so `python3`, `yt-dlp`, `ffmpeg`, curl-cffi etc. are reachable.
    static func environment(pathExtra: String = "") -> [String: String] {
        var env = ProcessInfo.processInfo.environment
        let home = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin" + pathExtra
        env["PATH"] = home
        return env
    }

    static func run(_ args: [String]) async -> String {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = [mgd] + args
        p.environment = environment()
        let pipe = Pipe()
        p.standardOutput = pipe
        p.standardError = pipe
        do {
            try p.run()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return String(data: data, encoding: .utf8) ?? ""
        } catch {
            return "error: \(error.localizedDescription)"
        }
    }

    struct RunningJob {
        let process: Process
    }

    static func spawn(_ args: [String],
                      onLine: @escaping (String) -> Void,
                      onEnd: @escaping (Int32) -> Void) -> Process {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        p.arguments = [mgd] + args
        p.environment = environment()
        let outPipe = Pipe()
        let errPipe = Pipe()
        p.standardOutput = outPipe
        p.standardError = errPipe
        var buf: [UInt8] = []
        func handle(_ data: Data) {
            buf += data
            while let nl = buf.firstIndex(of: 0x0A) {
                let lineData = Data(buf[buf.startIndex..<nl])
                buf.removeSubrange(buf.startIndex..<buf.index(after: nl))
                let line = String(bytes: lineData, encoding: .utf8) ?? ""
                DispatchQueue.main.async { onLine(line) }
            }
        }
        if #available(macOS 10.15.4, *) {
            outPipe.fileHandleForReading.readabilityHandler = { fh in
                handle(fh.availableData)
            }
            errPipe.fileHandleForReading.readabilityHandler = { fh in
                handle(fh.availableData)
            }
        }
        p.terminationHandler = { proc in
            outPipe.fileHandleForReading.readabilityHandler = nil
            errPipe.fileHandleForReading.readabilityHandler = nil
            DispatchQueue.main.async { onEnd(proc.terminationStatus) }
        }
        try? p.run()
        return p
    }
}

// MARK: - job model

enum JobPhase: String {
    case queued, running, paused, done, failed, cancelled
}

final class DownloadJob: ObservableObject, Identifiable {
    let id = UUID()
    @Published var url: String
    @Published var title: String
    @Published var phase: JobPhase = .queued
    @Published var done: Int64 = 0
    @Published var total: Int64 = 0
    @Published var bytesPerSecond: Double = 0
    @Published var log: String = ""
    @Published var finishedPath: String?
    var process: Process?
    private var lastTick = Date()
    private var lastBytes: Int64 = 0

    init(url: String, title: String? = nil) {
        self.url = url
        self.title = title ?? url
    }

    func ingest(_ line: String) {
        if line.hasPrefix("{\"type\"") {
            if let data = line.data(using: .utf8),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let type = obj["type"] as? String {
                switch type {
                case "progress":
                    done = (obj["done"] as? NSNumber)?.int64Value ?? done
                    total = (obj["total"] as? NSNumber)?.int64Value ?? total
                    now()
                case "start":
                    phase = .running
                case "done":
                    finishedPath = obj["path"] as? String
                    total = (obj["size"] as? NSNumber)?.int64Value ?? total
                    done = total
                    phase = .done
                case "error":
                    log += "✗ \(obj["message"] as? String ?? "error")\n"
                    phase = .failed
                default:
                    break
                }
                if obj["path"] != nil && type == "done" { /* handled */ }
                return
            }
        }
        appendLog(line)
        if line.contains("[download]") && phase != .running {
            phase = .running
        }
    }

    private func now() {
        let t = Date()
        let dt = t.timeIntervalSince(lastTick)
        let db = done - lastBytes
        if dt > 0.4 {
            let inst = Double(db) / dt
            bytesPerSecond = bytesPerSecond > 0 ? bytesPerSecond * 0.6 + inst * 0.4 : inst
            lastTick = t
            lastBytes = done
        }
    }

    func appendLog(_ line: String) {
        log += line + "\n"
        if log.count > 40_000 {
            log = String(log.suffix(20_000))
        }
    }

    var percent: Double {
        guard total > 0 else { return -1 }
        return min(100, Double(done) * 100 / Double(total))
    }

    var speedText: String {
        guard bytesPerSecond > 1 else { return "" }
        let units: [(String, Double)] = [("GB/s", 1_073_741_824.0), ("MB/s", 1_048_576.0), ("KB/s", 1024.0)]
        for (name, div) in units where bytesPerSecond >= div {
            return String(format: "%.1f %@", bytesPerSecond / div, name)
        }
        return String(format: "%.0f B/s", bytesPerSecond)
    }

    func signalStop() {
        guard let process, process.isRunning else { return }
        kill(process.processIdentifier, SIGTERM)  // mgd handler saves state
    }
}

// MARK: - store

final class JobStore: ObservableObject {
    @Published var jobs: [DownloadJob] = []
    @Published var pausedList: [PausedItem] = []
    @Published var destination: String = "~/Downloads/mgd"
    @Published var autoDestination = true
    @Published var statusFooter = ""

    struct PausedItem: Identifiable {
        let id: Int          // index+1 as in `mgd list`
        var url: String
        var filename: String
        var outPath: String
        var size: Int64
        var done: Int64
        var state: String
    }

    func start(_ job: DownloadJob, quality: String, category: String, destOverride: Bool) {
        var args = ["grab", job.url, "--json-progress", "--retries", "8"]
        if quality == "audio" {
            args += ["--quality", "audio"]
        } else if quality != "auto" {
            args += ["--quality", quality]
        }
        if category != "auto" {
            args += ["-t", category]
        }
        if destOverride {
            let expanded = (destination as NSString).expandingTildeInPath
            args += ["-d", expanded]
        }
        job.phase = .running
        jobs.insert(job, at: 0)
        JobStore.refreshPausedList { [weak self] items in
            DispatchQueue.main.async { self?.pausedList = items }
        }
        job.process = MGDPaths.spawn(args, onLine: { [weak job] line in
            guard let job else { return }
            job.ingest(line)
        }, onEnd: { [weak job, weak self] code in
            guard let job else { return }
            if job.phase == .running {
                job.phase = code == 0 ? .done : (code == 130 ? .paused : .failed)
                if code == 130 { job.appendLog("paused (state saved)") }
            }
            self?.refreshPaused()
            self?.statusFooter = "\(job.title): \(job.phase.rawValue)"
        })
    }

    func refreshPaused() {
        JobStore.refreshPausedList { [weak self] items in
            DispatchQueue.main.async { self?.pausedList = items }
        }
    }

    static func refreshPausedList(_ cb: @escaping ([PausedItem]) -> Void) {
        Task {
            let out = await MGDPaths.run(["list", "--json"])
            var items: [PausedItem] = []
            if let data = out.data(using: .utf8),
               let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
               let arr = obj["items"] as? [[String: Any]] {
                for (i, it) in arr.enumerated() {
                    if (it["state"] as? String) == "running" { continue }
                    items.append(PausedItem(id: i + 1,
                                            url: it["url"] as? String ?? "",
                                            filename: it["filename"] as? String ?? "",
                                            outPath: it["out_path"] as? String ?? "",
                                            size: (it["size"] as? NSNumber)?.int64Value ?? 0,
                                            done: (it["done"] as? NSNumber)?.int64Value ?? 0,
                                            state: it["state"] as? String ?? "paused"))
                }
            }
            cb(items)
        }
    }

    func resume(item: PausedItem) {
        let job = DownloadJob(url: item.url, title: item.filename)
        job.phase = .running
        jobs.insert(job, at: 0)
        job.process = MGDPaths.spawn(["resume", "\(item.id)"], onLine: { [weak job] line in
            job?.ingest(line)
        }, onEnd: { [weak job, weak self] code in
            guard let job else { return }
            job.phase = code == 0 ? .done : .failed
            self?.refreshPaused()
        })
    }

    func cancel(item: PausedItem) {
        Task {
            _ = await MGDPaths.run(["cancel", "\(item.id)"])
            await MainActor.run { self.refreshPaused() }
        }
    }
}

// MARK: - UI

@main
struct MediaGrabApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    var body: some Scene {
        WindowGroup("MediaGrab") {
            ContentView()
                .frame(minWidth: 680, minHeight: 520)
        }
        .windowStyle(.automatic)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

struct DownloadRow: View {
    @ObservedObject var job: DownloadJob

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                phaseBadge
                Text(job.title).lineLimit(1)
                Spacer()
                if !job.speedText.isEmpty && job.phase == .running {
                    Text(job.speedText).foregroundColor(.secondary)
                }
            }
            if job.total > 0 {
                GeometryReader { geo in
                    ZStack(alignment: .leading) {
                        Capsule().fill(Color.secondary.opacity(0.18))
                        Capsule().fill(colorForPhase).frame(width: max(4, geo.size.width * job.percent / 100))
                    }
                }
                .frame(height: 6)
                Text("\(fmtBytes(job.done)) / \(fmtBytes(job.total)) — \(Int(max(0, job.percent)))%")
                    .font(.caption).foregroundColor(.secondary)
            }
            if let path = job.finishedPath, job.phase == .done {
                Button("Reveal in Finder") {
                    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
                }
                .font(.caption)
            }
            if !job.log.isEmpty && job.phase != .done {
                Text(job.log.suffix(400))
                    .font(.system(size: 10, design: .monospaced))
                    .foregroundColor(.secondary)
                    .lineLimit(4)
            }
        }
        .padding(.vertical, 2)
    }

    private var colorForPhase: Color {
        switch job.phase {
        case .running, .queued: return .accentColor
        case .paused: return .yellow
        case .done: return .green
        case .failed, .cancelled: return .red
        }
    }

    private var phaseBadge: some View {
        Text(rawBadge)
            .font(.caption2.bold())
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(colorForPhase.opacity(0.15))
            .cornerRadius(4)
    }

    private var rawBadge: String {
        switch job.phase {
        case .running: return "RUNNING"
        case .paused: return "PAUSED"
        case .done: return "DONE"
        case .failed: return "FAILED"
        case .cancelled: return "CANCELLED"
        case .queued: return "QUEUED"
        }
    }

    private func fmtBytes(_ n: Int64) -> String {
        let v = Double(n)
        let units: [(String, Double)] = [("GB", 1_073_741_824.0), ("MB", 1_048_576.0), ("KB", 1024.0)]
        for (name, div) in units where v >= div {
            return String(format: "%.1f %@", v / div, name)
        }
        return "\(n) B"
    }
}

final class Session: ObservableObject {
    @Published var urlString = ""
    @Published var quality = "auto"
    @Published var category = "auto"
    @Published var showLog = false
}

struct ContentView: View {
    @StateObject private var store = JobStore()
    @StateObject private var session = Session()
    private let qualities = [("auto", "Automatic / ask engine"), ("auto-menu", "Let engine pick best"),
                             ("best", "Highest quality"), ("1080", "1080p"), ("720", "720p"),
                             ("480", "480p"), ("360", "360p"), ("audio", "Audio only (mp3)")]
    private let categories = ["auto", "video", "audio", "image", "archive",
                              "document", "software", "other"]

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack {
                TextField("Paste any file, page, YouTube or X link…",
                          text: $session.urlString)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(download)
                Button("Download") { download() }
                    .keyboardShortcut(.defaultAction)
                    .disabled(session.urlString.isEmpty)
            }
            .padding(.horizontal)

            HStack(spacing: 10) {
                Picker("Quality:", selection: $session.quality) {
                    ForEach(qualities, id: \.0) { Text($0.1).tag($0.0) }
                }
                .frame(maxWidth: 300)
                Picker("Save type:", selection: $session.category) {
                    ForEach(categories, id: \.self) { Text($0).tag($0) }
                }
                Spacer()
                Text("Destination: ~/Downloads/mgd (set with `mgd set-default`)")
                    .font(.caption).foregroundColor(.secondary)
            }
            .padding(.horizontal)

            Divider()
            if store.jobs.isEmpty && store.pausedList.isEmpty {
                VStack(spacing: 8) {
                    Text("Nothing downloading yet").foregroundColor(.secondary)
                    Text("Pause with ⌘. or the Pause button; resume anytime from mgd resume, or from this window.")
                        .font(.caption).foregroundColor(.secondary)
                }.frame(maxWidth: .infinity).padding(.top, 40)
            }
            List {
                ForEach(store.jobs.filter { $0.phase == .running || $0.phase == .queued }) { job in
                    DownloadRow(job: job)
                        .contextMenu {
                            Button("Pause") { job.signalStop() }
                        }
                }
                ForEach(store.pausedList) { item in
                    HStack {
                        Text("[paused]").foregroundColor(.yellow).font(.caption.bold())
                        VStack(alignment: .leading) {
                            Text(item.filename.isEmpty ? item.url : item.filename).lineLimit(1)
                            Text("\(fmt(item.done)) / \(fmt(item.size)) — \(item.url)")
                                .font(.caption).foregroundColor(.secondary).lineLimit(1)
                        }
                        Spacer()
                        Button("Resume") { store.resume(item: item) }
                        Button("Cancel") { store.cancel(item: item) }.foregroundColor(.red)
                    }
                }
                ForEach(store.jobs.filter { $0.phase == .done || $0.phase == .failed }) { job in
                    DownloadRow(job: job)
                }
            }

            Toggle("Show engine log", isOn: $session.showLog).font(.caption)
                .padding(.horizontal)
            if session.showLog {
                ScrollView {
                    Text(latestLog)
                        .font(.system(size: 10, design: .monospaced))
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .textSelection(.enabled)
                }
                .frame(height: 120)
                .background(Color(NSColor.textBackgroundColor))
            }
            Text(store.statusFooter).font(.caption).foregroundColor(.secondary)
        }
        .padding(.vertical)
        .onAppear { store.refreshPaused() }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button("Refresh") { store.refreshPaused() }
            }
            ToolbarItem {
                Button("Pause all") {
                    for j in store.jobs where j.phase == .running || j.phase == .queued {
                        j.signalStop()
                    }
                }
            }
            ToolbarItem {
                Menu("Diagnostics") {
                    Button("mgd doctor") {
                        Task { store.statusFooter = await MGDPaths.run(["doctor"]) }
                    }
                    Button("mgd defaults") {
                        Task { store.statusFooter = await MGDPaths.run(["defaults"]) }
                    }
                }
            }
        }
    }

    var latestLog: String {
        store.jobs.first?.log ?? "no output yet"
    }

    private func fmt(_ n: Int64) -> String {
        let v = Double(n)
        let units: [(String, Double)] = [("GB", 1_073_741_824.0), ("MB", 1_048_576.0), ("KB", 1024.0)]
        for (name, div) in units where v >= div {
            return String(format: "%.1f %@", v / div, name)
        }
        return "\(n) B"
    }

    private func download() {
        let url = session.urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty else { return }
        session.urlString = ""
        store.start(DownloadJob(url: url),
                    quality: session.quality == "auto-menu" ? "auto" : session.quality,
                    category: session.category,
                    destOverride: false)
    }
}