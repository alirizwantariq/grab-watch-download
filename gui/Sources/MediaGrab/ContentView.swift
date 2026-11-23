import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct ContentView: View {
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session
    @FocusState private var linkFieldFocused: Bool

    var body: some View {
        HStack(spacing: 0) {
            sidebar
            Divider()
            VStack(spacing: 0) {
                addBar
                if let offer = session.clipboardOffer {
                    clipboardBanner(offer)
                }
                if let warning = engineWarning {
                    warningBanner(warning)
                } else if let report = store.report, report.ytdlpUpdateAvailable,
                          !store.ytdlpBannerDismissed {
                    ytdlpBanner(report)
                }
                Divider()
                if store.selected.isEmpty {
                    listToolbar
                } else {
                    selectionBar
                }
                Divider()
                jobsArea
                Divider()
                statusBar
            }
        }
        .onDrop(of: [UTType.url, UTType.fileURL, UTType.text],
                isTargeted: $session.dropActive) { session.dropped($0) }
        .overlay {
            if session.dropActive { dropOverlay }
        }
        .sheet(item: $session.sheet) { sheet in
            sheetView(sheet)
        }
        .alert(item: $session.confirmation) { c in
            Alert(title: Text(c.title), message: Text(c.message),
                  primaryButton: .destructive(Text(c.actionLabel), action: c.action),
                  secondaryButton: .cancel())
        }
        .toolbar { toolbarContent }
        .onAppear { linkFieldFocused = true }
    }

    // MARK: sheets

    @ViewBuilder
    private func sheetView(_ sheet: ActiveSheet) -> some View {
        switch sheet {
        case .quality(let probe):
            QualitySheet(probe: probe,
                         onStart: { quality in
                             session.begin(url: probe.url, quality: quality, title: probe.title,
                                           cookies: probe.cookies)
                         },
                         onClose: { session.sheet = nil })
        case .rename(let pending):
            RenameSheet(pending: pending)
        case .batch(let batch):
            BatchSheet(batch: batch)
        case .torrent(let probe):
            TorrentSheet(probe: probe)
        case .handoff(let probe):
            HandoffSheet(probe: probe)
        case .diagnostics:
            DiagnosticsSheet()
        case .updateLink(let job):
            UpdateLinkSheet(job: job)
        case .extensionSetup:
            ExtensionSheet()
        }
    }

    // MARK: toolbar

    @ToolbarContentBuilder
    private var toolbarContent: some ToolbarContent {
        ToolbarItemGroup(placement: .primaryAction) {
            Button {
                store.resumeAll()
            } label: {
                Label("Resume All", systemImage: "play.fill")
            }
            .disabled(!store.jobs.contains { $0.phase == .paused })
            .help("Resume every paused download")

            Button {
                store.pauseAll()
            } label: {
                Label("Pause All", systemImage: "pause.fill")
            }
            .disabled(!store.hasWorkInFlight)
            .help("Pause every running and queued download (progress is saved)")

            Menu {
                ForEach(JobStore.speedChoices, id: \.bytes) { choice in
                    Button {
                        store.setSpeedLimit(choice)
                    } label: {
                        if store.speedLimit == choice.bytes {
                            Label(choice.label, systemImage: "checkmark")
                        } else {
                            Text(choice.label)
                        }
                    }
                }
            } label: {
                Label(store.speedLimit > 0 ? store.speedLimitLabel : "Speed Limit",
                      systemImage: store.speedLimit > 0 ? "tortoise.fill" : "speedometer")
            }
            .help("Speed limit shared by all downloads — now: \(store.speedLimitLabel)")

            Menu {
                Button("Open Downloads Folder") {
                    NSWorkspace.shared.open(URL(fileURLWithPath: store.downloadRoot))
                }
                Divider()
                Toggle("Watch Clipboard for Links", isOn: $store.watchClipboard)
                Divider()
                Button("Browser Extension…") { session.sheet = .extensionSetup }
                Button("Diagnostics…") { session.sheet = .diagnostics }
                Button("Make MediaGrab the App for Magnet Links") { makeDefaultTorrentApp() }
                Divider()
                settingsMenuItem
            } label: {
                Label("More", systemImage: "ellipsis.circle")
            }
            .help("Folders, browser extension, diagnostics, settings")
        }
    }

    /// Opens the Settings window (also in the app menu, ⌘,). Since macOS 14
    /// only SettingsLink may do that; older systems still take the action.
    @ViewBuilder
    private var settingsMenuItem: some View {
        if #available(macOS 14.0, *) {
            SettingsLink { Text("Settings…") }
        } else {
            Button("Settings…") {
                NSApp.sendAction(Selector(("showPreferencesWindow:")), to: nil, from: nil)
            }
        }
    }

    /// Claim the magnet: scheme so browser magnet links open MediaGrab.
    private func makeDefaultTorrentApp() {
        guard let bundleId = Bundle.main.bundleIdentifier else {
            store.flash("Only the installed app can become the magnet link handler")
            return
        }
        let status = LSSetDefaultHandlerForURLScheme("magnet" as CFString, bundleId as CFString)
        store.flash(status == noErr
                    ? "Magnet links now open in MediaGrab"
                    : "Could not change the magnet link handler (error \(status))")
    }

    // MARK: sidebar

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 2) {
            Text("LIBRARY")
                .font(.caption2.weight(.semibold))
                .foregroundColor(.secondary)
                .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 4)
            ForEach(MediaGrabCategory.names, id: \.self) { name in
                let count = categoryCount(name)
                if name == "All" || count > 0 || session.category == name {
                    sidebarRow(name, count: count)
                }
            }
            Spacer()
        }
        .frame(width: 176)
        .background(Color(NSColor.windowBackgroundColor))
    }

    private func sidebarRow(_ name: String, count: Int) -> some View {
        let active = session.category == name
        return Button {
            session.category = name
        } label: {
            HStack(spacing: 8) {
                Image(systemName: MediaGrabCategory.symbol(for: name))
                    .frame(width: 18)
                    .foregroundColor(active ? .accentColor : .secondary)
                Text(name)
                Spacer()
                if count > 0 {
                    Text("\(count)")
                        .font(.caption.monospacedDigit())
                        .foregroundColor(.secondary)
                }
            }
            .padding(.vertical, 5).padding(.horizontal, 8)
            .background(RoundedRectangle(cornerRadius: 6)
                .fill(active ? Color.accentColor.opacity(0.16) : Color.clear))
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .padding(.horizontal, 6)
        .accessibilityLabel("\(name), \(count) downloads")
    }

    private func categoryCount(_ name: String) -> Int {
        name == "All" ? store.jobs.count : store.jobs.filter { $0.category == name }.count
    }

    // MARK: add-a-link bar

    private var addBar: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 8) {
                HStack(spacing: 6) {
                    Image(systemName: "link").foregroundColor(.secondary)
                    TextField("Paste a link — video page, file, YouTube, X, magnet…",
                              text: $session.urlString)
                        .textFieldStyle(.plain)
                        .focused($linkFieldFocused)
                        .onSubmit { session.grab() }
                    if session.probing {
                        ProgressView().controlSize(.small)
                    } else if !session.urlString.isEmpty {
                        Button {
                            session.urlString = ""
                        } label: {
                            Image(systemName: "xmark.circle.fill").foregroundColor(.secondary)
                        }
                        .buttonStyle(.plain)
                        .help("Clear")
                    }
                }
                .padding(.horizontal, 10).padding(.vertical, 7)
                .background(RoundedRectangle(cornerRadius: 8)
                    .fill(Color(NSColor.textBackgroundColor)))
                .overlay(RoundedRectangle(cornerRadius: 8)
                    .strokeBorder(linkFieldFocused ? Color.accentColor.opacity(0.7)
                                  : Color.secondary.opacity(0.25), lineWidth: 1))

                Button {
                    session.pasteAndGrab()
                } label: {
                    Image(systemName: "doc.on.clipboard")
                }
                .controlSize(.large)
                .help("Download the link on the clipboard (⇧⌘V)")
                .disabled(session.probing)

                Button {
                    session.grab()
                } label: {
                    Label("Download", systemImage: "arrow.down.circle.fill")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                .disabled(session.urlString.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                          || session.probing)
            }
            HStack(spacing: 6) {
                if session.probing {
                    Text(session.probingLabel)
                        .font(.caption).foregroundColor(.secondary)
                } else {
                    Image(systemName: "folder").font(.caption).foregroundColor(.secondary)
                    Text("Save to")
                        .font(.caption).foregroundColor(.secondary)
                    Menu {
                        Button {
                            session.destination = nil
                        } label: {
                            if session.destination == nil {
                                Label("Downloads folder, sorted by type", systemImage: "checkmark")
                            } else {
                                Text("Downloads folder, sorted by type")
                            }
                        }
                        Button("Choose a Folder…") { session.pickDestination() }
                    } label: {
                        Text(session.destinationLabel)
                            .font(.caption)
                            .lineLimit(1).truncationMode(.middle)
                    }
                    .menuStyle(.borderlessButton)
                    .frame(maxWidth: 360, alignment: .leading)
                    .help("Where the next downloads are saved: \(session.destinationLabel)")
                }
                Spacer()
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 10)
    }

    // MARK: engine warnings

    private var engineWarning: (text: String, symbol: String)? {
        if !store.engineReachable {
            return ("The download engine is not responding — downloads cannot start.",
                    "exclamationmark.octagon.fill")
        }
        if let missing = store.report?.missingEssentials, !missing.isEmpty {
            let names = missing.map(\.id).joined(separator: " and ")
            return ("\(names) \(missing.count == 1 ? "is" : "are") not installed — "
                    + "YouTube, X and streaming sites will not download.",
                    "exclamationmark.triangle.fill")
        }
        return nil
    }

    private func clipboardBanner(_ offer: ClipboardOffer) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "doc.on.clipboard").foregroundColor(.accentColor)
            VStack(alignment: .leading, spacing: 1) {
                Text(offer.sourceApp.map { "Link copied in \($0)" } ?? "Link copied")
                    .font(.callout.weight(.medium))
                Text(offer.link ?? "Download it with MediaGrab?")
                    .font(.caption).foregroundColor(.secondary)
                    .lineLimit(1).truncationMode(.middle)
            }
            Spacer()
            Button("Download") { session.acceptClipboardOffer() }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
            Button {
                session.clipboardOffer = nil
            } label: {
                Image(systemName: "xmark")
            }
            .buttonStyle(.borderless)
            .help("Dismiss")
            .accessibilityLabel("Dismiss")
        }
        .padding(.horizontal, 14).padding(.vertical, 7)
        .background(Color.accentColor.opacity(0.10))
    }

    private func ytdlpBanner(_ report: EngineReport) -> some View {
        HStack(spacing: 8) {
            Image(systemName: "arrow.down.app").foregroundColor(.accentColor)
            VStack(alignment: .leading, spacing: 1) {
                Text("A newer yt-dlp is available"
                     + (report.ytdlpLatest.map { " (\($0))" } ?? ""))
                    .font(.callout.weight(.medium))
                Text("You have \(report.ytdlpVersion ?? "an older one"). Video sites change often — "
                     + "updating keeps YouTube & co. downloading.")
                    .font(.caption).foregroundColor(.secondary).lineLimit(1)
            }
            Spacer()
            if store.updatingTool {
                ProgressView().controlSize(.small)
                Text("Updating…").font(.caption).foregroundColor(.secondary)
            } else {
                Button("Update") { Task { await store.updateYtdlp() } }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .help(report.ytdlpInstall == "brew" ? "Runs: brew upgrade yt-dlp"
                          : report.ytdlpInstall == "pip" ? "Runs: pip install --upgrade yt-dlp"
                          : "Runs: yt-dlp -U")
                Button {
                    store.ytdlpBannerDismissed = true
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.borderless)
                .help("Not now")
                .accessibilityLabel("Dismiss")
            }
        }
        .padding(.horizontal, 14).padding(.vertical, 7)
        .background(Color.accentColor.opacity(0.10))
    }

    private func warningBanner(_ warning: (text: String, symbol: String)) -> some View {
        HStack(spacing: 8) {
            Image(systemName: warning.symbol).foregroundColor(.orange)
            Text(warning.text).font(.callout)
            Spacer()
            Button("Show Fix") { session.sheet = .diagnostics }
                .controlSize(.small)
        }
        .padding(.horizontal, 14).padding(.vertical, 7)
        .background(Color.orange.opacity(0.12))
    }

    // MARK: list toolbar / selection bar

    private var listToolbar: some View {
        HStack(spacing: 8) {
            HStack(spacing: 5) {
                Image(systemName: "magnifyingglass").foregroundColor(.secondary)
                TextField("Filter by name or link", text: $session.searchText)
                    .textFieldStyle(.plain)
                if !session.searchText.isEmpty {
                    Button {
                        session.searchText = ""
                    } label: {
                        Image(systemName: "xmark.circle.fill").foregroundColor(.secondary)
                    }
                    .buttonStyle(.plain)
                }
            }
            .font(.callout)
            .padding(.horizontal, 8).padding(.vertical, 4)
            .background(RoundedRectangle(cornerRadius: 6)
                .fill(Color(NSColor.textBackgroundColor)))
            .overlay(RoundedRectangle(cornerRadius: 6)
                .strokeBorder(Color.secondary.opacity(0.2), lineWidth: 1))
            .frame(maxWidth: 280)
            Spacer()
            Button("Select All") { store.select(visibleJobs) }
                .controlSize(.small)
                .disabled(visibleJobs.isEmpty)
            Button("Clear Finished") { store.clearFinished() }
                .controlSize(.small)
                .disabled(store.finishedJobs.isEmpty)
                .help("Remove finished downloads from the list (files stay on disk)")
        }
        .padding(.horizontal, 14).padding(.vertical, 7)
    }

    private var selectionBar: some View {
        let picked = store.selectedJobs
        let active = picked.filter { $0.phase.isActive }.count
        let files = picked.filter { $0.phase == .done && store.fileExists($0) }.count
        return HStack(spacing: 8) {
            Image(systemName: "checkmark.circle.fill").foregroundColor(.accentColor)
            Text("\(picked.count) selected").font(.callout.weight(.medium))
            Button("Select All") { store.select(visibleJobs) }
                .controlSize(.small)
                .disabled(visibleJobs.allSatisfy { store.selected.contains($0.id) })
            Button("Deselect") { store.clearSelection() }
                .controlSize(.small)
                .keyboardShortcut(.cancelAction)
            Spacer()
            if picked.contains(where: { store.isVideoFile($0) && $0.phase == .done }) {
                Button {
                    store.makeMacReady(picked)
                    store.clearSelection()
                } label: {
                    Label("Make Mac-Ready", systemImage: "wand.and.stars")
                }
                .controlSize(.small)
                .help("Convert the selected videos so they play smoothly on this Mac (only those that need it)")
            }
            Button {
                confirmRemoveSelected(active: active)
            } label: {
                Label("Remove from List", systemImage: "minus.circle")
            }
            .controlSize(.small)
            .help("Forget the selected rows. Finished files stay on disk; unfinished downloads are stopped.")
            Button(role: .destructive) {
                confirmDeleteSelected(active: active, files: files)
            } label: {
                Label("Delete Files", systemImage: "trash")
            }
            .controlSize(.small)
            .help("Remove the selected rows and move their files to the Trash")
        }
        .padding(.horizontal, 14).padding(.vertical, 7)
        .background(Color.accentColor.opacity(0.10))
    }

    private func confirmRemoveSelected(active: Int) {
        guard active > 0 else {
            store.removeSelected()
            return
        }
        session.confirmation = Confirmation(
            title: "Stop \(active) unfinished download\(active == 1 ? "" : "s")?",
            message: "Removing an unfinished download deletes the part already downloaded. "
                + "Finished files are kept.",
            actionLabel: "Stop and Remove",
            action: { store.removeSelected() })
    }

    private func confirmDeleteSelected(active: Int, files: Int) {
        var parts: [String] = []
        if files > 0 { parts.append("\(files) file\(files == 1 ? "" : "s") will be moved to the Trash") }
        if active > 0 {
            parts.append("\(active) unfinished download\(active == 1 ? "" : "s") will be stopped and "
                         + "the downloaded part deleted")
        }
        guard !parts.isEmpty else {
            store.deleteSelected()
            return
        }
        session.confirmation = Confirmation(
            title: "Delete \(store.selected.count) selected item\(store.selected.count == 1 ? "" : "s")?",
            message: parts.joined(separator: "; ") + ".",
            actionLabel: "Delete",
            action: { store.deleteSelected() })
    }

    // MARK: the list

    private func matches(_ job: DownloadJob) -> Bool {
        if session.category != "All" && job.category != session.category { return false }
        let q = session.searchText.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        guard !q.isEmpty else { return true }
        return job.title.lowercased().contains(q)
            || job.url.lowercased().contains(q)
            || (job.finishedPath ?? "").lowercased().contains(q)
    }

    private var visibleJobs: [DownloadJob] { store.jobs.filter(matches) }

    /// Section headers and downloads as one flat, uniquely-keyed list (a job
    /// keeps its row identity when it moves from one section to the next).
    private enum ListRow: Identifiable {
        case header(String, Int)
        case job(DownloadJob)

        var id: String {
            switch self {
            case .header(let title, _): return "header-" + title
            case .job(let job): return job.id.uuidString
            }
        }
    }

    private func listRows(_ visible: [DownloadJob]) -> [ListRow] {
        var rows: [ListRow] = []
        // a download waiting to retry by itself is still in progress
        let waiting = { (j: DownloadJob) in
            j.phase == .failed && j.connectionProblem
                && (j.retryAt != nil || !store.networkAvailable)
        }
        let sections: [(String, [DownloadJob])] = [
            ("In Progress", visible.filter { $0.phase.isActive || waiting($0) }),
            ("Failed", visible.filter { $0.phase == .failed && !waiting($0) }),
            ("Completed", visible.filter { $0.phase == .done }),
        ]
        for (title, jobs) in sections where !jobs.isEmpty {
            rows.append(.header(title, jobs.count))
            rows.append(contentsOf: jobs.map { .job($0) })
        }
        return rows
    }

    private var jobsArea: some View {
        let visible = visibleJobs
        return Group {
            if visible.isEmpty {
                emptyState
            } else {
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 6) {
                        ForEach(listRows(visible)) { row in
                            switch row {
                            case .header(let title, let count):
                                SectionHeader(title: title, count: count)
                            case .job(let job):
                                JobRow(job: job, isSelected: store.selected.contains(job.id))
                            }
                        }
                    }
                    .padding(.horizontal, 14)
                    .padding(.vertical, 8)
                }
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(NSColor.underPageBackgroundColor).opacity(0.35))
    }

    private var emptyState: some View {
        let filtered = !store.jobs.isEmpty
        return VStack(spacing: 10) {
            Image(systemName: filtered ? "line.3.horizontal.decrease.circle" : "arrow.down.circle")
                .font(.system(size: 44, weight: .light))
                .foregroundColor(.secondary.opacity(0.6))
            Text(filtered ? "Nothing matches" : "No downloads yet")
                .font(.title3.weight(.medium))
            Text(filtered
                 ? "No download in “\(session.category)” matches the filter."
                 : "Paste a link above, drag one onto this window, or send videos from "
                   + "your browser with the MediaGrab extension.")
                .font(.callout).foregroundColor(.secondary)
                .multilineTextAlignment(.center)
                .frame(maxWidth: 380)
            if filtered {
                Button("Show All Downloads") {
                    session.category = "All"
                    session.searchText = ""
                }
            } else {
                HStack(spacing: 10) {
                    Button("Paste Link from Clipboard") { session.pasteAndGrab() }
                    Button("Set Up Browser Extension…") { session.sheet = .extensionSetup }
                }
                .padding(.top, 4)
            }
        }
        .padding(30)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    // MARK: status bar

    private var statusBar: some View {
        HStack(spacing: 10) {
            if !store.status.isEmpty {
                Text(store.status)
                    .lineLimit(1).truncationMode(.middle)
            } else {
                Text(summaryText)
            }
            Spacer()
            if store.speedLimit > 0 {
                Label("limit \(store.speedLimitLabel)", systemImage: "tortoise")
                    .labelStyle(.titleAndIcon)
            }
            if store.totalSpeed > 1 {
                Label(fmtSpeed(store.totalSpeed), systemImage: "arrow.down")
                    .labelStyle(.titleAndIcon)
                    .monospacedDigit()
            }
        }
        .font(.caption)
        .foregroundColor(.secondary)
        .padding(.horizontal, 14).padding(.vertical, 6)
    }

    private var summaryText: String {
        let running = store.runningCount
        let queued = store.jobs.filter { $0.phase == .queued }.count
        let paused = store.jobs.filter { $0.phase == .paused }.count
        var parts: [String] = []
        if running > 0 { parts.append("\(running) downloading") }
        if queued > 0 { parts.append("\(queued) queued") }
        if paused > 0 { parts.append("\(paused) paused") }
        if parts.isEmpty {
            let done = store.jobs.filter { $0.phase == .done }.count
            return done > 0 ? "\(done) completed" : "Ready"
        }
        return parts.joined(separator: " · ")
    }

    private var dropOverlay: some View {
        RoundedRectangle(cornerRadius: 12)
            .strokeBorder(Color.accentColor, style: StrokeStyle(lineWidth: 3, dash: [8, 6]))
            .background(RoundedRectangle(cornerRadius: 12)
                .fill(Color.accentColor.opacity(0.10)))
            .overlay {
                Label("Drop the link to download it", systemImage: "arrow.down.circle.fill")
                    .font(.title2.bold())
                    .foregroundColor(.accentColor)
            }
            .padding(8)
            .allowsHitTesting(false)
    }
}
