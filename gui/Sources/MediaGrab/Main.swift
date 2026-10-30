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

    /// Blocking one-shot call meant for detached execution.
    static func runBlocking(_ args: [String]) -> String {
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

    static func run(_ args: [String]) async -> String {
        await Task.detached { MGDPaths.runBlocking(args) }.value
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
            guard !data.isEmpty else { return }
            buf += data
            while let nl = buf.firstIndex(of: 0x0A) {
                let lineData = Data(buf[buf.startIndex..<nl])
                buf.removeSubrange(buf.startIndex..<buf.index(after: nl))
                let line = String(bytes: lineData, encoding: .utf8) ?? ""
                DispatchQueue.main.async { onLine(line) }
            }
        }
        outPipe.fileHandleForReading.readabilityHandler = { fh in
            handle(fh.availableData)
        }
        errPipe.fileHandleForReading.readabilityHandler = { fh in
            handle(fh.availableData)
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

// MARK: - mgd JSON payloads

struct ListEntry: Identifiable {
    var id: Int { index }   // 1-based id as used by `mgd pause/resume/cancel N`
    let index: Int
    let url: String
    let filename: String
    let done: Int64
    let size: Int64
    let state: String

    static func parse(_ stdout: String) -> [ListEntry] {
        guard let data = stdout.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let arr = obj["items"] as? [[String: Any]] else { return [] }
        return arr.enumerated().map { i, it in
            ListEntry(index: i + 1,
                      url: it["url"] as? String ?? "",
                      filename: it["filename"] as? String ?? "",
                      done: (it["done"] as? NSNumber)?.int64Value ?? 0,
                      size: (it["size"] as? NSNumber)?.int64Value ?? 0,
                      state: it["state"] as? String ?? "paused")
        }
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

    static func parse(url: String, _ stdout: String) -> QualityProbe {
        guard let data = stdout.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return QualityProbe(url: url, title: nil, source: nil, options: [],
                                error: "could not read quality info (mgd qualities failed)")
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
    @Published var finishedSize: Int64 = 0
    @Published var showLog = false
    var process: Process?
    /// fallback path parsed from yt-dlp's "[download] Destination: …" output
    var candidatePath: String?
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
                    finishedSize = (obj["size"] as? NSNumber)?.int64Value ?? total
                    total = finishedSize
                    done = total
                    phase = .done
                case "error":
                    log += "✗ \(obj["message"] as? String ?? "error")\n"
                    phase = .failed
                case "paused":
                    phase = .paused
                default:
                    break
                }
                return
            }
        }
        // yt-dlp passthrough: keep a candidate path for the finished file in
        // case the after_move event never fires (file already existed etc.)
        if let r = line.range(of: "[download] Destination: ") {
            candidatePath = String(line[r.upperBound...]).trimmingCharacters(
                in: CharacterSet.whitespaces)
        } else if line.contains("has already been downloaded") {
            let inner = line.replacingOccurrences(of: "[download] ", with: "")
            if let r = inner.range(of: " has already been downloaded") {
                candidatePath = String(inner[inner.startIndex..<r.lowerBound])
            }
        }
        appendLog(line)
    }

    func finalizeAfterExit(code: Int32) {
        if finishedPath == nil, let p = candidatePath, !p.isEmpty, code == 0 {
            finishedPath = p
            phase = .done
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

    /// SIGTERM -> mgd saves exact byte positions and exits (graceful pause).
    func pause() {
        guard let process, process.isRunning else { return }
        kill(process.processIdentifier, SIGTERM)
    }
}

// MARK: - helpers

func fmtBytes(_ n: Int64) -> String {
    let v = Double(n)
    let units: [(String, Double)] = [("GB", 1_073_741_824.0), ("MB", 1_048_576.0), ("KB", 1024.0)]
    for (name, div) in units where v >= div {
        return String(format: "%.1f %@", v / div, name)
    }
    return "\(n) B"
}

func fileSymbol(forPath path: String) -> String {
    let ext = (path as NSString).pathExtension.lowercased()
    switch ext {
    case "mp4", "mov", "mkv", "webm", "avi", "m4v":
        return "film.fill"
    case "mp3", "m4a", "wav", "flac", "aac":
        return "music.note"
    case "jpg", "jpeg", "png", "gif", "webp", "heic":
        return "photo.fill"
    case "zip", "tar", "gz", "7z", "rar", "dmg":
        return "archivebox"
    case "pdf", "doc", "docx", "txt", "md":
        return "doc.text.fill"
    default:
        return "arrow.down.doc.fill"
    }
}

func revealInFinder(_ path: String) {
    let url = URL(fileURLWithPath: path)
    if FileManager.default.fileExists(atPath: path) {
        NSWorkspace.shared.activateFileViewerSelecting([url])
    } else if let dir = url.deletingLastPathComponent() as URL? {
        NSWorkspace.shared.open(dir)
    }
}

// MARK: - store

final class JobStore: ObservableObject {
    @Published var jobs: [DownloadJob] = []
    @Published var pausedList: [ListEntry] = []   // paused elsewhere / before launch
    @Published var statusFooter = ""

    // actions: start / pause / resume

    func start(_ job: DownloadJob, quality: String, category: String) {
        var args = ["grab", job.url, "--json-progress", "--retries", "8"]
        if quality == "audio" {
            args += ["--quality", "audio"]
        } else if quality != "auto" && quality != "best" {
            args += ["--quality", quality]
        }
        if category != "auto" {
            args += ["-t", category]
        }
        job.phase = .running
        jobs.insert(job, at: 0)
        refreshPaused()
        job.process = MGDPaths.spawn(args, onLine: { [weak job] line in
            job?.ingest(line)
        }, onEnd: { [weak job, weak self] code in
            guard let job, let self else { return }
            if job.phase == .running || job.phase == .queued {
                switch code {
                case 0:
                    job.phase = .done
                case 130:
                    job.phase = .paused
                    job.appendLog("paused — progress saved")
                default:
                    job.phase = .failed
                }
            }
            job.finalizeAfterExit(code: code)
            self.refreshPaused()
            self.statusFooter = "\(job.title): \(job.phase.rawValue)"
        })
    }

    /// Resume a job the app itself paused (id resolved by url from `mgd list`).
    func resume(job: DownloadJob) {
        Task {
            let out = await MGDPaths.run(["list", "--json"])
            let entries = ListEntry.parse(out)
            guard let entry = entries.first(where: { $0.url == job.url }) else {
                await MainActor.run {
                    job.appendLog("could not find that download in `mgd jobs`")
                    self.statusFooter = "resume failed: not found in mgd jobs"
                }
                return
            }
            await MainActor.run {
                job.phase = .running
                job.appendLog("resuming…")
                job.process = MGDPaths.spawn(["resume", "\(entry.index)", "--json-progress"],
                                             onLine: { [weak job] line in
                                                 job?.ingest(line)
                                             }, onEnd: { [weak job, weak self] code in
                    guard let job, let self else { return }
                    switch code {
                    case 0: job.phase = .done
                    case 130: job.phase = .paused
                    default: job.phase = .failed
                    }
                    job.finalizeAfterExit(code: code)
                    self.refreshPaused()
                })
                self.refreshPaused()
            }
        }
    }

    /// Resume one of the externally paused downloads (id known from list).
    func resumeExternal(entry: ListEntry) {
        let job = DownloadJob(url: entry.url,
                              title: entry.filename.isEmpty ? entry.url : entry.filename)
        job.done = entry.done
        job.total = entry.size
        job.phase = .running
        jobs.insert(job, at: 0)
        job.process = MGDPaths.spawn(["resume", "\(entry.index)", "--json-progress"],
                                     onLine: { [weak job] line in
                                         job?.ingest(line)
                                     }, onEnd: { [weak job, weak self] code in
            guard let job, let self else { return }
            switch code {
            case 0: job.phase = .done
            case 130: job.phase = .paused
            default: job.phase = .failed
            }
            job.finalizeAfterExit(code: code)
            self.refreshPaused()
        })
        refreshPaused()
    }

    /// Stop an in-app paused job and delete its partial data.
    func cancel(job: DownloadJob) {
        job.pause()   // stop the process if somehow still running
        Task {
            let out = await MGDPaths.run(["list", "--json"])
            if let entry = ListEntry.parse(out).first(where: { $0.url == job.url }) {
                _ = await MGDPaths.run(["cancel", "\(entry.index)"])
            }
            await MainActor.run {
                self.remove(job)
                self.statusFooter = "cancelled — partial data deleted"
                self.refreshPaused()
            }
        }
    }

    func cancel(entry: ListEntry) {
        Task {
            _ = await MGDPaths.run(["cancel", "\(entry.index)"])
            await MainActor.run { self.refreshPaused() }
        }
    }

    // actions: delete

    func remove(_ job: DownloadJob) {
        jobs.removeAll { $0.id == job.id }
    }

    /// Move the finished file to Trash, then drop the row.
    func trashFile(of job: DownloadJob) {
        let path = job.finishedPath
        remove(job)
        statusFooter = "removed from list: \(job.title)"
        guard let path, FileManager.default.fileExists(atPath: path) else { return }
        NSWorkspace.shared.recycle([URL(fileURLWithPath: path)]) { _, err in
            DispatchQueue.main.async {
                self.statusFooter = err == nil
                    ? "moved to Trash: \((path as NSString).lastPathComponent)"
                    : "could not delete file: \(err!.localizedDescription)"
            }
        }
    }

    func clearFinished() {
        jobs.removeAll { $0.phase == .done || $0.phase == .failed || $0.phase == .cancelled }
        statusFooter = "cleared completed downloads"
    }

    func refreshPaused() {
        Task.detached {
            let out = await MGDPaths.run(["list", "--json"])
            let items = ListEntry.parse(out).filter { $0.state != "running" }
            await MainActor.run { self.pausedList = items }
        }
    }
}

// MARK: - app

@main
struct MediaGrabApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    var body: some Scene {
        WindowGroup("MediaGrab") {
            ContentView()
                .frame(minWidth: 740, minHeight: 580)
        }
        .windowStyle(.automatic)
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        true
    }
}

// MARK: - UI pieces

func card<Content: View>(_ content: Content) -> some View {
    content
        .padding(.vertical, 10)
        .padding(.horizontal, 12)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(
            RoundedRectangle(cornerRadius: 12)
                .fill(Color(NSColor.controlBackgroundColor))
                .shadow(color: Color.black.opacity(0.06), radius: 2, y: 1)
        )
}

struct SectionHeader: View {
    let title: String
    let count: Int
    let systemImage: String
    let color: Color

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: systemImage)
                .foregroundColor(color)
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundColor(.secondary)
            Text("\(count)")
                .font(.caption2.weight(.bold))
                .foregroundColor(.white)
                .padding(.horizontal, 5).padding(.vertical, 1)
                .background(Capsule().fill(color.opacity(0.8)))
            Spacer()
        }
        .padding(.top, 6)
    }
}

struct ProgressBar: View {
    let fraction: Double
    let color: Color

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                Capsule()
                    .fill(color)
                    .frame(width: max(4, geo.size.width * min(1, max(0, fraction))))
                    .opacity(fraction < 0 ? 0 : 1)
            }
        }
        .frame(height: 6)
    }
}

struct ActiveJobRow: View {
    @ObservedObject var job: DownloadJob
    var onResume: () -> Void
    var onCancel: () -> Void

    var body: some View {
        card(
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .center, spacing: 12) {
                    ZStack {
                        switch job.phase {
                        case .running:
                            ProgressView().controlSize(.regular)
                        case .paused:
                            Image(systemName: "pause.circle.fill")
                                .font(.system(size: 22)).foregroundColor(.orange)
                        case .queued:
                            Image(systemName: "clock.fill")
                                .font(.system(size: 18)).foregroundColor(.secondary)
                        default:
                            Image(systemName: "circle.dashed")
                                .font(.system(size: 18)).foregroundColor(.secondary)
                        }
                    }
                    .frame(width: 30)

                    VStack(alignment: .leading, spacing: 4) {
                        Text(job.title).lineLimit(1).font(.body.weight(.medium))
                        HStack(spacing: 10) {
                            if job.total > 0 {
                                Text("\(fmtBytes(job.done)) / \(fmtBytes(job.total)) — \(Int(max(0, job.percent)))%")
                            }
                            if job.phase == .running, !job.speedText.isEmpty {
                                Text("· \(job.speedText)")
                            }
                            if job.phase == .paused {
                                Text("· paused — resume any time")
                            }
                        }
                        .font(.caption).foregroundColor(.secondary)
                        if job.total > 0 {
                            ProgressBar(fraction: job.percent / 100,
                                        color: job.phase == .paused ? .orange : .accentColor)
                        }
                    }

                    Spacer()

                    HStack(spacing: 6) {
                        if job.phase == .running || job.phase == .queued {
                            Button {
                                job.pause()
                            } label: {
                                Label("Pause", systemImage: "pause.fill")
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .help("Pause and save progress (you can also pause from any terminal: mgd pause)")
                        } else if job.phase == .paused {
                            Button {
                                onResume()
                            } label: {
                                Label("Resume", systemImage: "play.fill")
                            }
                            .buttonStyle(.borderedProminent)
                            .controlSize(.small)
                            .help("Continue from saved progress")
                            Button {
                                onCancel()
                            } label: {
                                Label("Delete", systemImage: "trash")
                            }
                            .buttonStyle(.bordered)
                            .controlSize(.small)
                            .help("Stop and delete the partial data")
                        }
                        Button {
                            job.showLog.toggle()
                        } label: {
                            Image(systemName: job.showLog ? "chevron.up" : "chevron.down")
                        }
                        .buttonStyle(.borderless)
                        .controlSize(.small)
                        .help("Details")
                    }
                }
                if job.showLog {
                    Text(job.log.suffix(700))
                        .font(.system(size: 10, design: .monospaced))
                        .foregroundColor(.secondary)
                        .lineLimit(7)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        )
    }
}

struct ExternalPausedRow: View {
    let entry: ListEntry
    var onResume: () -> Void
    var onCancel: () -> Void

    var body: some View {
        card(
            HStack(alignment: .center, spacing: 12) {
                Image(systemName: "pause.circle.fill")
                    .font(.system(size: 22)).foregroundColor(.orange)
                    .padding(.leading, 8)
                VStack(alignment: .leading, spacing: 3) {
                    Text(entry.filename.isEmpty ? entry.url : entry.filename)
                        .lineLimit(1).font(.body.weight(.medium))
                    Text("\(fmtBytes(entry.done)) / \(fmtBytes(entry.size)) — \(entry.url)")
                        .font(.caption).foregroundColor(.secondary).lineLimit(1)
                }
                Spacer()
                Button {
                    onResume()
                } label: {
                    Label("Resume", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                Button {
                    onCancel()
                } label: {
                    Label("Delete", systemImage: "trash")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Stop and delete the partial data")
            }
        )
    }
}

struct FinishedJobRow: View {
    @ObservedObject var job: DownloadJob
    var onRemove: () -> Void
    var onTrashFile: () -> Void

    var body: some View {
        card(
            HStack(alignment: .center, spacing: 12) {
                if job.phase == .done, let path = job.finishedPath {
                    Button {
                        revealInFinder(path)
                    } label: {
                        Image(systemName: fileSymbol(forPath: path))
                            .font(.system(size: 26))
                            .foregroundColor(.accentColor)
                    }
                    .buttonStyle(.borderless)
                    .help("Open the folder containing this file")
                } else {
                    Image(systemName: job.phase == .failed
                          ? "exclamationmark.triangle.fill" : "xmark.circle.fill")
                        .font(.system(size: 24)).foregroundColor(.red)
                }

                VStack(alignment: .leading, spacing: 3) {
                    HStack(spacing: 6) {
                        if job.phase == .done {
                            Image(systemName: "checkmark.circle.fill")
                                .font(.caption).foregroundColor(.green)
                        }
                        Text(job.title).lineLimit(1).font(.body.weight(.medium))
                    }
                    if job.phase == .done, let path = job.finishedPath {
                        Text("\(path) · \(fmtBytes(job.finishedSize > 0 ? job.finishedSize : job.total))")
                            .font(.caption).foregroundColor(.secondary).lineLimit(1)
                    } else if job.phase == .failed || job.phase == .cancelled {
                        Text(job.log.suffix(220).replacingOccurrences(of: "\n", with: " → "))
                            .font(.caption).foregroundColor(.secondary).lineLimit(1)
                    }
                }

                Spacer()

                if job.phase == .done, let path = job.finishedPath {
                    Button {
                        revealInFinder(path)
                    } label: {
                        Label("Show", systemImage: "folder")
                    }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .help("Open the folder containing this file")
                }
                Menu {
                    Button("Remove from list", action: onRemove)
                        .help("Keep the file, forget this row")
                    if job.finishedPath != nil {
                        Divider()
                        Button("Delete file (moves to Trash)", action: onTrashFile)
                            .help("Move the downloaded file to the Trash")
                    }
                } label: {
                    Image(systemName: "trash")
                }
                .menuStyle(.borderlessButton)
                .frame(width: 30)
                .fixedSize()
                .help("Delete options")
            }
        )
    }
}

// MARK: - quality chooser sheet

struct QualitySheet: View {
    let probe: QualityProbe
    var onStart: (String) -> Void
    var onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Choose a quality")
                .font(.headline)
            if let title = probe.title, !title.isEmpty {
                Text(title).font(.subheadline).foregroundColor(.secondary).lineLimit(2)
            }

            if !probe.options.isEmpty {
                ScrollView {
                    VStack(spacing: 6) {
                        ForEach(probe.options) { opt in
                            Button {
                                onStart(opt.quality)
                            } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: opt.quality == "audio"
                                          ? "music.note"
                                          : opt.quality == "best" ? "sparkles" : "film")
                                        .foregroundColor(.accentColor)
                                        .frame(width: 22)
                                    Text(opt.label)
                                        .font(.body)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    Image(systemName: "arrow.down.circle")
                                        .foregroundColor(.secondary.opacity(0.6))
                                }
                                .padding(.vertical, 8)
                                .padding(.horizontal, 12)
                                .background(
                                    RoundedRectangle(cornerRadius: 8)
                                        .fill(Color.accentColor.opacity(0.08))
                                )
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .frame(maxHeight: 260)
            } else if let err = probe.error {
                Label(err, systemImage: "exclamationmark.triangle")
                    .foregroundColor(.orange)
            }

            HStack {
                Spacer()
                if probe.error != nil || probe.options.isEmpty {
                    Button {
                        onStart("auto")
                    } label: {
                        Label("Try automatic anyway", systemImage: "bolt.horizontal")
                    }
                    .buttonStyle(.borderedProminent)
                    .help("Skip quality picking and let the engine decide")
                }
                Button("Close") { onClose() }
                    .keyboardShortcut(.cancelAction)
            }
        }
        .padding(20)
        .frame(width: 430)
    }
}

// MARK: - session + content view

final class Session: ObservableObject {
    @Published var urlString = ""
    @Published var showLog = false
    @Published var probing = false
    @Published var qualitySheet: QualityProbe?
}

struct ContentView: View {
    @StateObject private var store = JobStore()
    @StateObject private var session = Session()
    private let timer = Timer.publish(every: 3, on: .main, in: .common).autoconnect()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            header
            Divider()
            jobsArea
            Divider()
            footer
        }
        .padding(.vertical, 10)
        .onAppear { store.refreshPaused() }
        .onReceive(timer) { _ in store.refreshPaused() }
        .sheet(item: $session.qualitySheet) { probe in
            QualitySheet(probe: probe, onStart: { quality in
                begin(url: probe.url, quality: quality)
            }, onClose: { session.qualitySheet = nil })
        }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button {
                    store.refreshPaused()
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
            }
            ToolbarItem {
                Button {
                    for j in store.jobs where j.phase == .running || j.phase == .queued {
                        j.pause()
                    }
                } label: {
                    Label("Pause all", systemImage: "pause.circle")
                }
                .disabled(!store.jobs.contains { $0.phase == .running })
            }
            ToolbarItem {
                Button {
                    store.clearFinished()
                } label: {
                    Label("Clear completed", systemImage: "trash.slash")
                }
                .disabled(!store.jobs.contains {
                    $0.phase == .done || $0.phase == .failed || $0.phase == .cancelled
                })
            }
            ToolbarItem {
                Menu {
                    Button("mgd doctor") {
                        Task { store.statusFooter = await MGDPaths.run(["doctor"]) }
                    }
                    Button("mgd defaults") {
                        Task { store.statusFooter = await MGDPaths.run(["defaults"]) }
                    }
                    Button("Open download root") {
                        NSWorkspace.shared.open(URL(fileURLWithPath:
                                ("~/Downloads/mgd" as NSString).expandingTildeInPath))
                    }
                } label: {
                    Label("Diagnostics", systemImage: "stethoscope")
                }
            }
        }
    }

    private var header: some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 10) {
                Image(systemName: "arrow.down.circle.fill")
                    .font(.system(size: 30))
                    .foregroundColor(.accentColor)
                VStack(alignment: .leading, spacing: 1) {
                    Text("MediaGrab").font(.title3.bold())
                    Text("Segmented, resumable downloads — any file, page, YouTube or X/Twitter")
                        .font(.caption).foregroundColor(.secondary)
                }
                Spacer()
            }
            HStack(spacing: 8) {
                TextField("Paste a link: file, page, youtube.com, x.com …",
                          text: $session.urlString)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(grab)
                if session.probing {
                    ProgressView().controlSize(.small)
                }
                Button {
                    grab()
                } label: {
                    Label("Grab", systemImage: "arrow.down.circle.fill")
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(session.urlString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                          || session.probing)
            }
            Text("If the link has several resolutions, MediaGrab asks which one you want. " +
                 "Downloads can be paused and resumed at any time — even from another terminal.")
                .font(.caption).foregroundColor(.secondary)
        }
        .padding(.horizontal)
    }

    private var jobsArea: some View {
        let active = store.jobs.filter {
            $0.phase == .queued || $0.phase == .running || $0.phase == .paused
        }
        let finished = store.jobs.filter {
            $0.phase == .done || $0.phase == .failed || $0.phase == .cancelled
        }
        return Group {
            if active.isEmpty && finished.isEmpty && store.pausedList.isEmpty {
                VStack(spacing: 8) {
                    Image(systemName: "tray")
                        .font(.system(size: 34)).foregroundColor(.secondary.opacity(0.5))
                    Text("No downloads yet").foregroundColor(.secondary)
                    Text("Paste a link above to start. " +
                         "Finished files show a file icon — click it to open its folder.")
                        .font(.caption).foregroundColor(.secondary)
                        .multilineTextAlignment(.center)
                }
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 8) {
                        if !active.isEmpty {
                            SectionHeader(title: "Downloading", count: active.count,
                                          systemImage: "arrow.down.circle", color: .accentColor)
                            ForEach(active) { job in
                                ActiveJobRow(job: job,
                                             onResume: { store.resume(job: job) },
                                             onCancel: { store.cancel(job: job) })
                            }
                        }
                        if !store.pausedList.isEmpty {
                            SectionHeader(title: "Paused elsewhere",
                                          count: store.pausedList.count,
                                          systemImage: "pause.circle", color: .orange)
                            ForEach(store.pausedList) { entry in
                                ExternalPausedRow(entry: entry,
                                                  onResume: { store.resumeExternal(entry: entry) },
                                                  onCancel: { store.cancel(entry: entry) })
                            }
                        }
                        if !finished.isEmpty {
                            SectionHeader(title: "Completed", count: finished.count,
                                          systemImage: "checkmark.circle", color: .green)
                            ForEach(finished) { job in
                                FinishedJobRow(job: job,
                                               onRemove: { store.remove(job) },
                                               onTrashFile: { store.trashFile(of: job) })
                            }
                        }
                    }
                    .padding(.horizontal)
                    .padding(.bottom, 8)
                }
            }
        }
    }

    private var footer: some View {
        VStack(spacing: 6) {
            HStack {
                Toggle("Engine details", isOn: $session.showLog)
                    .font(.caption)
                    .toggleStyle(.checkbox)
                Spacer()
                Text(store.statusFooter.isEmpty
                     ? "Destination: ~/Downloads/mgd (change with `mgd set-default root …`)"
                     : store.statusFooter)
                    .font(.caption).foregroundColor(.secondary)
                    .lineLimit(1)
            }
            .padding(.horizontal)
            if session.showLog {
                ScrollViewReader { proxy in
                    ScrollView {
                        Text(latestLog)
                            .font(.system(size: 10, design: .monospaced))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .textSelection(.enabled)
                            .id("logBottom")
                    }
                    .frame(height: 110)
                    .background(Color(NSColor.textBackgroundColor))
                    .onChange(of: latestLog) { _ in
                        proxy.scrollTo("logBottom", anchor: .bottom)
                    }
                }
            }
        }
    }

    var latestLog: String {
        if let first = store.jobs.first, !first.log.isEmpty {
            return first.log
        }
        return "no engine output yet"
    }

    // MARK: grab flow

    private func grab() {
        let url = session.urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !url.isEmpty, !session.probing else { return }
        session.probing = true
        Task {
            let out = await MGDPaths.run(["qualities", url])
            await MainActor.run {
                session.probing = false
                session.urlString = ""
                let probe = QualityProbe.parse(url: url, out)
                if probe.options.count > 1 {
                    session.qualitySheet = probe
                } else if let err = probe.error, probe.options.isEmpty {
                    store.statusFooter = "quality check failed — \(err)"
                    session.qualitySheet = probe
                } else {
                    begin(url: url, quality: "auto")
                }
            }
        }
    }

    private func begin(url: String, quality: String) {
        session.qualitySheet = nil
        store.start(DownloadJob(url: url), quality: quality, category: "auto")
    }
}