import Foundation
import SwiftUI

// MARK: - one download

enum JobPhase: String {
    case queued, running, paused, done, failed

    var isActive: Bool { self == .queued || self == .running || self == .paused }
    var isFinished: Bool { self == .done || self == .failed }
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
    /// the reason shown on a failed row (last engine error)
    @Published var errorMessage: String?
    /// what kind of failure: "link_expired" / "link_mismatch" offer Update Link
    @Published var errorKind: String?
    /// making the finished file play smoothly on a Mac (engine "convert" events)
    @Published var converting = false
    @Published var convertProgress: Double = 0
    var convertAction: String?
    var convertReasons: [String] = []
    var convertStarted: Date?

    /// the engine's stable id for this download (`mgd pause|resume|cancel
    /// <id>`); nil for yt-dlp-backed downloads, which have no state file
    var engineID: String?
    /// where the engine is saving it (known as soon as the download starts)
    var outPath: String?
    /// the `mgd` arguments that started it: re-run to retry, or to resume a
    /// yt-dlp download (yt-dlp continues its own partial files)
    var args: [String] = []
    /// true when the download was started outside this window (extension
    /// headless mode, a terminal, a previous app session): the app follows
    /// it through `mgd list` instead of a pipe
    var adopted = false
    var isTorrent = false
    /// page thumbnail (batch sheet / page-media); kept for history rows
    var poster: String?
    /// when the download finished (history list, newest first)
    var finishedAt: Date?
    var process: Process?
    /// set when the user asked for a pause, so the process ending is not
    /// mistaken for a failure
    var stopRequested = false
    /// set when the user cancelled: once the engine has stopped, its partial
    /// data is deleted and the row removed
    var cancelRequested = false
    /// a replacement link the user gave for an expired one; used by the
    /// next launch (`mgd resume <id> --url <link>`)
    var relinkURL: String?
    /// automatic retry after a connection problem: when (nil = none planned)
    @Published var retryAt: Date?
    /// automatic retries made since the download last made it through
    var autoRetries = 0
    /// since when the engine's state for this running download is gone
    /// while the app has not heard it finish (self-healing, see apply())
    var stateMissingSince: Date?
    /// "Download complete" was already announced for this run
    var completionAnnounced = false

    var connectionProblem: Bool { errorKind == "network" || errorKind == "server" }
    /// what to start instead when a link sent for this download turns out
    /// to be a different file
    var fallbackRequest: DownloadRequest?

    /// The engine still has this download's saved part, so a new link to the
    /// same file can continue it.
    var canUpdateLink: Bool {
        guard engineID != nil, !isTorrent, phase == .paused || phase == .failed,
              let out = outPath, !out.isEmpty else { return false }
        return FileManager.default.fileExists(atPath: out + ".mgdstate")
    }

    var linkExpired: Bool { errorKind == "link_expired" || errorKind == "link_mismatch" }
    /// fallback path parsed from yt-dlp's "[download] Destination: …" output
    var candidatePath: String?
    private var lastTick = Date()
    private var lastBytes: Int64 = 0

    init(url: String, title: String? = nil, poster: String? = nil) {
        self.url = url
        self.title = (title?.isEmpty == false) ? title! : url
        self.poster = poster
    }

    /// Sidebar category (from the file being written, else the title/url).
    var category: String {
        if isTorrent { return "Torrents" }
        for candidate in [finishedPath, outPath, candidatePath, title] {
            if let c = candidate, !c.isEmpty {
                let cat = MediaGrabCategory.category(forPath: c)
                if cat != "Other" { return cat }
            }
        }
        return MediaGrabCategory.category(forPath: Naming.fileName(fromURL: url))
    }

    /// The file-type symbol for rows.
    var symbol: String {
        if isTorrent { return "arrow.down.circle.dotted" }
        return fileSymbol(forPath: finishedPath ?? outPath ?? candidatePath ?? title)
    }

    func ingest(_ line: String) {
        if line.hasPrefix("{\"type\""),
           let data = line.data(using: .utf8),
           let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
           let type = obj["type"] as? String {
            switch type {
            case "progress":
                let newDone = (obj["done"] as? NSNumber)?.int64Value ?? done
                let newTotal = (obj["total"] as? NSNumber)?.int64Value ?? total
                // a total smaller than what already arrived is no total
                // (older engines reported a stream playlist's ~1 KB size)
                total = newTotal >= newDone ? newTotal : 0
                setProgress(newDone)
            case "start":
                phase = .running
                errorMessage = nil
                errorKind = nil
                if let id = obj["id"] as? String, !id.isEmpty { engineID = id }
                if let path = obj["out_path"] as? String, !path.isEmpty { outPath = path }
            case "done":
                finishedPath = obj["path"] as? String
                let size = (obj["size"] as? NSNumber)?.int64Value ?? 0
                finishedSize = size > 0 ? size : max(total, done)
                total = finishedSize
                done = total
                finishedAt = Date()
                errorMessage = nil
                errorKind = nil
                phase = .done
            case "error":
                let message = obj["message"] as? String ?? "download failed"
                errorMessage = message
                errorKind = obj["kind"] as? String
                appendLog("✗ \(message)")
            case "note":
                if let message = obj["message"] as? String { appendLog("• \(message)") }
            case "convert":
                ingestConvert(obj)
            case "paused":
                phase = .paused
            default:
                break
            }
            return
        }
        // yt-dlp passthrough: keep a candidate path for the finished file in
        // case the after_move event never fires (file already existed etc.)
        if let r = line.range(of: "[download] Destination: ") {
            candidatePath = String(line[r.upperBound...])
                .trimmingCharacters(in: .whitespaces)
        } else if line.contains("has already been downloaded") {
            let inner = line.replacingOccurrences(of: "[download] ", with: "")
            if let r = inner.range(of: " has already been downloaded") {
                candidatePath = String(inner[inner.startIndex..<r.lowerBound])
            }
        } else if line.hasPrefix("error:") || line.hasPrefix("ERROR:") {
            let message = String(line.dropFirst(6)).trimmingCharacters(in: .whitespaces)
            if !message.isEmpty, errorMessage == nil { errorMessage = message }
        }
        appendLog(line)
    }

    private func ingestConvert(_ obj: [String: Any]) {
        if let fraction = (obj["progress"] as? NSNumber)?.doubleValue {
            convertProgress = max(convertProgress, min(1, fraction))
            return
        }
        switch obj["stage"] as? String {
        case "start":
            converting = true
            convertProgress = 0
            convertStarted = Date()
            convertAction = obj["action"] as? String
            convertReasons = obj["reasons"] as? [String] ?? []
            appendLog("• making it play smoothly on a Mac: " + convertReasons.joined(separator: "; "))
        case "done":
            converting = false
            convertProgress = 1
            if let path = obj["path"] as? String {
                // a row named after its file follows the new name (clip.webm
                // -> clip.mp4); a name the user typed stays as it is
                let before = (outPath ?? finishedPath).map { ($0 as NSString).lastPathComponent }
                if let before, title == before { title = (path as NSString).lastPathComponent }
                finishedPath = path
                outPath = path
            }
            appendLog("• Mac-ready")
        case "failed":
            converting = false
            appendLog("✗ could not convert (\(obj["message"] as? String ?? "error")) - kept the file as downloaded")
        case "stopped":
            converting = false
            appendLog("• conversion skipped - kept the file as downloaded")
        default:
            break
        }
    }

    /// What the conversion is doing, for the row.
    var convertSummary: String {
        switch convertAction {
        case "remux": return "rewrapping for QuickTime (no quality loss)"
        case "audio": return "converting the audio"
        default: return "re-encoding with the Mac's hardware encoder"
        }
    }

    var convertETA: String {
        guard let start = convertStarted, convertProgress > 0.03, convertProgress < 1 else { return "" }
        let elapsed = Date().timeIntervalSince(start)
        let left = elapsed * (1 - convertProgress) / convertProgress
        return left.isFinite && left < 60 * 60 * 6 ? fmtDuration(left) + " left" : ""
    }

    /// Progress update (pipe events and `mgd list` polls) + speed estimate.
    func setProgress(_ newDone: Int64, at now: Date = Date()) {
        let dt = now.timeIntervalSince(lastTick)
        if newDone < lastBytes {            // restarted / second stream (audio)
            lastBytes = newDone
            lastTick = now
        } else if dt > 0.5 {
            let inst = Double(newDone - lastBytes) / dt
            bytesPerSecond = bytesPerSecond > 0 ? bytesPerSecond * 0.6 + inst * 0.4 : inst
            lastTick = now
            lastBytes = newDone
        }
        if newDone != done { done = newDone }
    }

    func resetSpeed() {
        bytesPerSecond = 0
        lastTick = Date()
        lastBytes = done
    }

    func appendLog(_ line: String) {
        guard !line.isEmpty else { return }
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
        bytesPerSecond > 1 ? fmtSpeed(bytesPerSecond) : ""
    }

    /// "1:05 left" once the speed has settled.
    var etaText: String {
        guard phase == .running, total > 0, done < total, bytesPerSecond > 1024 else { return "" }
        let seconds = Double(total - done) / bytesPerSecond
        guard seconds.isFinite, seconds < 60 * 60 * 48 else { return "" }
        return fmtDuration(seconds) + " left"
    }

    /// One line describing why it failed (for the row).
    var failureSummary: String {
        if let message = errorMessage, !message.isEmpty { return message }
        let lines = log.split(separator: "\n").map(String.init)
        return lines.last(where: { !$0.hasPrefix("#") && !$0.hasPrefix("{") }) ?? "download failed"
    }
}

// MARK: - formatting helpers

func fmtBytes(_ n: Int64) -> String {
    let v = Double(n)
    let units: [(String, Double)] = [("GB", 1_073_741_824.0), ("MB", 1_048_576.0), ("KB", 1024.0)]
    for (name, div) in units where v >= div {
        return String(format: "%.1f %@", v / div, name)
    }
    return "\(n) B"
}

func fmtSpeed(_ v: Double) -> String {
    let units: [(String, Double)] = [("GB/s", 1_073_741_824.0), ("MB/s", 1_048_576.0), ("KB/s", 1024.0)]
    for (name, div) in units where v >= div {
        return String(format: "%.1f %@", v / div, name)
    }
    return String(format: "%.0f B/s", v)
}

func fmtDuration(_ seconds: Double) -> String {
    let s = Int(seconds.rounded())
    if s >= 3600 { return String(format: "%d:%02d:%02d", s / 3600, (s % 3600) / 60, s % 60) }
    return String(format: "%d:%02d", s / 60, s % 60)
}

func fmtWhen(_ date: Date?) -> String {
    guard let date else { return "" }
    let f = RelativeDateTimeFormatter()
    f.unitsStyle = .abbreviated
    if abs(date.timeIntervalSinceNow) < 45 { return "just now" }
    return f.localizedString(for: date, relativeTo: Date())
}

/// Shorten a path for display: the home folder becomes "~".
func tildePath(_ path: String) -> String {
    let home = NSHomeDirectory()
    return path.hasPrefix(home + "/") ? "~" + path.dropFirst(home.count) : path
}

func fileSymbol(forPath path: String) -> String {
    switch MediaGrabCategory.category(forPath: path) {
    case "Video": return "film.fill"
    case "Audio": return "music.note"
    case "Images": return "photo.fill"
    case "Archives": return "archivebox.fill"
    case "Documents": return "doc.text.fill"
    default: return "arrow.down.doc.fill"
    }
}

/// FDM-style sidebar categories.
enum MediaGrabCategory {
    static let names = ["All", "Video", "Audio", "Images", "Archives", "Documents",
                        "Torrents", "Other"]

    static func category(forPath path: String) -> String {
        let ext = (path as NSString).pathExtension.lowercased()
        switch ext {
        case "mp4", "mov", "mkv", "webm", "avi", "m4v", "mpg", "mpeg", "ts", "flv", "wmv",
             "3gp", "m3u8", "mpd":
            return "Video"
        case "mp3", "m4a", "wav", "flac", "aac", "ogg", "opus", "wma":
            return "Audio"
        case "jpg", "jpeg", "png", "gif", "webp", "heic", "tiff", "bmp":
            return "Images"
        case "zip", "tar", "gz", "bz2", "xz", "7z", "rar", "dmg", "iso", "apk", "pkg":
            return "Archives"
        case "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "md", "rtf", "csv", "epub":
            return "Documents"
        default:
            return "Other"
        }
    }

    static func symbol(for name: String) -> String {
        switch name {
        case "All": return "tray.2"
        case "Video": return "film"
        case "Audio": return "music.note"
        case "Images": return "photo"
        case "Archives": return "archivebox"
        case "Documents": return "doc"
        case "Torrents": return "arrow.down.circle.dotted"
        default: return "questionmark.folder"
        }
    }
}

// MARK: - file names

enum Naming {
    private static let streamExtensions: Set<String> = ["m3u8", "mpd", "m3u"]

    /// The file name at the end of a URL: query and fragment dropped,
    /// percent-escapes decoded ("" when the path has none).
    static func fileName(fromURL url: String) -> String {
        var path = url
        if let comps = URLComponents(string: url), comps.scheme != nil {
            path = comps.percentEncodedPath
        } else {
            for sep in ["?", "#"] {
                if let r = path.range(of: sep) { path = String(path[..<r.lowerBound]) }
            }
        }
        let last = path.split(separator: "/", omittingEmptySubsequences: true).last.map(String.init) ?? ""
        return last.removingPercentEncoding ?? last
    }

    /// Characters that cannot be part of a file name replaced, never empty
    /// dots, trimmed.
    static func sanitize(_ raw: String) -> String {
        var out = ""
        for scalar in raw.unicodeScalars {
            if scalar == "/" || scalar == ":" || scalar == "\\" || scalar.value < 32 {
                out.append("-")
            } else {
                out.unicodeScalars.append(scalar)
            }
        }
        var trimmed = out.trimmingCharacters(in: .whitespacesAndNewlines)
        while trimmed.hasPrefix(".") { trimmed.removeFirst() }
        trimmed = trimmed.trimmingCharacters(in: .whitespaces)
        return String(trimmed.prefix(200))
    }

    /// What to pre-fill in the "Save as" sheet for a link. Prefers a title
    /// (page / video title) and falls back to the URL's own file name;
    /// playlist extensions (.m3u8) are dropped because the result is a video.
    static func suggested(forURL url: String, title: String? = nil) -> String {
        if let title, !sanitize(title).isEmpty {
            return stripStreamExtension(sanitize(title))
        }
        let fromURL = sanitize(fileName(fromURL: url))
        if !fromURL.isEmpty, fromURL.count <= 180 {
            let stripped = stripStreamExtension(fromURL)
            // "index"/"master" say nothing: name it after the site instead
            if !["index", "master", "playlist", "manifest", "video", "watch"].contains(stripped.lowercased()) {
                return stripped
            }
        }
        if let host = URLComponents(string: url)?.host, !host.isEmpty {
            let site = host.hasPrefix("www.") ? String(host.dropFirst(4)) : host
            return sanitize("\(site) download")
        }
        return "download"
    }

    static func stripStreamExtension(_ name: String) -> String {
        let ext = (name as NSString).pathExtension.lowercased()
        guard streamExtensions.contains(ext) else { return name }
        let base = (name as NSString).deletingPathExtension
        return base.isEmpty ? name : base
    }

    /// Two links to the same file on the same site that differ only in their
    /// query (signed links: ?token=…, ?secure=…,expiry).
    static func sameFileLink(_ a: String, _ b: String) -> Bool {
        guard let x = URLComponents(string: a), let y = URLComponents(string: b),
              let hx = x.host?.lowercased(), let hy = y.host?.lowercased(),
              hx == hy, x.path == y.path, x.path.count > 1 else { return false }
        return a != b
    }

    static func isValid(_ name: String) -> Bool {
        let t = name.trimmingCharacters(in: .whitespacesAndNewlines)
        return !t.isEmpty && !t.contains("/") && t != "." && t != ".."
    }

    static func isTorrent(_ link: String) -> Bool {
        let lower = link.lowercased()
        if lower.hasPrefix("magnet:") { return true }
        return fileName(fromURL: link).lowercased().hasSuffix(".torrent")
            || lower.hasSuffix(".torrent")
    }

    /// First http(s)/magnet link inside a piece of text (clipboard, drops).
    static func firstLink(in text: String) -> String? {
        allLinks(in: text, limit: 1).first
    }

    /// Every http(s)/magnet link in a piece of text, in order, without
    /// repeats (a pasted list, a links.txt file, an email).
    static func allLinks(in text: String, limit: Int = 200) -> [String] {
        var out: [String] = []
        var seen = Set<String>()
        let separators: (Character) -> Bool = { " \n\t\r\"'<>()[],;|".contains($0) }
        for raw in text.split(whereSeparator: separators) {
            var token = String(raw)
            while let last = token.last, ".!?".contains(last) { token.removeLast() }
            let lower = token.lowercased()
            let ok: Bool
            if lower.hasPrefix("magnet:?") {
                ok = true
            } else if lower.hasPrefix("http://") || lower.hasPrefix("https://"),
                      let comps = URLComponents(string: token), let host = comps.host {
                ok = host.contains(".") || host == "localhost"
            } else {
                ok = false
            }
            guard ok, seen.insert(token).inserted else { continue }
            out.append(token)
            if out.count >= limit { break }
        }
        return out
    }

    /// A link that names a file itself (…/clip.mp4, …/setup.dmg) rather
    /// than a page that has to be looked at.
    static func looksLikeFile(_ link: String) -> Bool {
        let ext = (fileName(fromURL: link) as NSString).pathExtension.lowercased()
        guard !ext.isEmpty, !["html", "htm", "php", "asp", "aspx", "jsp"].contains(ext) else {
            return false
        }
        return MediaGrabCategory.category(forPath: "x." + ext) != "Other"
            || ["bin", "exe", "msi", "apk", "srt", "vtt", "m3u8", "mpd"].contains(ext)
    }
}

// MARK: - persistence model

/// One persisted row (~/Library/Application Support/MediaGrab/history.json).
/// Fields added over time are optional so older files keep loading.
struct HistoryEntry: Codable {
    var url: String
    var title: String
    var phase: String
    var done: Int64
    var total: Int64
    var finishedPath: String?
    var finishedSize: Int64
    var poster: String?
    /// ISO-8601 (kept lenient so hand-edited files still load)
    var finishedAt: String?
    var logTail: String?
    var args: [String]?
    var error: String?
    var torrent: Bool?
    /// engine id + target path of a download that can still be resumed
    var engineID: String?
    var outPath: String?
    var errorKind: String?
}

extension HistoryEntry {
    var finishedDate: Date? {
        guard let finishedAt, !finishedAt.isEmpty else { return nil }
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        if let d = f.date(from: finishedAt) { return d }
        let f2 = ISO8601DateFormatter()
        if let d = f2.date(from: finishedAt) { return d }
        let df = DateFormatter()
        df.locale = Locale(identifier: "en_US_POSIX")
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss.SSSSSS"
        if let d = df.date(from: finishedAt) { return d }
        df.dateFormat = "yyyy-MM-dd'T'HH:mm:ss"
        return df.date(from: finishedAt)
    }
}
