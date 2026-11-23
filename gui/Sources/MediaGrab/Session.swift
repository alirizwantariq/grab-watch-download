import AppKit
import Combine
import Foundation
import SwiftUI

// MARK: - sheet payloads

/// A download waiting for the user to confirm its name and folder.
final class PendingDownload: ObservableObject, Identifiable {
    let id = UUID()
    let url: String
    let quality: String
    let referer: String?
    let poster: String?
    let cookies: String?
    @Published var name: String

    init(url: String, quality: String, name: String, referer: String? = nil,
         poster: String? = nil, cookies: String? = nil) {
        self.url = url
        self.quality = quality
        self.name = name
        self.referer = referer
        self.poster = poster
        self.cookies = cookies
    }
}

/// One media item found by `mgd page-media` (url/title/quality/poster/size).
struct BatchItem: Identifiable {
    let id = UUID()
    let url: String
    let title: String
    let quality: String
    let poster: String
    let size: Int64?

    init?(dict: [String: Any]) {
        guard let url = dict["url"] as? String, !url.isEmpty else { return nil }
        self.url = url
        self.title = (dict["title"] as? String) ?? ""
        self.quality = (dict["quality"] as? String) ?? ""
        self.poster = (dict["poster"] as? String) ?? ""
        self.size = (dict["size"] as? NSNumber)?.int64Value
    }

    var sizeText: String {
        guard let size, size > 0 else { return "size unknown" }
        return fmtBytes(size)
    }
}

/// Selection state of the multi-video picker.
final class BatchProbe: ObservableObject, Identifiable {
    let id = UUID()
    let url: String
    let items: [BatchItem]
    let cookies: String?
    @Published var selected: Set<UUID>

    init(url: String, items: [BatchItem], cookies: String? = nil) {
        self.url = url
        self.items = items
        self.cookies = cookies
        selected = Set(items.map(\.id))   // everything ticked by default
    }

    var chosen: [BatchItem] { items.filter { selected.contains($0.id) } }
    var totalSelectedBytes: Int64 { chosen.reduce(0) { $0 + ($1.size ?? 0) } }
}

/// One file inside a torrent (from `mgd torrent-info`).
struct TorrentFile: Identifiable {
    let id = UUID()
    let index: Int        // aria2 --select-file numbering
    let path: String
    let size: Int64
    let video: Bool

    init?(dict: [String: Any]) {
        guard let index = (dict["index"] as? NSNumber)?.intValue else { return nil }
        self.index = index
        self.path = (dict["path"] as? String) ?? ""
        self.size = (dict["size"] as? NSNumber)?.int64Value ?? 0
        self.video = (dict["video"] as? Bool) ?? false
    }
}

final class TorrentProbe: ObservableObject, Identifiable {
    let id = UUID()
    let url: String
    let name: String
    let files: [TorrentFile]
    @Published var selected: Set<UUID>

    init(url: String, name: String, files: [TorrentFile]) {
        self.url = url
        self.name = name
        self.files = files
        selected = Set(files.map(\.id))   // all ticked by default
    }

    var chosen: [TorrentFile] { files.filter { selected.contains($0.id) } }
    var hasVideo: Bool { files.contains { $0.video } }
    var totalSelectedBytes: Int64 { chosen.reduce(0) { $0 + $1.size } }
}

/// One link handed over by the browser extension, with an editable name.
final class HandoffItem: ObservableObject, Identifiable {
    let id = UUID()
    let url: String
    let referer: String?
    let kind: String          // file | stream | page
    /// private cookies.txt with the site login (never the cookies themselves)
    let cookies: String?
    @Published var name: String
    @Published var included = true

    init(url: String, name: String, referer: String?, kind: String, cookies: String? = nil) {
        self.url = url
        self.name = name
        self.referer = referer
        self.kind = kind
        self.cookies = cookies
    }
}

/// A batch the extension handed to the app (rename + destination sheet).
final class HandoffProbe: ObservableObject, Identifiable {
    let id = UUID()
    @Published var items: [HandoffItem] {
        didSet { watchItems() }
    }
    let destination: String?
    /// nil = sent from the browser; otherwise what the links came from
    /// ("the clipboard", "links.txt")
    let origin: String?
    private var watchers = Set<AnyCancellable>()

    init(items: [HandoffItem], destination: String?, origin: String? = nil) {
        self.items = items
        self.destination = destination
        self.origin = origin
        watchItems()
    }

    var title: String {
        if let origin {
            return items.count == 1 ? "1 link from \(origin)" : "\(items.count) links from \(origin)"
        }
        return items.count == 1 ? "Download from your browser"
            : "\(items.count) downloads from your browser"
    }

    /// Ticking or renaming one item changes the sheet's "Download N" button.
    private func watchItems() {
        watchers.removeAll()
        for item in items {
            item.objectWillChange
                .sink { [weak self] _ in self?.objectWillChange.send() }
                .store(in: &watchers)
        }
    }

    /// Ticked items with a usable name (an empty name means "automatic").
    var chosen: [HandoffItem] {
        items.filter { $0.included && ($0.name.isEmpty || Naming.isValid($0.name)) }
    }

    var source: String {
        if origin != nil {
            let hosts = Set(items.compactMap { URLComponents(string: $0.url)?.host })
            return hosts.count == 1 ? hosts.first! : "\(hosts.count) sites"
        }
        let pages = Set(items.compactMap(\.referer))
        if pages.count == 1, let only = pages.first { return only }
        return pages.isEmpty ? "sent from the browser" : "\(pages.count) pages"
    }

    /// Read (and consume) the hand-off the native host left for us.
    static func readPending() -> HandoffProbe? {
        let path = Engine.configDirectory.appendingPathComponent("handoff.json").path
        guard let data = FileManager.default.contents(atPath: path) else { return nil }
        try? FileManager.default.removeItem(atPath: path)   // consume
        guard let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else {
            return nil
        }
        let sharedReferer = obj["referer"] as? String
        var raw = obj["items"] as? [[String: Any]] ?? []
        if raw.isEmpty {   // older engine: plain url list
            raw = (obj["urls"] as? [String] ?? []).map { ["url": $0] }
        }
        var seen = Set<String>()
        let items: [HandoffItem] = raw.compactMap { it in
            guard let url = it["url"] as? String, !url.isEmpty,
                  seen.insert(url).inserted else { return nil }
            let kind = it["kind"] as? String ?? "file"
            let referer = (it["referer"] as? String) ?? sharedReferer
            let name = Naming.suggested(forURL: url, title: it["name"] as? String)
            let cookies = it["cookies"] as? String
            return HandoffItem(url: url, name: name,
                               referer: referer?.isEmpty == false ? referer : nil,
                               kind: kind,
                               cookies: cookies?.isEmpty == false ? cookies : nil)
        }
        guard !items.isEmpty else { return nil }
        let destination = obj["destination"] as? String
        return HandoffProbe(items: items,
                            destination: destination?.isEmpty == false ? destination : nil)
    }
}

/// Exactly one sheet is on screen at a time.
enum ActiveSheet: Identifiable {
    case quality(QualityProbe)
    case rename(PendingDownload)
    case batch(BatchProbe)
    case torrent(TorrentProbe)
    case handoff(HandoffProbe)
    case diagnostics
    case updateLink(DownloadJob)
    case extensionSetup

    var id: String {
        switch self {
        case .quality(let p): return "quality-\(p.id)"
        case .rename(let p): return "rename-\(p.id)"
        case .batch(let p): return "batch-\(p.id)"
        case .torrent(let p): return "torrent-\(p.id)"
        case .handoff(let p): return "handoff-\(p.id)"
        case .diagnostics: return "diagnostics"
        case .updateLink(let job): return "updatelink-\(job.id)"
        case .extensionSetup: return "extension"
        }
    }
}

/// A destructive action waiting for the user's OK.
struct Confirmation: Identifiable {
    let id = UUID()
    let title: String
    let message: String
    let actionLabel: String
    let action: () -> Void
}

// MARK: - window state + the "add a download" flows

/// UI state of the main window and the flows that turn a link into a
/// download: probe → (quality picker) → (page scan / batch picker) →
/// (name + folder) → JobStore. Main-thread only.
final class Session: ObservableObject {
    static let shared = Session()

    @Published var urlString = ""
    @Published var probing = false
    @Published var probingLabel = ""
    /// sidebar filter
    @Published var category = "All"
    @Published var searchText = ""
    /// nil = the engine's folders (root/<type>); otherwise the folder picked
    /// for the downloads being added now
    @Published var destination: String?
    /// true while a drag hovers the window
    @Published var dropActive = false
    @Published var sheet: ActiveSheet?
    @Published var confirmation: Confirmation?
    /// a link copied in another app, offered in a banner
    @Published var clipboardOffer: ClipboardOffer?

    lazy var clipboard: ClipboardWatcher = {
        #if DEBUG
        if UITestHooks.active {        // never touch the user's real clipboard
            return ClipboardWatcher(pasteboard: NSPasteboard(name: .init("mediagrab.uitest.clipboard")))
        }
        #endif
        return ClipboardWatcher()
    }()

    private var store: JobStore { JobStore.shared }
    private init() {}

    // MARK: entry points

    /// The Download button / Return in the link field.
    func grab() {
        let text = urlString.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        submit(text)
    }

    /// Any text that should contain a link (field, clipboard, drop).
    func submit(_ text: String, title: String? = nil, cookies: String? = nil) {
        guard !probing else {
            store.flash("Still checking the previous link…")
            return
        }
        let trimmed = text.trimmingCharacters(in: .whitespacesAndNewlines)
        // a local .torrent file path
        if trimmed.hasPrefix("/"), trimmed.lowercased().hasSuffix(".torrent"),
           FileManager.default.fileExists(atPath: trimmed) {
            urlString = ""
            scanTorrent(trimmed)
            return
        }
        let links = Naming.allLinks(in: trimmed)
        guard let link = links.first else {
            store.flash("That does not look like a link — paste an http(s) or magnet link")
            return
        }
        urlString = ""
        if links.count > 1 {
            addLinks(links, from: "the clipboard")
            return
        }
        if Naming.isTorrent(link) {
            scanTorrent(link)
        } else {
            probe(link, title: title, cookies: cookies)
        }
    }

    // MARK: clipboard watch

    func updateClipboardWatch() {
        if store.watchClipboard {
            clipboard.onOffer = { [weak self] offer in self?.offer(offer) }
            clipboard.start()
        } else {
            clipboard.stop()
            clipboardOffer = nil
        }
    }

    private func offer(_ offer: ClipboardOffer) {
        if store.autoDownloadClipboard, let link = offer.link, !probing {
            submit(link)
            store.flash("Downloading the copied link")
            return
        }
        clipboardOffer = offer
        NSApp.requestUserAttention(.informationalRequest)
    }

    func acceptClipboardOffer() {
        guard let offer = clipboardOffer else { return }
        clipboardOffer = nil
        if let link = offer.link {
            submit(link)
            return
        }
        // reading now, at the user's request (macOS may ask once)
        guard clipboard.pasteboard.changeCount == offer.changeCount,
              let text = clipboard.readText(), Naming.firstLink(in: text) != nil else {
            store.flash("The copied link could not be read (macOS did not allow it, or something else was copied since)")
            return
        }
        submit(text)
    }

    func pasteAndGrab() {
        guard let text = NSPasteboard.general.string(forType: .string),
              Naming.firstLink(in: text) != nil else {
            store.flash("No link on the clipboard")
            return
        }
        submit(text)
    }

    /// `mediagrab://handoff`, `magnet:` links and .torrent files opened with
    /// the app.
    func handleOpenURL(_ url: URL) {
        #if DEBUG
        let scripted = UITestHooks.active
        #else
        let scripted = false
        #endif
        if !scripted { NSApp.activate(ignoringOtherApps: true) }
        if url.isFileURL {
            if url.pathExtension.lowercased() == "torrent" { scanTorrent(url.path) }
            return
        }
        let link = url.absoluteString
        if (url.scheme ?? "").lowercased().hasPrefix("mediagrab") {
            receiveHandoff()
        } else if Naming.isTorrent(link) {
            scanTorrent(link)
        } else if Naming.firstLink(in: link) != nil {
            submit(link)
        }
    }

    // MARK: extension hand-off

    func receiveHandoff() {
        guard let incoming = HandoffProbe.readPending() else { return }
        if let d = incoming.destination { destination = d }
        // a single page link (YouTube & co.): same path as pasting it, so
        // the quality picker shows up
        if incoming.items.count == 1, let only = incoming.items.first, only.kind == "page" {
            if case .handoff = sheet {} else {
                submit(only.url, title: only.name, cookies: only.cookies)
                return
            }
        }
        if case .handoff(let open) = sheet {
            // the sheet is already up: add to it instead of replacing what
            // the user is in the middle of renaming
            let known = Set(open.items.map(\.url))
            let fresh = incoming.items.filter { !known.contains($0.url) }
            open.items.append(contentsOf: fresh)
            store.flash("Added \(fresh.count) more from the browser")
            return
        }
        if !store.askBeforeDownload {
            startHandoff(incoming)
            return
        }
        sheet = .handoff(incoming)
        let n = incoming.items.count
        store.flash("Received \(n) download\(n == 1 ? "" : "s") from the browser — confirm the names")
    }

    func startHandoff(_ probe: HandoffProbe) {
        let chosen = probe.chosen
        for item in chosen.reversed() {
            let name = item.name.trimmingCharacters(in: .whitespacesAndNewlines)
            var request = DownloadRequest(url: item.url,
                                          title: name.isEmpty ? Naming.suggested(forURL: item.url) : name,
                                          referer: item.referer,
                                          destination: destination,
                                          outName: name.isEmpty ? nil : name)
            request.cookies = item.cookies
            request.isTorrent = Naming.isTorrent(item.url)
            store.start(request)
        }
        sheet = nil
        store.flash("Added \(chosen.count) download\(chosen.count == 1 ? "" : "s")")
    }

    // MARK: several links at once

    /// A list of links (pasted, dropped, a links.txt): one sheet to name,
    /// untick and send them all. File links keep their file name; pages are
    /// named after their video automatically unless a name is typed.
    func addLinks(_ links: [String], from origin: String) {
        let items = links.map { link -> HandoffItem in
            let file = Naming.looksLikeFile(link)
            let kind = Naming.isTorrent(link) ? "torrent" : file ? "file" : "page"
            return HandoffItem(url: link,
                               name: file ? Naming.suggested(forURL: link) : "",
                               referer: nil, kind: kind)
        }
        let probe = HandoffProbe(items: items, destination: nil, origin: origin)
        if !store.askBeforeDownload {
            startHandoff(probe)
            return
        }
        sheet = .handoff(probe)
    }

    /// Links in a text file (links.txt, a saved list, an .m3u).
    func importLinks(from file: URL) {
        guard let attrs = try? FileManager.default.attributesOfItem(atPath: file.path),
              (attrs[.size] as? NSNumber)?.intValue ?? 0 <= 4_000_000,
              let data = FileManager.default.contents(atPath: file.path) else {
            store.flash("Could not read \(file.lastPathComponent)")
            return
        }
        let text = String(decoding: data, as: UTF8.self)
        let links = Naming.allLinks(in: text)
        guard !links.isEmpty else {
            store.flash("No links in \(file.lastPathComponent)")
            return
        }
        if links.count == 1 {
            submit(links[0])
        } else {
            addLinks(links, from: file.lastPathComponent)
        }
    }

    func chooseLinkFile() {
        let panel = NSOpenPanel()
        panel.allowedContentTypes = [.plainText, .text, .commaSeparatedText, .m3uPlaylist,
                                     .init(filenameExtension: "torrent") ?? .data]
        panel.allowsMultipleSelection = false
        panel.prompt = "Import"
        panel.message = "Choose a text file with links (one per line, or anywhere in the text)"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        if url.pathExtension.lowercased() == "torrent" {
            scanTorrent(url.path)
        } else {
            importLinks(from: url)
        }
    }

    // MARK: link → download

    private func probe(_ url: String, title: String?, cookies: String? = nil) {
        probing = true
        probingLabel = "Checking the link…"
        Task {
            let answer = await Engine.runJSON(["qualities", url] + cookieArgs(cookies))
            await MainActor.run {
                self.probing = false
                var result = QualityProbe.parse(url: url, answer)
                result.cookies = cookies
                if result.options.count > 1 {
                    self.sheet = .quality(result)
                } else if result.error != nil, result.options.isEmpty,
                          result.source != "direct" {
                    self.sheet = .quality(result)     // explains + "try anyway"
                } else if Self.isPlatform(url) {
                    self.begin(url: url, quality: "auto", title: result.title ?? title,
                               cookies: cookies)
                } else {
                    self.scanPage(url, title: title, cookies: cookies)
                }
            }
        }
    }

    /// Pages with several videos (albums, galleries) open a picker; a page
    /// with one video goes straight to the name sheet.
    private func scanPage(_ url: String, title: String?, cookies: String? = nil) {
        probing = true
        probingLabel = "Looking for media on the page…"
        Task {
            let answer = await Engine.runJSON(["page-media", url] + cookieArgs(cookies))
            await MainActor.run {
                self.probing = false
                let raw = answer?["items"] as? [[String: Any]] ?? []
                let items = raw.compactMap { BatchItem(dict: $0) }
                if items.count > 1 {
                    self.sheet = .batch(BatchProbe(url: url, items: items, cookies: cookies))
                } else if let item = items.first, item.url != url {
                    self.begin(url: item.url, quality: "auto",
                               title: title ?? (item.title.isEmpty ? nil : item.title),
                               referer: url,
                               poster: item.poster.isEmpty ? nil : item.poster,
                               cookies: cookies)
                } else {
                    // a plain file, or a page the engine resolves itself
                    self.begin(url: url, quality: "auto", title: title, cookies: cookies)
                }
            }
        }
    }

    func scanTorrent(_ url: String) {
        guard !probing else { return }
        probing = true
        probingLabel = "Reading the torrent (magnets can take a minute)…"
        Task {
            let answer = await Engine.runJSON(["torrent-info", url])
            await MainActor.run {
                self.probing = false
                let name = (answer?["name"] as? String) ?? url
                let raw = answer?["files"] as? [[String: Any]] ?? []
                let files = raw.compactMap { TorrentFile(dict: $0) }
                guard !files.isEmpty else {
                    let reason = (answer?["error"] as? String) ?? "no answer from the engine"
                    self.store.flash("Could not read this torrent: \(reason)", seconds: 10)
                    return
                }
                self.sheet = .torrent(TorrentProbe(url: url, name: name, files: files))
            }
        }
    }

    static func isPlatform(_ url: String) -> Bool {
        guard let host = URLComponents(string: url)?.host?.lowercased() else { return false }
        let platforms = ["youtube.com", "youtu.be", "x.com", "twitter.com", "instagram.com",
                         "tiktok.com", "vimeo.com", "dailymotion.com", "twitch.tv",
                         "facebook.com", "fb.watch", "reddit.com", "soundcloud.com"]
        return platforms.contains { host == $0 || host.hasSuffix("." + $0) }
    }

    /// Last step before a download exists: confirm the name (unless the
    /// user turned that off).
    func begin(url: String, quality: String, title: String? = nil,
               referer: String? = nil, poster: String? = nil, cookies: String? = nil) {
        let name = Naming.suggested(forURL: url, title: title)
        if store.askBeforeDownload {
            sheet = .rename(PendingDownload(url: url, quality: quality, name: name,
                                            referer: referer, poster: poster, cookies: cookies))
        } else {
            var request = DownloadRequest(url: url, title: name, quality: quality,
                                          referer: referer, destination: destination,
                                          outName: title == nil ? nil : name,
                                          poster: poster)
            request.cookies = cookies
            store.start(request)
            store.flash("Added: \(name)")
        }
    }

    func confirmRename(_ pending: PendingDownload) {
        let name = Naming.sanitize(pending.name)
        guard Naming.isValid(name) else { return }
        var request = DownloadRequest(url: pending.url, title: name, quality: pending.quality,
                                      referer: pending.referer, destination: destination,
                                      outName: name, poster: pending.poster)
        request.cookies = pending.cookies
        store.start(request)
        sheet = nil
    }

    func startBatch(_ batch: BatchProbe) {
        let chosen = batch.chosen
        for item in chosen.reversed() {
            let title = item.title.isEmpty ? Naming.suggested(forURL: item.url) : item.title
            var request = DownloadRequest(url: item.url, title: title,
                                          referer: batch.url, destination: destination,
                                          outName: item.title.isEmpty ? nil : item.title,
                                          poster: item.poster.isEmpty ? nil : item.poster)
            request.cookies = batch.cookies
            store.start(request)
        }
        sheet = nil
        store.flash("Added \(chosen.count) download\(chosen.count == 1 ? "" : "s")")
    }

    func startTorrent(_ probe: TorrentProbe) {
        var request = DownloadRequest(url: probe.url, title: probe.name)
        request.isTorrent = true
        request.destination = destination
        if probe.selected.count < probe.files.count {
            request.torrentSelection = probe.chosen.map { String($0.index) }.joined(separator: ",")
        }
        store.start(request)
        sheet = nil
    }

    /// Stream a torrent's video while it downloads (webtorrent + player).
    func streamTorrent(_ probe: TorrentProbe) {
        sheet = nil
        store.flash("Streaming \(probe.name) — closing the player stops it", seconds: 12)
        Engine.spawn(["stream", probe.url], onLine: { _ in }, onEnd: { code in
            JobStore.shared.flash(code == 0 ? "Stream ended"
                : "Streaming needs webtorrent-cli and VLC or IINA (see Diagnostics)")
        })
    }

    // MARK: folders

    /// A folder picker that also works while a sheet is open.
    func chooseFolder(startingAt: String? = nil) -> String? {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.canCreateDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = "Choose"
        panel.message = "Choose where to save"
        if let start = startingAt ?? destination {
            panel.directoryURL = URL(fileURLWithPath: start)
        }
        return panel.runModal() == .OK ? panel.url?.path : nil
    }

    func pickDestination() {
        if let path = chooseFolder() { destination = path }
    }

    var destinationLabel: String {
        if let d = destination { return tildePath(d) }
        return tildePath(store.downloadRoot) + " (sorted by type)"
    }

    // MARK: drag & drop

    func dropped(_ providers: [NSItemProvider]) -> Bool {
        guard !providers.isEmpty else { return false }
        let group = DispatchGroup()
        let lock = NSLock()
        var texts: [Int: String] = [:]
        var files: [URL] = []
        for (index, provider) in providers.enumerated() {
            if provider.canLoadObject(ofClass: URL.self) {
                group.enter()
                _ = provider.loadObject(ofClass: URL.self) { url, _ in
                    lock.lock()
                    if let url {
                        if url.isFileURL { files.append(url) } else { texts[index] = url.absoluteString }
                    }
                    lock.unlock()
                    group.leave()
                }
            } else if provider.canLoadObject(ofClass: NSString.self) {
                group.enter()
                _ = provider.loadObject(ofClass: NSString.self) { obj, _ in
                    lock.lock()
                    if let text = obj as? String { texts[index] = text }
                    lock.unlock()
                    group.leave()
                }
            }
        }
        group.notify(queue: .main) {
            for file in files {
                let ext = file.pathExtension.lowercased()
                if ext == "torrent" {
                    self.scanTorrent(file.path)
                } else if ["txt", "text", "csv", "m3u", "list", "md", ""].contains(ext) {
                    self.importLinks(from: file)
                } else {
                    self.store.flash("\(file.lastPathComponent) is not a link list — drop links or a .txt file")
                }
            }
            let joined = texts.keys.sorted().compactMap { texts[$0] }.joined(separator: "\n")
            if !joined.isEmpty { self.submit(joined) }
        }
        return true
    }
}
