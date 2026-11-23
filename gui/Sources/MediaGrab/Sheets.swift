import AppKit
import SwiftUI

// MARK: - shared: where the downloads being added will be saved

struct DestinationLine: View {
    @EnvironmentObject var session: Session
    @EnvironmentObject var store: JobStore

    var body: some View {
        HStack(spacing: 6) {
            Image(systemName: "folder").foregroundColor(.secondary)
            Text(session.destination.map { tildePath($0) }
                 ?? tildePath(store.downloadRoot) + " (sorted by type)")
                .lineLimit(1).truncationMode(.middle)
                .foregroundColor(.secondary)
            Button("Change…") { session.pickDestination() }
                .controlSize(.small)
            if session.destination != nil {
                Button("Use Default") { session.destination = nil }
                    .controlSize(.small)
            }
        }
        .font(.caption)
    }
}

struct SheetHeader: View {
    let symbol: String
    let title: String
    let subtitle: String

    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: symbol)
                .font(.title2).foregroundColor(.accentColor)
            VStack(alignment: .leading, spacing: 2) {
                Text(title).font(.headline)
                if !subtitle.isEmpty {
                    Text(subtitle).font(.caption).foregroundColor(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                }
            }
            Spacer()
        }
    }
}

// MARK: - quality chooser

struct QualitySheet: View {
    let probe: QualityProbe
    var onStart: (String) -> Void
    var onClose: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SheetHeader(symbol: "sparkles.tv", title: "Choose a quality",
                        subtitle: probe.title ?? probe.url)
            if !probe.options.isEmpty {
                ScrollView {
                    VStack(spacing: 5) {
                        ForEach(probe.options) { opt in
                            Button {
                                onStart(opt.quality)
                            } label: {
                                HStack(spacing: 10) {
                                    Image(systemName: opt.quality == "audio" ? "music.note"
                                          : opt.quality == "best" ? "sparkles" : "film")
                                        .foregroundColor(.accentColor)
                                        .frame(width: 22)
                                    Text(opt.label)
                                        .frame(maxWidth: .infinity, alignment: .leading)
                                    Image(systemName: "chevron.right")
                                        .font(.caption).foregroundColor(.secondary)
                                }
                                .padding(.vertical, 8).padding(.horizontal, 12)
                                .background(RoundedRectangle(cornerRadius: 8)
                                    .fill(Color.accentColor.opacity(0.08)))
                                .contentShape(Rectangle())
                            }
                            .buttonStyle(.plain)
                        }
                    }
                    .padding(.vertical, 2)
                }
                .frame(maxHeight: 280)
            } else {
                Label(probe.error ?? "No quality information for this link.",
                      systemImage: "exclamationmark.triangle")
                    .foregroundColor(.orange)
                    .fixedSize(horizontal: false, vertical: true)
                Text("MediaGrab can still try to download it with automatic settings.")
                    .font(.caption).foregroundColor(.secondary)
            }
            HStack {
                Spacer()
                Button("Cancel") { onClose() }
                    .keyboardShortcut(.cancelAction)
                if probe.options.isEmpty {
                    Button("Try Anyway") { onStart("auto") }
                        .buttonStyle(.borderedProminent)
                        .keyboardShortcut(.defaultAction)
                }
            }
        }
        .padding(18)
        .frame(width: 440)
    }
}

// MARK: - name + folder before a download starts

struct RenameSheet: View {
    @ObservedObject var pending: PendingDownload
    @EnvironmentObject var session: Session
    @FocusState private var nameFocused: Bool

    private var valid: Bool { Naming.isValid(pending.name) }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SheetHeader(symbol: "square.and.arrow.down", title: "Save as",
                        subtitle: pending.url)
            TextField("File name", text: $pending.name)
                .textFieldStyle(.roundedBorder)
                .focused($nameFocused)
                .onSubmit { if valid { session.confirmRename(pending) } }
            if pending.name.contains("/") {
                Text("A file name cannot contain “/”.")
                    .font(.caption).foregroundColor(.red)
            } else {
                Text("The file extension is added for you.")
                    .font(.caption).foregroundColor(.secondary)
            }
            DestinationLine()
            HStack {
                Spacer()
                Button("Cancel") { session.sheet = nil }
                    .keyboardShortcut(.cancelAction)
                Button {
                    session.confirmRename(pending)
                } label: {
                    Label("Download", systemImage: "arrow.down.circle.fill")
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!valid)
            }
        }
        .padding(18)
        .frame(width: 500)
        .onAppear { nameFocused = true }
    }
}

// MARK: - multi-video picker

struct PosterThumb: View {
    let poster: String
    let fallback: String

    var body: some View {
        Group {
            if !poster.isEmpty, let url = URL(string: poster) {
                AsyncImage(url: url) { phase in
                    switch phase {
                    case .success(let img):
                        img.resizable().aspectRatio(contentMode: .fill)
                    default:
                        placeholder
                    }
                }
            } else {
                placeholder
            }
        }
        .frame(width: 72, height: 44)
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

    private var placeholder: some View {
        ZStack {
            Rectangle().fill(Color.secondary.opacity(0.15))
            Image(systemName: fallback).foregroundColor(.secondary)
        }
    }
}

struct BatchSheet: View {
    @ObservedObject var batch: BatchProbe
    @EnvironmentObject var session: Session

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                SheetHeader(symbol: "square.stack.3d.down.right",
                            title: "\(batch.items.count) items on this page",
                            subtitle: batch.url)
                Button("All") { batch.selected = Set(batch.items.map(\.id)) }
                    .controlSize(.small)
                Button("None") { batch.selected = [] }
                    .controlSize(.small)
            }
            .padding(14)
            Divider()
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(batch.items) { item in
                        row(item)
                        Divider()
                    }
                }
            }
            Divider()
            VStack(alignment: .leading, spacing: 8) {
                DestinationLine()
                HStack {
                    Text("\(batch.selected.count) of \(batch.items.count) selected"
                         + (batch.totalSelectedBytes > 0 ? " · " + fmtBytes(batch.totalSelectedBytes) : ""))
                        .font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Cancel") { session.sheet = nil }
                        .keyboardShortcut(.cancelAction)
                    Button {
                        session.startBatch(batch)
                    } label: {
                        Label("Download \(batch.selected.count)", systemImage: "arrow.down.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(batch.selected.isEmpty)
                }
            }
            .padding(14)
        }
        .frame(width: 680, height: 540)
    }

    private func row(_ item: BatchItem) -> some View {
        let isOn = batch.selected.contains(item.id)
        return Button {
            if isOn { batch.selected.remove(item.id) } else { batch.selected.insert(item.id) }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: isOn ? "checkmark.circle.fill" : "circle")
                    .foregroundColor(isOn ? .accentColor : .secondary)
                    .font(.title3)
                PosterThumb(poster: item.poster, fallback: "film")
                VStack(alignment: .leading, spacing: 3) {
                    Text(item.title.isEmpty ? Naming.fileName(fromURL: item.url) : item.title)
                        .lineLimit(1).truncationMode(.middle).font(.body.weight(.medium))
                    HStack(spacing: 8) {
                        if !item.quality.isEmpty {
                            Text(item.quality)
                                .font(.caption2.weight(.bold))
                                .padding(.horizontal, 5).padding(.vertical, 1)
                                .background(Capsule().fill(Color.accentColor.opacity(0.22)))
                        }
                        Text(item.sizeText).font(.caption).foregroundColor(.secondary)
                        Text(URLComponents(string: item.url)?.host ?? "")
                            .font(.caption).foregroundColor(.secondary).lineLimit(1)
                    }
                }
                Spacer()
            }
            .padding(.horizontal, 14).padding(.vertical, 7)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - torrent file picker

struct TorrentSheet: View {
    @ObservedObject var probe: TorrentProbe
    @EnvironmentObject var session: Session

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                SheetHeader(symbol: "arrow.down.circle.dotted", title: "Torrent",
                            subtitle: probe.name)
                Button("All") { probe.selected = Set(probe.files.map(\.id)) }
                    .controlSize(.small)
                Button("None") { probe.selected = [] }
                    .controlSize(.small)
            }
            .padding(14)
            Divider()
            ScrollView {
                LazyVStack(spacing: 0) {
                    ForEach(probe.files) { file in
                        row(file)
                        Divider()
                    }
                }
            }
            Divider()
            VStack(alignment: .leading, spacing: 8) {
                DestinationLine()
                HStack(spacing: 8) {
                    Text("\(probe.selected.count) of \(probe.files.count) files · "
                         + fmtBytes(probe.totalSelectedBytes))
                        .font(.caption).foregroundColor(.secondary)
                    Spacer()
                    if probe.hasVideo {
                        Button {
                            session.streamTorrent(probe)
                        } label: {
                            Label("Stream", systemImage: "play.tv")
                        }
                        .help("Play the video while it downloads (needs webtorrent-cli and VLC or IINA)")
                    }
                    Button("Cancel") { session.sheet = nil }
                        .keyboardShortcut(.cancelAction)
                    Button {
                        session.startTorrent(probe)
                    } label: {
                        Label("Download", systemImage: "arrow.down.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(probe.selected.isEmpty)
                }
            }
            .padding(14)
        }
        .frame(width: 680, height: 520)
    }

    private func row(_ file: TorrentFile) -> some View {
        let isOn = probe.selected.contains(file.id)
        return Button {
            if isOn { probe.selected.remove(file.id) } else { probe.selected.insert(file.id) }
        } label: {
            HStack(spacing: 10) {
                Image(systemName: isOn ? "checkmark.circle.fill" : "circle")
                    .foregroundColor(isOn ? .accentColor : .secondary)
                    .font(.title3)
                Image(systemName: file.video ? "film" : fileSymbol(forPath: file.path))
                    .foregroundColor(file.video ? .accentColor : .secondary)
                    .frame(width: 22)
                Text(file.path.isEmpty ? "file \(file.index)" : file.path)
                    .lineLimit(1).truncationMode(.middle)
                Spacer()
                Text(fmtBytes(file.size))
                    .font(.caption).monospacedDigit().foregroundColor(.secondary)
            }
            .padding(.horizontal, 14).padding(.vertical, 8)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
    }
}

// MARK: - downloads sent from the browser

struct HandoffRow: View {
    @ObservedObject var item: HandoffItem

    var body: some View {
        HStack(spacing: 10) {
            Toggle("", isOn: $item.included)
                .labelsHidden()
                .toggleStyle(.checkbox)
                .help("Download this one")
            Image(systemName: item.kind == "stream" ? "dot.radiowaves.left.and.right"
                  : item.kind == "page" ? "globe"
                  : item.kind == "torrent" ? "arrow.down.circle.dotted"
                  : fileSymbol(forPath: Naming.fileName(fromURL: item.url)))
                .foregroundColor(.accentColor).frame(width: 22)
            VStack(alignment: .leading, spacing: 3) {
                TextField(item.kind == "page" ? "Automatic name (from the page)"
                          : item.kind == "torrent" ? "Automatic name (from the torrent)" : "File name",
                          text: $item.name)
                    .textFieldStyle(.roundedBorder)
                    .disabled(!item.included)
                HStack(spacing: 6) {
                    if item.kind == "stream" {
                        Text("STREAM").font(.caption2.weight(.bold))
                            .padding(.horizontal, 4).padding(.vertical, 1)
                            .background(Capsule().fill(Color.purple.opacity(0.25)))
                    }
                    if item.cookies != nil {
                        Image(systemName: "person.badge.key")
                            .font(.caption2).foregroundColor(.secondary)
                            .help("Uses your login for this site (from the browser)")
                            .accessibilityLabel("Uses your site login")
                    }
                    Text(item.url)
                        .font(.caption2).foregroundColor(.secondary)
                        .lineLimit(1).truncationMode(.middle)
                    if item.included && !item.name.isEmpty && !Naming.isValid(item.name) {
                        Text("no “/” in names")
                            .font(.caption2).foregroundColor(.red)
                    }
                }
            }
        }
        .opacity(item.included ? 1 : 0.5)
    }
}

struct HandoffSheet: View {
    @ObservedObject var probe: HandoffProbe
    @EnvironmentObject var session: Session

    var body: some View {
        let ready = probe.chosen.count
        VStack(spacing: 0) {
            SheetHeader(symbol: probe.origin == nil ? "puzzlepiece.extension" : "list.bullet.rectangle",
                        title: probe.title,
                        subtitle: probe.source)
                .padding(14)
            Divider()
            ScrollView {
                LazyVStack(spacing: 10) {
                    ForEach(probe.items) { item in
                        HandoffRow(item: item)
                    }
                }
                .padding(.horizontal, 14).padding(.vertical, 10)
            }
            .frame(height: min(330, CGFloat(probe.items.count) * 58 + 20))
            Divider()
            VStack(alignment: .leading, spacing: 8) {
                DestinationLine()
                HStack {
                    Text(probe.origin == nil ? "The file extension is added for you."
                         : "Leave a name empty to name it after its page.")
                        .font(.caption).foregroundColor(.secondary)
                    Spacer()
                    Button("Cancel") { session.sheet = nil }
                        .keyboardShortcut(.cancelAction)
                    Button {
                        session.startHandoff(probe)
                    } label: {
                        Label(ready == 1 ? "Download" : "Download \(ready)",
                              systemImage: "arrow.down.circle.fill")
                    }
                    .buttonStyle(.borderedProminent)
                    .keyboardShortcut(.defaultAction)
                    .disabled(ready == 0)
                }
            }
            .padding(14)
        }
        .frame(width: 640)
    }
}

// MARK: - diagnostics

/// View-local state lives in small model objects: SwiftUI's @State is a
/// macro whose plugin only ships with full Xcode, and this package must
/// build with the Command Line Tools alone.
final class DiagnosticsModel: ObservableObject {
    @Published var fullReport = ""
    @Published var running = false
}

struct DiagnosticsSheet: View {
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session
    @StateObject private var model = DiagnosticsModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SheetHeader(symbol: "stethoscope", title: "Diagnostics",
                        subtitle: "What MediaGrab needs on this Mac")
            if let report = store.report, report.reachable {
                VStack(spacing: 0) {
                    toolRow(name: "Download engine", ok: true,
                            detail: "mgd · Python \(report.python)", fix: nil)
                    ForEach(report.tools) { tool in
                        Divider()
                        toolRow(name: tool.id, ok: tool.path != nil,
                                detail: tool.path.map { "\(tool.purpose) — \(tildePath($0))" }
                                    ?? "Needed for: \(tool.purpose)",
                                fix: tool.path == nil ? tool.install : nil)
                        if tool.id == "yt-dlp", tool.path != nil {
                            ytdlpVersionRow(report)
                        }
                    }
                }
                .background(RoundedRectangle(cornerRadius: 8)
                    .fill(Color(NSColor.controlBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(Color.secondary.opacity(0.15)))
                if report.tools.contains(where: { $0.path == nil }) {
                    Text("Missing tools install with Homebrew (brew.sh): run the command in Terminal, then press Check Again.")
                        .font(.caption).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            } else {
                Label("The download engine did not answer.", systemImage: "exclamationmark.octagon.fill")
                    .foregroundColor(.red)
                Text("MediaGrab runs its engine with python3 (3.10 or newer). Install it with "
                     + "“brew install python”, then press Check Again.\nEngine: \(tildePath(Engine.launcher))")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .textSelection(.enabled)
            }
            VStack(alignment: .leading, spacing: 3) {
                pathLine("Downloads", store.downloadRoot)
                pathLine("Engine", Engine.launcher)
                pathLine("Settings", Engine.configDirectory.path)
            }
            if !model.fullReport.isEmpty {
                ScrollView {
                    Text(model.fullReport)
                        .font(.system(size: 11, design: .monospaced))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                        .padding(8)
                }
                .frame(height: 150)
                .background(RoundedRectangle(cornerRadius: 6)
                    .fill(Color(NSColor.textBackgroundColor)))
            }
            HStack {
                Button {
                    model.running = true
                    store.loadEngineSettings(checkUpdates: true)
                    let model = model
                    Task {
                        let text = await Engine.run(["doctor"])
                        await MainActor.run {
                            model.fullReport = text.trimmingCharacters(in: .whitespacesAndNewlines)
                            model.running = false
                        }
                    }
                } label: {
                    Label("Check Again", systemImage: "arrow.clockwise")
                }
                .disabled(model.running)
                if model.running { ProgressView().controlSize(.small) }
                Button("Open Browser Log") {
                    let log = Engine.configDirectory.appendingPathComponent("native-host.log")
                    if FileManager.default.fileExists(atPath: log.path) {
                        NSWorkspace.shared.open(log)
                    } else {
                        store.flash("No browser log yet — the extension has not been used")
                    }
                }
                .help("What the browser extension's link to MediaGrab has been doing")
                Spacer()
                Button("Done") { session.sheet = nil }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(18)
        .frame(width: 580)
    }

    private func toolRow(name: String, ok: Bool, detail: String, fix: String?) -> some View {
        HStack(alignment: .center, spacing: 10) {
            Image(systemName: ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundColor(ok ? .green : .orange)
            VStack(alignment: .leading, spacing: 2) {
                Text(name).font(.body.weight(.medium))
                Text(detail).font(.caption).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            if let fix {
                Text(fix)
                    .font(.system(size: 11, design: .monospaced))
                    .textSelection(.enabled)
                Button {
                    copyToClipboard(fix)
                    store.flash("Copied: \(fix)")
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .help("Copy the install command")
                .accessibilityLabel("Copy install command")
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
    }

    @ViewBuilder
    private func ytdlpVersionRow(_ report: EngineReport) -> some View {
        HStack(spacing: 8) {
            Text("Version \(report.ytdlpVersion ?? "?")"
                 + (report.ytdlpAgeDays.map { $0 == 0 ? " (released today)" : " (\($0) days old)" } ?? "")
                 + (report.ytdlpInstall.map { " · via \($0 == "brew" ? "Homebrew" : $0)" } ?? ""))
                .font(.caption).foregroundColor(.secondary)
            Spacer()
            if store.updatingTool {
                ProgressView().controlSize(.small)
            } else if report.ytdlpUpdateAvailable {
                Button("Update to \(report.ytdlpLatest ?? "the newest")") {
                    Task { await store.updateYtdlp() }
                }
                .controlSize(.small)
            } else if report.ytdlpLatest != nil {
                Label("Up to date", systemImage: "checkmark").font(.caption).foregroundColor(.green)
            }
        }
        .padding(.leading, 42).padding(.trailing, 12).padding(.bottom, 8)
    }

    private func pathLine(_ label: String, _ path: String) -> some View {
        HStack(spacing: 6) {
            Text(label).font(.caption.weight(.semibold)).foregroundColor(.secondary)
                .frame(width: 70, alignment: .trailing)
            Text(tildePath(path)).font(.caption).lineLimit(1).truncationMode(.middle)
                .textSelection(.enabled)
        }
    }
}

// MARK: - browser extension setup

final class ExtensionModel: ObservableObject {
    @Published var states: [(browser: BrowserLink.Browser, state: BrowserLink.State)] = []
    @Published var message = ""
    @Published var messageIsError = false

    func reload() {
        states = BrowserLink.browsers.filter(\.installed)
            .map { ($0, BrowserLink.state(of: $0)) }
    }

    func say(_ text: String, error: Bool = false) {
        message = text
        messageIsError = error
    }
}

struct ExtensionSheet: View {
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session
    @StateObject private var model = ExtensionModel()

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            SheetHeader(symbol: "puzzlepiece.extension", title: "Browser Extension",
                        subtitle: "Pick videos on any page and send them to MediaGrab")
            step(1, "Add the MediaGrab extension to your browser",
                 "Open the extensions page (chrome://extensions), turn on Developer mode, "
                 + "click “Load unpacked” and choose the folder below.") {
                HStack(spacing: 8) {
                    Button("Show Extension Folder") { showExtension() }
                    Button("Copy “chrome://extensions”") {
                        copyToClipboard("chrome://extensions")
                        model.say("Copied — paste it into the browser's address bar")
                    }
                }
            }
            step(2, "Connect your browsers to MediaGrab",
                 "Lets the extension talk to this app. Quit the browser completely (⌘Q) "
                 + "and reopen it afterwards.") {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(model.states, id: \.browser.id) { entry in
                        browserLine(entry.browser, entry.state)
                    }
                    if model.states.isEmpty {
                        Text("No Chromium-based browser found (Chrome, Brave, Edge, Arc, Vivaldi).")
                            .font(.caption).foregroundColor(.secondary)
                    }
                    Button {
                        connect()
                    } label: {
                        Label(model.states.contains { $0.state == .linked } ? "Reconnect Browsers"
                              : "Connect Browsers", systemImage: "link")
                    }
                    .disabled(model.states.isEmpty)
                }
            }
            step(3, "Use it",
                 "Click the MediaGrab icon on a page with videos, tick what you want and press "
                 + "Download — or right-click any link or video and choose “Download with MediaGrab”.") {
                EmptyView()
            }
            if !model.message.isEmpty {
                Label(model.message, systemImage: model.messageIsError ? "exclamationmark.triangle.fill"
                      : "checkmark.circle.fill")
                    .font(.callout)
                    .foregroundColor(model.messageIsError ? .orange : .green)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Spacer()
                Button("Done") { session.sheet = nil }
                    .keyboardShortcut(.defaultAction)
            }
        }
        .padding(18)
        .frame(width: 560)
        .onAppear { model.reload() }
    }

    private func step<Content: View>(_ number: Int, _ title: String, _ text: String,
                                     @ViewBuilder content: () -> Content) -> some View {
        HStack(alignment: .top, spacing: 10) {
            Text("\(number)")
                .font(.callout.weight(.bold))
                .foregroundColor(.white)
                .frame(width: 22, height: 22)
                .background(Circle().fill(Color.accentColor))
            VStack(alignment: .leading, spacing: 6) {
                Text(title).font(.body.weight(.semibold))
                Text(text).font(.callout).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                content()
            }
        }
    }

    private func browserLine(_ browser: BrowserLink.Browser, _ state: BrowserLink.State) -> some View {
        HStack(spacing: 6) {
            switch state {
            case .linked:
                Image(systemName: "checkmark.circle.fill").foregroundColor(.green)
                Text("\(browser.id) — connected")
            case .stale(let why):
                Image(systemName: "exclamationmark.triangle.fill").foregroundColor(.orange)
                Text("\(browser.id) — needs reconnecting (\(why))")
            case .notLinked:
                Image(systemName: "circle").foregroundColor(.secondary)
                Text("\(browser.id) — not connected")
            }
        }
        .font(.callout)
    }

    private func connect() {
        do {
            let linked = try BrowserLink.connect()
            model.reload()
            model.say(linked.isEmpty ? "No browser to connect."
                : "Connected \(linked.joined(separator: ", ")). Quit and reopen the browser to finish.")
        } catch {
            model.say("Could not connect: \(error.localizedDescription)", error: true)
        }
    }

    private func showExtension() {
        do {
            let folder = try BrowserLink.exportExtension()
            NSWorkspace.shared.activateFileViewerSelecting([folder])
            model.say("Choose this “chrome-extension” folder in “Load unpacked”.")
        } catch {
            model.say(error.localizedDescription, error: true)
        }
    }
}

// MARK: - update an expired download link

final class UpdateLinkModel: ObservableObject {
    @Published var link: String

    init() {
        let clip = NSPasteboard.general.string(forType: .string) ?? ""
        link = Naming.firstLink(in: clip) ?? ""
    }
}

struct UpdateLinkSheet: View {
    @ObservedObject var job: DownloadJob
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session
    @StateObject private var model = UpdateLinkModel()

    private var link: String { model.link.trimmingCharacters(in: .whitespacesAndNewlines) }
    private var valid: Bool {
        guard let comps = URLComponents(string: link), let scheme = comps.scheme?.lowercased()
        else { return false }
        return (scheme == "http" || scheme == "https") && comps.host?.isEmpty == false
    }

    private var progressText: String {
        guard job.total > 0 else { return "the part already downloaded" }
        return "\(fmtBytes(job.done)) of \(fmtBytes(job.total)) (\(Int(max(0, job.percent)))%)"
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            SheetHeader(symbol: "link.badge.plus", title: "Update the download link",
                        subtitle: job.title)
            Text("Video sites hand out links that stop working after a while. Open the page "
                 + "in your browser, copy the video's link — or just the page's address — and "
                 + "paste it here. MediaGrab checks it is the same file and continues from "
                 + "\(progressText).")
                .font(.callout).foregroundColor(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            HStack(spacing: 6) {
                TextField("New link or page address", text: $model.link)
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(start)
                Button {
                    if let text = NSPasteboard.general.string(forType: .string),
                       let found = Naming.firstLink(in: text) {
                        model.link = found
                    }
                } label: {
                    Image(systemName: "doc.on.clipboard")
                }
                .help("Paste the link from the clipboard")
                .accessibilityLabel("Paste link")
            }
            if let message = job.errorMessage, job.linkExpired {
                Label(message, systemImage: "info.circle")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Text("A link to a different file is refused — nothing already downloaded is lost.")
                    .font(.caption).foregroundColor(.secondary)
                Spacer()
                Button("Cancel") { session.sheet = nil }
                    .keyboardShortcut(.cancelAction)
                Button {
                    start()
                } label: {
                    Label("Continue", systemImage: "arrow.down.circle.fill")
                }
                .buttonStyle(.borderedProminent)
                .keyboardShortcut(.defaultAction)
                .disabled(!valid)
            }
        }
        .padding(18)
        .frame(width: 540)
    }

    private func start() {
        guard valid else { return }
        store.updateLink(job, to: link)
        session.sheet = nil
    }
}
