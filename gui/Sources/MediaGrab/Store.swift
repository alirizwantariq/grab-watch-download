import AppKit
import Combine
import Foundation
import Network
import UserNotifications

/// What the UI asks the store to download.
struct DownloadRequest {
    var url: String
    var title: String
    var quality: String = "auto"          // auto | best | 1080 | … | audio
    var referer: String?
    var destination: String?              // nil = the engine's category folders
    var outName: String?
    var poster: String?
    var isTorrent = false
    var torrentSelection: String?         // "1,3,5" (aria2 numbering)
    /// private cookies.txt with the site login (from the browser extension)
    var cookies: String?

    var arguments: [String] {
        var args = ["grab", url, "--json-progress", "--retries", "8"]
        if quality == "audio" {
            args += ["--quality", "audio"]
        } else if quality != "auto" && quality != "best" && !quality.isEmpty {
            args += ["--quality", quality]
        }
        if let r = referer, !r.isEmpty, r != url { args += ["--referer", r] }
        if let d = destination, !d.isEmpty { args += ["-d", d] }
        if let n = outName, Naming.isValid(n) { args += ["--out-name", Naming.sanitize(n)] }
        if let sel = torrentSelection, !sel.isEmpty { args += ["--select-files", sel] }
        if let c = cookies, FileManager.default.fileExists(atPath: c) { args += ["--cookies", c] }
        return args
    }
}

/// Where preferences are stored (a throwaway domain during UI test runs).
enum Prefs {
    static let defaults: UserDefaults = {
        #if DEBUG
        if UITestHooks.active, let scratch = UserDefaults(suiteName: "mediagrab.uitest") {
            scratch.removePersistentDomain(forName: "mediagrab.uitest")
            return scratch
        }
        #endif
        return .standard
    }()
}

/// The one list of downloads, shared by every view (and by the app
/// delegate for the quit/close decisions). Main-thread only.
final class JobStore: ObservableObject {
    static let shared = JobStore()

    @Published var jobs: [DownloadJob] = [] {
        didSet { watchPhases() }
    }
    @Published var selected: Set<UUID> = []
    /// transient message for the status bar (clears itself)
    @Published var status = ""
    /// engine settings mirrored from `mgd defaults --json`
    @Published var downloadRoot = (NSHomeDirectory() as NSString)
        .appendingPathComponent("Downloads/mgd")
    @Published var speedLimit: Int = 0            // bytes/s, 0 = unlimited
    @Published var engineReachable = true
    @Published var report: EngineReport?

    // user preferences (UserDefaults)
    @Published var maxConcurrent: Int {
        didSet { Prefs.defaults.set(maxConcurrent, forKey: "maxConcurrent"); pump() }
    }
    @Published var askBeforeDownload: Bool {
        didSet { Prefs.defaults.set(askBeforeDownload, forKey: "askBeforeDownload") }
    }
    @Published var notifyOnFinish: Bool {
        didSet { Prefs.defaults.set(notifyOnFinish, forKey: "notifyOnFinish") }
    }
    @Published var keepAwake: Bool {
        didSet { Prefs.defaults.set(keepAwake, forKey: "keepAwake"); updateDockBadge() }
    }
    @Published var autoRetry: Bool {
        didSet {
            Prefs.defaults.set(autoRetry, forKey: "autoRetry")
            if !autoRetry { for job in jobs { job.retryAt = nil } }
        }
    }
    @Published var watchClipboard: Bool {
        didSet {
            Prefs.defaults.set(watchClipboard, forKey: "watchClipboard")
            Session.shared.updateClipboardWatch()
        }
    }
    @Published var autoDownloadClipboard: Bool {
        didSet { Prefs.defaults.set(autoDownloadClipboard, forKey: "autoDownloadClipboard") }
    }
    /// false while macOS reports no usable network connection
    @Published var networkAvailable = true
    /// the "keep the Mac awake" assertion, while held
    private(set) var awakeActivity: NSObjectProtocol?
    private let pathMonitor = NWPathMonitor()
    /// automatic retry delays after a connection problem (seconds)
    static let retryDelays: [Double] = [15, 30, 60, 120, 300]

    private var phaseWatchers = Set<AnyCancellable>()
    private var pollTimer: Timer?
    private var refreshing = false
    private var statusToken = 0
    private var saveScheduled = false
    private var terminating = false

    private init() {
        let d = Prefs.defaults
        d.register(defaults: ["maxConcurrent": 3, "askBeforeDownload": true,
                              "notifyOnFinish": true, "keepAwake": true, "autoRetry": true,
                              "watchClipboard": false, "autoDownloadClipboard": false])
        maxConcurrent = d.integer(forKey: "maxConcurrent")
        askBeforeDownload = d.bool(forKey: "askBeforeDownload")
        notifyOnFinish = d.bool(forKey: "notifyOnFinish")
        keepAwake = d.bool(forKey: "keepAwake")
        autoRetry = d.bool(forKey: "autoRetry")
        watchClipboard = d.bool(forKey: "watchClipboard")
        autoDownloadClipboard = d.bool(forKey: "autoDownloadClipboard")
        loadHistory()
    }

    /// A job moving between running / paused / done changes which section it
    /// belongs to, the toolbar and the counters - all computed from this
    /// store, so the store has to announce it.
    private func watchPhases() {
        phaseWatchers.removeAll()
        for job in jobs {
            job.$phase.dropFirst().removeDuplicates()
                .sink { [weak self] _ in
                    DispatchQueue.main.async {
                        self?.objectWillChange.send()
                        self?.updateDockBadge()
                    }
                }
                .store(in: &phaseWatchers)
        }
    }

    /// Called once the app is up: start following the engine.
    func activate() {
        guard pollTimer == nil else { return }
        refresh()
        loadEngineSettings(checkUpdates: true)
        let timer = Timer(timeInterval: 2.5, repeats: true) { [weak self] _ in
            self?.refresh()
            self?.retryDueDownloads()
        }
        RunLoop.main.add(timer, forMode: .common)
        pollTimer = timer
        pathMonitor.pathUpdateHandler = { [weak self] path in
            let up = path.status == .satisfied
            DispatchQueue.main.async { self?.networkChanged(up) }
        }
        pathMonitor.start(queue: DispatchQueue(label: "mediagrab.network"))
        NSWorkspace.shared.notificationCenter.addObserver(
            forName: NSWorkspace.didWakeNotification, object: nil, queue: .main) { [weak self] _ in
            // connections rarely survive sleep: give the network a moment,
            // then pick up what was waiting
            self?.retrySoon(after: 5)
        }
    }

    // MARK: connection problems: retry by themselves

    func networkChanged(_ up: Bool) {
        let cameBack = up && !networkAvailable
        networkAvailable = up
        if cameBack {
            retrySoon(after: 2)
            flash("Back online — resuming downloads")
        }
    }

    /// Bring every planned automatic retry forward.
    func retrySoon(after seconds: Double) {
        let at = Date().addingTimeInterval(seconds)
        for job in jobs where job.retryAt != nil && job.phase == .failed {
            job.retryAt = min(job.retryAt ?? at, at)
        }
    }

    func retryDueDownloads() {
        guard autoRetry, networkAvailable else { return }
        let now = Date()
        for job in jobs where job.phase == .failed {
            guard let at = job.retryAt, at <= now else { continue }
            job.retryAt = nil
            job.appendLog("retrying automatically (attempt \(job.autoRetries))")
            resume(job)
        }
    }

    private func planRetry(_ job: DownloadJob) {
        guard autoRetry, job.connectionProblem, canRestart(job),
              job.autoRetries < Self.retryDelays.count else {
            job.retryAt = nil
            return
        }
        let delay = Self.retryDelays[job.autoRetries]
        job.autoRetries += 1
        job.retryAt = Date().addingTimeInterval(delay)
    }

    // MARK: keep the Mac awake while downloading

    private func updateAwake() {
        let busy = keepAwake && jobs.contains { $0.phase == .running || $0.phase == .queued }
        if busy, awakeActivity == nil {
            awakeActivity = ProcessInfo.processInfo.beginActivity(
                options: [.userInitiated, .idleSystemSleepDisabled],
                reason: "Downloading files")
        } else if !busy, let activity = awakeActivity {
            ProcessInfo.processInfo.endActivity(activity)
            awakeActivity = nil
        }
    }

    // MARK: derived state

    var activeJobs: [DownloadJob] { jobs.filter { $0.phase.isActive } }
    var finishedJobs: [DownloadJob] { jobs.filter { $0.phase.isFinished } }
    var runningCount: Int { jobs.filter { $0.phase == .running }.count }
    var hasWorkInFlight: Bool {
        jobs.contains { $0.phase == .running || $0.phase == .queued }
    }
    var totalSpeed: Double {
        jobs.filter { $0.phase == .running }.reduce(0) { $0 + $1.bytesPerSecond }
    }
    /// downloads this app is driving through a pipe right now
    private var ownedRunning: Int {
        jobs.filter { $0.phase == .running && $0.process != nil }.count
    }

    // MARK: feedback

    func flash(_ text: String, seconds: Double = 6) {
        status = text
        statusToken += 1
        let token = statusToken
        DispatchQueue.main.asyncAfter(deadline: .now() + seconds) { [weak self] in
            guard let self, self.statusToken == token else { return }
            self.status = ""
        }
    }

    private var notificationsUsable: Bool {
        #if DEBUG
        if UITestHooks.active { return false }
        #endif
        return notifyOnFinish && Bundle.main.bundleIdentifier != nil
    }

    /// Ask for permission to notify when it first matters (a download was
    /// just added and the user wants notifications), not at launch.
    private var askedForNotifications = false
    private func prepareNotifications() {
        guard notificationsUsable, !askedForNotifications else { return }
        askedForNotifications = true
        UNUserNotificationCenter.current().requestAuthorization(
            options: [.alert, .sound]) { _, _ in }
    }

    private func notify(_ title: String, _ body: String) {
        guard notificationsUsable else { return }
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        UNUserNotificationCenter.current().add(
            UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil))
    }

    func updateDockBadge() {
        let n = jobs.filter { $0.phase == .running || $0.phase == .queued }.count
        NSApp?.dockTile.badgeLabel = n > 0 ? String(n) : nil
        updateAwake()
    }

    // MARK: starting

    @discardableResult
    func start(_ request: DownloadRequest) -> DownloadJob {
        // the same file again (a paused download, or one whose link expired):
        // continue the saved part instead of downloading it a second time
        if !request.isTorrent,
           let earlier = jobs.first(where: { $0.canUpdateLink && $0.process == nil
                                             && ($0.url == request.url
                                                 || Naming.sameFileLink($0.url, request.url)) }) {
            if earlier.url == request.url {
                resume(earlier)
                flash("“\(earlier.title)” was already started — continuing it")
            } else {
                earlier.fallbackRequest = request
                updateLink(earlier, to: request.url)
                flash("Continuing “\(earlier.title)” through the new link")
            }
            return earlier
        }
        return enqueue(request)
    }

    @discardableResult
    private func enqueue(_ request: DownloadRequest) -> DownloadJob {
        let job = DownloadJob(url: request.url, title: request.title, poster: request.poster)
        job.args = request.arguments
        job.isTorrent = request.isTorrent
        job.phase = .queued
        jobs.insert(job, at: 0)
        prepareNotifications()
        pump()
        updateDockBadge()
        return job
    }

    /// Launch queued downloads while there is room (oldest first).
    func pump() {
        guard !terminating else { return }
        let limit = maxConcurrent <= 0 ? Int.max : maxConcurrent
        while ownedRunning < limit,
              let next = jobs.last(where: { $0.phase == .queued }) {
            launch(next)
        }
        updateDockBadge()
    }

    /// True when the engine still has this download's partial data + state
    /// (so it can continue instead of starting over).
    private func hasEngineState(_ job: DownloadJob) -> Bool {
        guard job.engineID != nil else { return false }
        if job.isTorrent || job.args.isEmpty { return true }   // followed via mgd list
        guard let out = job.outPath, !out.isEmpty else { return false }
        return FileManager.default.fileExists(atPath: out + ".mgdstate")
    }

    private func launch(_ job: DownloadJob) {
        let args: [String]
        if let id = job.engineID, hasEngineState(job), let link = job.relinkURL {
            args = ["resume", id, "--url", link, "--json-progress"]
            job.appendLog("continuing through the new link…")
        } else if let id = job.engineID, hasEngineState(job) {
            args = ["resume", id, "--json-progress"]
            job.appendLog("resuming…")
        } else if !job.args.isEmpty {
            args = job.args
            job.engineID = nil      // a fresh run gets a fresh id
        } else {
            job.phase = .failed
            job.errorMessage = "This download can no longer be resumed (its data is gone)."
            scheduleSave()
            return
        }
        job.relinkURL = nil
        job.stateMissingSince = nil
        job.completionAnnounced = false
        job.phase = .running
        job.stopRequested = false
        job.cancelRequested = false
        job.errorMessage = nil
        job.resetSpeed()
        job.process = Engine.spawn(args, onLine: { [weak job] line in
            job?.ingest(line)
        }, onEnd: { [weak self, weak job] code in
            guard let self, let job else { return }
            self.processEnded(job, code: code)
        })
    }

    private func processEnded(_ job: DownloadJob, code: Int32) {
        job.process = nil
        job.bytesPerSecond = 0
        if job.cancelRequested {
            cleanUp(job)
            pump()
            return
        }
        if job.phase != .done {
            if code == 0 {
                // yt-dlp/torrent runs that never sent a "done" with a path
                if job.finishedPath == nil {
                    job.finishedPath = job.candidatePath ?? job.outPath
                }
                if job.finishedSize == 0 { job.finishedSize = max(job.total, job.done) }
                job.finishedAt = Date()
                job.phase = .done
            } else if job.stopRequested || code == 130 || job.phase == .paused {
                job.phase = .paused
            } else {
                job.phase = .failed
                if job.errorMessage == nil { job.errorMessage = job.failureSummary }
            }
        }
        if job.phase == .failed {
            planRetry(job)
        } else {
            job.retryAt = nil
            if job.phase == .done { job.autoRetries = 0 }
        }
        if job.phase == .failed, job.errorKind == "link_mismatch",
           let fallback = job.fallbackRequest {
            // the link sent for it was a different file: download that one
            // as it was asked for; the saved part stays where it was
            job.fallbackRequest = nil
            enqueue(fallback)
        }
        if job.phase != .failed { job.fallbackRequest = nil }
        if job.phase == .done {
            if job.finishedAt == nil { job.finishedAt = Date() }
            job.adopted = false
            announceCompletion(job)
        } else if job.phase == .failed, job.retryAt != nil {
            flash("Connection problem with “\(job.title)” — it will retry by itself")
        } else if job.phase == .failed {
            notify("Download failed", job.title)
            flash("Failed: \(job.title)")
        }
        scheduleSave()
        pump()
        updateDockBadge()
    }

    private func announceCompletion(_ job: DownloadJob) {
        guard !job.completionAnnounced else { return }
        job.completionAnnounced = true
        let name = job.finishedPath.map { ($0 as NSString).lastPathComponent } ?? job.title
        notify(job.isTorrent ? "Torrent complete" : "Download complete", name)
        flash("Finished: \(name)")
    }

    // MARK: pause / resume / cancel

    func pause(_ job: DownloadJob) {
        job.retryAt = nil
        switch job.phase {
        case .queued:
            job.phase = .paused
            scheduleSave()
        case .running:
            job.stopRequested = true
            if let process = job.process, process.isRunning {
                process.terminate()          // SIGTERM: the engine saves its position
            } else if let id = job.engineID {
                Task {
                    _ = await Engine.run(["pause", id])
                    await MainActor.run { self.refresh() }
                }
            }
        default:
            break
        }
        updateDockBadge()
    }

    func resume(_ job: DownloadJob) {
        guard job.phase == .paused || job.phase == .failed else { return }
        guard canRestart(job) else {
            flash("“\(job.title)” cannot be restarted — add the link again")
            return
        }
        job.phase = .queued
        job.errorMessage = nil
        job.retryAt = nil
        // move to the front of the queue order (jobs.last is launched first)
        pump()
        scheduleSave()
    }

    /// Continue a download whose link expired through a new link (the file
    /// link itself, or the page it is on). The engine checks it is the same
    /// file before touching the saved part.
    func updateLink(_ job: DownloadJob, to link: String) {
        guard job.canUpdateLink else {
            flash("“\(job.title)” has no saved part to continue")
            return
        }
        job.relinkURL = link
        job.url = link
        job.errorMessage = nil
        job.errorKind = nil
        job.phase = .queued
        pump()
        scheduleSave()
    }

    func canRestart(_ job: DownloadJob) -> Bool {
        !job.args.isEmpty || hasEngineState(job)
    }

    func pauseAll() {
        for job in jobs where job.phase == .running || job.phase == .queued {
            pause(job)
        }
    }

    func resumeAll() {
        for job in jobs.reversed() where job.phase == .paused {
            resume(job)
        }
    }

    /// Stop a download and delete its partial data, then drop the row.
    /// "Try Again" from the user: a fresh set of automatic retries too.
    func retryNow(_ job: DownloadJob) {
        job.autoRetries = 0
        resume(job)
    }

    func cancel(_ job: DownloadJob) {
        guard job.phase.isActive || job.phase == .failed else { return }
        job.cancelRequested = true
        if let process = job.process, process.isRunning {
            process.terminate()          // processEnded → cleanUp
        } else {
            cleanUp(job)
        }
    }

    private func cleanUp(_ job: DownloadJob) {
        let id = job.engineID
        let partials = [job.candidatePath, job.outPath].compactMap { $0 }
        let engineBacked = id != nil
        remove(job, quiet: true)
        Task.detached {
            if let id {
                _ = Engine.runBlocking(["cancel", id])
            }
            if !engineBacked {
                // yt-dlp leaves "<name>.part" / "<name>.ytdl" beside the target
                let fm = FileManager.default
                for base in partials {
                    for suffix in [".part", ".ytdl"] {
                        try? fm.removeItem(atPath: base + suffix)
                    }
                }
            }
            await MainActor.run { JobStore.shared.refresh() }
        }
    }

    // MARK: removing rows / files

    func remove(_ job: DownloadJob, quiet: Bool = false) {
        jobs.removeAll { $0.id == job.id }
        selected.remove(job.id)
        scheduleSave()
        updateDockBadge()
        if !quiet { flash("Removed “\(job.title)” from the list (file kept)") }
    }

    /// Move the finished file to the Trash and drop the row.
    func trashFile(of job: DownloadJob) {
        let path = job.finishedPath
        remove(job, quiet: true)
        guard let path, !path.isEmpty, FileManager.default.fileExists(atPath: path) else {
            flash("Removed from the list (the file was already gone)")
            return
        }
        let name = (path as NSString).lastPathComponent
        do {
            try FileManager.default.trashItem(at: URL(fileURLWithPath: path), resultingItemURL: nil)
            flash("Moved to Trash: \(name)")
        } catch {
            NSWorkspace.shared.recycle([URL(fileURLWithPath: path)]) { [weak self] _, err in
                DispatchQueue.main.async {
                    self?.flash(err == nil ? "Moved to Trash: \(name)"
                                : "Could not delete the file: \(err!.localizedDescription)")
                }
            }
        }
    }

    func clearFinished() {
        let n = finishedJobs.count
        jobs.removeAll { $0.phase.isFinished }
        selected.formIntersection(Set(jobs.map(\.id)))
        scheduleSave()
        flash("Cleared \(n) finished download\(n == 1 ? "" : "s") from the list (files kept)")
    }

    // MARK: selection

    var selectedJobs: [DownloadJob] { jobs.filter { selected.contains($0.id) } }

    func toggleSelection(_ job: DownloadJob) {
        if selected.contains(job.id) { selected.remove(job.id) } else { selected.insert(job.id) }
    }

    func select(_ list: [DownloadJob]) { selected.formUnion(list.map(\.id)) }
    func clearSelection() { selected.removeAll() }

    /// Forget the selected rows. Finished files stay on disk; unfinished
    /// downloads are stopped and their partial data removed (they would
    /// otherwise come straight back as "paused").
    func removeSelected() {
        let picked = selectedJobs
        for job in picked {
            if job.phase.isActive { cancel(job) } else { remove(job, quiet: true) }
        }
        clearSelection()
        flash("Removed \(picked.count) item\(picked.count == 1 ? "" : "s") from the list")
    }

    /// Remove the selected rows and move their finished files to the Trash.
    func deleteSelected() {
        let picked = selectedJobs
        var trashed = 0
        for job in picked {
            if job.phase.isActive {
                cancel(job)
            } else if job.phase == .done, let p = job.finishedPath,
                      FileManager.default.fileExists(atPath: p) {
                trashFile(of: job)
                trashed += 1
            } else if job.phase == .failed {
                cancel(job)
            } else {
                remove(job, quiet: true)
            }
        }
        clearSelection()
        flash("Deleted \(picked.count) item\(picked.count == 1 ? "" : "s")"
              + (trashed > 0 ? " — \(trashed) file\(trashed == 1 ? "" : "s") moved to Trash" : ""))
    }

    // MARK: finder

    func reveal(_ job: DownloadJob) {
        let path = job.finishedPath ?? job.outPath ?? ""
        guard !path.isEmpty else {
            flash("No file location recorded for “\(job.title)”")
            return
        }
        let fm = FileManager.default
        var isDir: ObjCBool = false
        if fm.fileExists(atPath: path, isDirectory: &isDir) {
            if isDir.boolValue {
                NSWorkspace.shared.open(URL(fileURLWithPath: path))
            } else {
                NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
            }
            return
        }
        let dir = (path as NSString).deletingLastPathComponent
        if fm.fileExists(atPath: dir) {
            NSWorkspace.shared.open(URL(fileURLWithPath: dir))
            flash(job.phase == .done ? "The file was moved or deleted — opened its folder instead"
                                     : "Opened the folder it is downloading into")
        } else {
            NSWorkspace.shared.open(URL(fileURLWithPath: downloadRoot))
            flash("That folder no longer exists — opened the downloads folder")
        }
    }

    func open(_ job: DownloadJob) {
        guard let path = job.finishedPath, !path.isEmpty,
              FileManager.default.fileExists(atPath: path) else {
            flash("The file no longer exists: \(job.title)")
            return
        }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func fileExists(_ job: DownloadJob) -> Bool {
        guard let p = job.finishedPath, !p.isEmpty else { return false }
        return FileManager.default.fileExists(atPath: p)
    }

    // MARK: following the engine (downloads started anywhere)

    /// Poll `mgd list`: adopt downloads started outside this window (the
    /// extension's headless mode, a terminal, a previous session), follow
    /// their progress and notice when they finish or disappear.
    func refresh() {
        guard !refreshing else { return }
        refreshing = true
        Task.detached(priority: .utility) {
            let entries = ListEntry.parse(Engine.runJSONBlocking(["list", "--json"]))
            await MainActor.run {
                let store = JobStore.shared
                store.refreshing = false
                store.engineReachable = entries != nil
                if let entries { store.apply(entries) }
            }
        }
    }

    func apply(_ entries: [ListEntry]) {
        var changed = false
        let byID = Dictionary(entries.map { ($0.id, $0) }, uniquingKeysWith: { a, _ in a })
        let known = Dictionary(jobs.compactMap { j in j.engineID.map { ($0, j) } },
                               uniquingKeysWith: { a, _ in a })
        for entry in entries {
            if let job = known[entry.id] {
                guard job.process == nil else { continue }   // the pipe is fresher
                if entry.size > 0, job.total != entry.size { job.total = entry.size }
                if job.outPath == nil, !entry.outPath.isEmpty { job.outPath = entry.outPath }
                if entry.running {
                    job.setProgress(entry.done)
                    if job.phase != .running {
                        job.phase = .running
                        job.adopted = true
                        job.resetSpeed()
                        changed = true
                    }
                } else {
                    if job.done != entry.done { job.done = entry.done }
                    if job.phase == .running {
                        job.phase = .paused
                        job.bytesPerSecond = 0
                        changed = true
                    }
                }
            } else {
                // one of our own downloads whose "start" line (carrying this
                // id) is still on its way through the pipe: not a stranger
                let starting = jobs.contains {
                    $0.process != nil && $0.engineID == nil
                        && ($0.url == entry.url || $0.isTorrent == entry.isTorrent && entry.isTorrent)
                }
                if starting { continue }
                let job = DownloadJob(url: entry.url, title: entry.displayName)
                job.engineID = entry.id
                job.outPath = entry.outPath.isEmpty ? nil : entry.outPath
                job.isTorrent = entry.isTorrent
                job.adopted = true
                job.done = entry.done
                job.total = entry.size
                job.phase = entry.running ? .running : .paused
                job.resetSpeed()
                jobs.insert(job, at: 0)
                changed = true
            }
        }
        // Self-healing: a download this app is driving whose engine state
        // is gone and whose file is complete on disk has finished, even if
        // the engine's last lines never reached us. Give the normal "done"
        // a few seconds to arrive first.
        for job in jobs where job.process != nil && job.phase == .running && !job.isTorrent {
            let fm = FileManager.default
            guard let id = job.engineID, byID[id] == nil, let out = job.outPath,
                  let size = (try? fm.attributesOfItem(atPath: out)[.size] as? NSNumber)?.int64Value,
                  size > 0, job.total <= 0 || size == job.total,
                  !fm.fileExists(atPath: out + ".mgdstate"),
                  !fm.fileExists(atPath: out + ".mgdpart") else {
                job.stateMissingSince = nil
                continue
            }
            let since = job.stateMissingSince ?? Date()
            job.stateMissingSince = since
            guard Date().timeIntervalSince(since) > 4 else { continue }
            #if DEBUG
            UITestHooks.trace("self-heal: \(job.title) finished on disk, engine pid \(job.process?.processIdentifier ?? -1) running=\(job.process?.isRunning ?? false)")
            #endif
            job.finishedPath = out
            job.finishedSize = size
            job.done = size
            job.finishedAt = Date()
            job.bytesPerSecond = 0
            job.stateMissingSince = nil
            job.phase = .done
            announceCompletion(job)
            if let process = job.process, process.isRunning {
                job.stopRequested = true
                process.terminate()        // its job is done; do not leave it hanging
            }
            changed = true
        }
        // downloads whose engine state disappeared while nobody here was
        // driving them: finished elsewhere (file is there) or cancelled
        for job in jobs where job.process == nil && job.phase.isActive {
            guard let id = job.engineID, byID[id] == nil else { continue }
            if job.phase == .queued && !job.args.isEmpty { continue }   // about to start fresh
            if let out = job.outPath, !job.isTorrent,
               FileManager.default.fileExists(atPath: out) {
                job.finishedPath = out
                let size = (try? FileManager.default.attributesOfItem(atPath: out)[.size]
                            as? NSNumber)?.int64Value ?? job.total
                job.finishedSize = size
                job.total = size
                job.done = size
                job.finishedAt = Date()
                job.bytesPerSecond = 0
                job.adopted = false
                job.phase = .done
                notify("Download complete", (out as NSString).lastPathComponent)
                flash("Finished: \((out as NSString).lastPathComponent)")
            } else if job.isTorrent, job.phase == .running, let out = job.outPath {
                job.finishedPath = out
                job.finishedSize = job.total
                job.done = job.total
                job.finishedAt = Date()
                job.bytesPerSecond = 0
                job.adopted = false
                job.phase = .done
                notify("Torrent complete", job.title)
            } else if job.args.isEmpty {
                jobs.removeAll { $0.id == job.id }     // cancelled elsewhere
                selected.remove(job.id)
            } else {
                // its partial data is gone but we know how to start it again
                job.engineID = nil
                job.done = 0
                if job.phase == .running { job.phase = .paused }
            }
            changed = true
        }
        if changed {
            scheduleSave()
            updateDockBadge()
        }
    }

    // MARK: engine settings

    /// true while yt-dlp is being updated
    @Published var updatingTool = false
    @Published var ytdlpBannerDismissed = false

    /// checkUpdates: also ask GitHub for the newest yt-dlp (once per launch,
    /// and when Diagnostics checks again)
    func loadEngineSettings(checkUpdates: Bool = false) {
        Task.detached(priority: .utility) {
            let cfg = Engine.runJSONBlocking(["defaults", "--json"])
            let doc = EngineReport.parse(Engine.runJSONBlocking(
                ["doctor", "--json"] + (checkUpdates ? ["--check-updates"] : [])))
            await MainActor.run {
                let store = JobStore.shared
                if let cfg {
                    if let root = cfg["root"] as? String, !root.isEmpty { store.downloadRoot = root }
                    store.speedLimit = (cfg["speed_limit"] as? NSNumber)?.intValue ?? 0
                }
                if !checkUpdates, let old = store.report, old.ytdlpVersion == doc.ytdlpVersion {
                    var merged = doc          // keep what the last update check found
                    merged.ytdlpLatest = old.ytdlpLatest
                    merged.ytdlpUpdateAvailable = old.ytdlpUpdateAvailable
                    store.report = merged
                } else {
                    store.report = doc
                }
                store.engineReachable = doc.reachable
            }
        }
    }

    /// Update yt-dlp the way it was installed (Homebrew, pip, own updater).
    @discardableResult
    func updateYtdlp() async -> String {
        await MainActor.run { self.updatingTool = true }
        let answer = await Engine.runJSON(["update-tool", "yt-dlp", "--json"])
        let doc = EngineReport.parse(await Engine.runJSON(["doctor", "--json", "--check-updates"]))
        let message = (answer?["message"] as? String) ?? "The update did not run"
        await MainActor.run {
            self.updatingTool = false
            if doc.reachable { self.report = doc }
            self.flash(message, seconds: 10)
        }
        return message
    }

    static let speedChoices: [(label: String, value: String, bytes: Int)] = [
        ("Unlimited", "off", 0), ("10 MB/s", "10MB", 10_000_000), ("5 MB/s", "5MB", 5_000_000),
        ("2 MB/s", "2MB", 2_000_000), ("1 MB/s", "1MB", 1_000_000), ("500 KB/s", "500KB", 500_000),
    ]

    var speedLimitLabel: String {
        guard speedLimit > 0 else { return "Unlimited" }
        if let known = Self.speedChoices.first(where: { $0.bytes == speedLimit }) { return known.label }
        return fmtSpeed(Double(speedLimit))
    }

    func setSpeedLimit(_ choice: (label: String, value: String, bytes: Int)) {
        speedLimit = choice.bytes
        Task { _ = await Engine.run(["set-default", "speed-limit", choice.value]) }
        flash(choice.bytes == 0 ? "Speed limit off"
              : "Speed limit \(choice.label), shared by all downloads (applies within a few seconds)")
    }

    func setDownloadRoot(_ path: String) {
        downloadRoot = path
        Task {
            _ = await Engine.run(["set-default", "root", path])
            await MainActor.run { self.flash("New downloads are saved in \(tildePath(path))") }
        }
    }

    // MARK: persistence (the list survives relaunches)

    static var dataDirectory: URL {
        if let override = ProcessInfo.processInfo.environment["MEDIAGRAB_DATA_DIR"],
           !override.isEmpty {
            let url = URL(fileURLWithPath: override, isDirectory: true)
            try? FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
            return url
        }
        let dir = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
            .appendingPathComponent("MediaGrab", isDirectory: true)
        try? FileManager.default.createDirectory(at: dir, withIntermediateDirectories: true)
        return dir
    }

    static var historyURL: URL { dataDirectory.appendingPathComponent("history.json") }

    /// Coalesce bursts of changes into one write.
    func scheduleSave() {
        guard !saveScheduled else { return }
        saveScheduled = true
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.4) { [weak self] in
            self?.saveScheduled = false
            self?.saveHistory()
        }
    }

    /// What is worth remembering: finished rows, plus unfinished downloads
    /// only this app knows how to restart (yt-dlp-backed ones have no engine
    /// state file; the rest come back through `mgd list`).
    func saveHistory() {
        let f = ISO8601DateFormatter()
        f.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        var entries: [HistoryEntry] = []
        for job in jobs {
            let keep: Bool
            switch job.phase {
            case .done, .failed: keep = true
            case .queued, .running, .paused: keep = !job.args.isEmpty && !hasEngineState(job)
            }
            guard keep else { continue }
            let phase: JobPhase = job.phase.isFinished ? job.phase : .paused
            entries.append(HistoryEntry(
                url: job.url, title: job.title, phase: phase.rawValue,
                done: job.done, total: job.total,
                finishedPath: job.finishedPath, finishedSize: job.finishedSize,
                poster: job.poster,
                finishedAt: job.finishedAt.map { f.string(from: $0) },
                logTail: String(job.log.suffix(600)),
                args: job.args.isEmpty ? nil : job.args,
                error: job.errorMessage,
                torrent: job.isTorrent ? true : nil,
                engineID: job.phase == .failed ? job.engineID : nil,
                outPath: job.outPath,
                errorKind: job.errorKind))
            if entries.count >= 300 { break }
        }
        guard let data = try? JSONEncoder().encode(entries) else { return }
        // rolling one-generation backup: a bad write is always recoverable
        let url = Self.historyURL
        let bak = url.appendingPathExtension("bak")
        if FileManager.default.fileExists(atPath: url.path) {
            try? FileManager.default.removeItem(at: bak)
            try? FileManager.default.copyItem(at: url, to: bak)
        }
        try? data.write(to: url, options: .atomic)
    }

    private func loadHistory() {
        guard let data = try? Data(contentsOf: Self.historyURL),
              let entries = try? JSONDecoder().decode([HistoryEntry].self, from: data) else {
            return
        }
        for e in entries.prefix(300) {
            let job = DownloadJob(url: e.url, title: e.title, poster: e.poster)
            var phase = JobPhase(rawValue: e.phase) ?? .done
            if phase == .queued || phase == .running { phase = .paused }
            job.phase = phase
            job.done = e.done
            job.total = e.total
            job.finishedPath = e.finishedPath
            job.finishedSize = e.finishedSize
            job.finishedAt = e.finishedDate
            job.args = e.args ?? []
            job.errorMessage = e.error
            job.errorKind = e.errorKind
            job.isTorrent = e.torrent ?? false
            job.engineID = e.engineID
            job.outPath = e.outPath
            if let tail = e.logTail, !tail.isEmpty { job.log = tail }
            jobs.append(job)
        }
    }

    // MARK: quitting

    /// Pause everything this app is driving (the engine saves exact
    /// positions) and call back when the engines are gone.
    func pauseForQuit(_ done: @escaping () -> Void) {
        terminating = true
        let live = jobs.filter { $0.process?.isRunning == true }
        for job in jobs where job.phase == .queued { job.phase = .paused }
        for job in live {
            job.stopRequested = true
            job.process?.terminate()
        }
        let deadline = Date().addingTimeInterval(6)
        func check() {
            if live.allSatisfy({ $0.process?.isRunning != true }) || Date() > deadline {
                saveHistory()
                done()
            } else {
                DispatchQueue.main.asyncAfter(deadline: .now() + 0.15, execute: check)
            }
        }
        check()
    }
}
