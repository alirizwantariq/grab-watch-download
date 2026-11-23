import AppKit
import Foundation

/// A link copied in another app, offered for download.
struct ClipboardOffer: Equatable {
    let changeCount: Int
    /// the link itself when MediaGrab may read the clipboard without
    /// asking; nil when only "a web link was copied" is known
    let link: String?
    /// the app that was in front when it was copied
    let sourceApp: String?
}

/// Watches the clipboard for copied links (opt-in, Settings).
///
/// macOS 15.4+ asks the user before an app reads what another app copied.
/// The watcher stays within that: it only checks *whether* a web link was
/// copied (no prompt) and reads the content when the user clicks Download,
/// unless MediaGrab is allowed to paste without asking (System Settings >
/// Privacy & Security > Paste from Other Apps), in which case it reads the
/// link straight away and offers only media, platform and torrent links.
final class ClipboardWatcher {
    let pasteboard: NSPasteboard
    private var lastChange: Int
    private var ownChange = -1
    private var timer: Timer?
    private var offered = Set<String>()
    var onOffer: ((ClipboardOffer) -> Void)?

    init(pasteboard: NSPasteboard = .general) {
        self.pasteboard = pasteboard
        lastChange = pasteboard.changeCount
    }

    var running: Bool { timer != nil }

    func start() {
        guard timer == nil else { return }
        lastChange = pasteboard.changeCount       // only what is copied from now on
        let t = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in self?.poll() }
        RunLoop.main.add(t, forMode: .common)
        timer = t
    }

    func stop() {
        timer?.invalidate()
        timer = nil
    }

    /// Our own "Copy Link" must not come back as an offer.
    func noteOwnCopy() {
        ownChange = pasteboard.changeCount
    }

    /// May the content be read without macOS asking the user?
    var canReadSilently: Bool {
        if pasteboard.name != .general { return true }
        if #available(macOS 15.4, *) {
            return pasteboard.accessBehavior == .alwaysAllow
        }
        return true                                // no clipboard privacy before 15.4
    }

    var accessDenied: Bool {
        if #available(macOS 15.4, *), pasteboard.name == .general {
            return pasteboard.accessBehavior == .alwaysDeny
        }
        return false
    }

    /// The copied text, read now (may make macOS ask the user once).
    func readText() -> String? {
        pasteboard.string(forType: .string)
    }

    private func poll() {
        let count = pasteboard.changeCount
        guard count != lastChange else { return }
        lastChange = count
        guard count != ownChange else { return }
        // copying inside MediaGrab: the user can paste it themselves
        if NSApp.isActive, pasteboard.name == .general { return }
        let source = NSWorkspace.shared.frontmostApplication?.localizedName
        if canReadSilently {
            guard let text = pasteboard.string(forType: .string),
                  let link = Naming.allLinks(in: text, limit: 20).first(where: Self.worthOffering),
                  offered.insert(link).inserted else { return }
            onOffer?(ClipboardOffer(changeCount: count, link: link, sourceApp: source))
            return
        }
        guard !accessDenied else { return }
        if #available(macOS 15.4, *) {
            Task { @MainActor in
                let found = try? await self.pasteboard.detectedPatterns(for: [\.probableWebURL, \.links])
                guard let found, !found.isEmpty, self.pasteboard.changeCount == count else { return }
                self.onOffer?(ClipboardOffer(changeCount: count, link: nil, sourceApp: source))
            }
        }
    }

    /// Media files, video platforms and torrents - not every link anyone
    /// copies (a news article, a search result).
    static func worthOffering(_ link: String) -> Bool {
        if Naming.isTorrent(link) { return true }
        if Session.isPlatform(link) { return true }
        if Naming.looksLikeFile(link) { return true }
        let path = (URLComponents(string: link)?.path ?? "").lowercased()
        return path.contains("/video") || path.contains("/watch") || path.contains("/embed")
    }
}
