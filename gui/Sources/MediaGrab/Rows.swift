import AppKit
import SwiftUI

// MARK: - shared pieces

struct RowCard<Content: View>: View {
    let selected: Bool
    @ViewBuilder let content: () -> Content

    var body: some View {
        content()
            .padding(.vertical, 9)
            .padding(.horizontal, 10)
            .frame(maxWidth: .infinity, alignment: .leading)
            .background(
                RoundedRectangle(cornerRadius: 10)
                    .fill(selected ? Color.accentColor.opacity(0.13)
                          : Color(NSColor.controlBackgroundColor))
            )
            .overlay(
                RoundedRectangle(cornerRadius: 10)
                    .strokeBorder(selected ? Color.accentColor.opacity(0.65)
                                  : Color.secondary.opacity(0.12), lineWidth: 1)
            )
    }
}

/// The multi-select toggle at the leading edge of every row.
struct SelectDot: View {
    let on: Bool
    let action: () -> Void

    var body: some View {
        Button(action: action) {
            Image(systemName: on ? "checkmark.circle.fill" : "circle")
                .font(.system(size: 15))
                .foregroundColor(on ? .accentColor : .secondary.opacity(0.6))
        }
        .buttonStyle(.plain)
        .frame(width: 20)
        .help(on ? "Deselect" : "Select (to remove or delete several at once)")
        .accessibilityLabel(on ? "Selected" : "Not selected")
    }
}

struct SectionHeader: View {
    let title: String
    let count: Int

    var body: some View {
        HStack(spacing: 6) {
            Text(title.uppercased())
                .font(.caption.weight(.semibold))
                .foregroundColor(.secondary)
            Text("\(count)")
                .font(.caption.monospacedDigit())
                .foregroundColor(.secondary.opacity(0.8))
            Spacer()
        }
        .padding(.top, 8).padding(.leading, 2)
    }
}

struct ProgressBar: View {
    /// 0…1, or negative when the total size is not known yet
    let fraction: Double
    let color: Color

    var body: some View {
        GeometryReader { geo in
            ZStack(alignment: .leading) {
                Capsule().fill(Color.secondary.opacity(0.18))
                if fraction >= 0 {
                    Capsule()
                        .fill(color)
                        .frame(width: max(5, geo.size.width * min(1, fraction)))
                }
            }
        }
        .frame(height: 5)
    }
}

func copyToClipboard(_ text: String) {
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
    Session.shared.clipboard.noteOwnCopy()
}

// MARK: - one download: the layout follows its state

/// Picks the in-progress or finished layout from the job's own phase, so a
/// row can never show stale controls (a Pause button on a finished file).
struct JobRow: View {
    @ObservedObject var job: DownloadJob
    let isSelected: Bool

    var body: some View {
        if job.phase.isFinished {
            FinishedJobRow(job: job, isSelected: isSelected)
        } else {
            ActiveJobRow(job: job, isSelected: isSelected)
        }
    }
}

// MARK: - a download in progress (running / queued / paused)

struct ActiveJobRow: View {
    @ObservedObject var job: DownloadJob
    let isSelected: Bool
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session

    private var tint: Color {
        switch job.phase {
        case .paused: return .orange
        case .queued: return .secondary
        default: return .accentColor
        }
    }

    private var statusLine: String {
        if job.converting {
            var parts = ["Making it play smoothly on your Mac",
                         "\(Int(job.convertProgress * 100))%", job.convertSummary]
            if !job.convertETA.isEmpty { parts.append(job.convertETA) }
            return parts.joined(separator: " · ")
        }
        var parts: [String] = []
        if job.total > 0 {
            parts.append("\(fmtBytes(job.done)) of \(fmtBytes(job.total))")
            parts.append("\(Int(max(0, job.percent)))%")
        } else if job.done > 0 {
            parts.append(fmtBytes(job.done))
        }
        switch job.phase {
        case .running:
            if !job.speedText.isEmpty { parts.append(job.speedText) }
            if !job.etaText.isEmpty { parts.append(job.etaText) }
            if parts.isEmpty { parts.append("Starting…") }
        case .queued:
            if let at = store.startAt, store.isHeld {
                parts.append("Starts \(JobStore.whenText(at))")
            } else {
                parts.append("Waiting for a free slot")
            }
        case .paused:
            parts.append("Paused")
        default:
            break
        }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        RowCard(selected: isSelected) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .center, spacing: 10) {
                    SelectDot(on: isSelected) { store.toggleSelection(job) }
                    stateIcon.frame(width: 28)
                    VStack(alignment: .leading, spacing: 4) {
                        HStack(spacing: 6) {
                            Text(job.title).lineLimit(1).truncationMode(.middle)
                                .font(.body.weight(.medium))
                            if job.adopted {
                                Text("from browser / terminal")
                                    .font(.caption2)
                                    .padding(.horizontal, 5).padding(.vertical, 1)
                                    .background(Capsule().fill(Color.secondary.opacity(0.15)))
                                    .foregroundColor(.secondary)
                                    .help("Started outside this window; MediaGrab is following it")
                            }
                        }
                        Text(statusLine)
                            .font(.caption).monospacedDigit()
                            .foregroundColor(.secondary).lineLimit(1)
                        if job.converting {
                            ProgressBar(fraction: job.convertProgress, color: .purple)
                                .help(job.convertReasons.joined(separator: "\n"))
                        } else {
                            if job.total <= 0 && job.phase == .running {
                                // size not known (yet): show that it is working
                                ProgressView()
                                    .progressViewStyle(.linear)
                                    .controlSize(.small)
                                    .tint(tint)
                            } else {
                                ProgressBar(fraction: job.total > 0 ? job.percent / 100 : -1, color: tint)
                            }
                        }
                    }
                    Spacer(minLength: 6)
                    controls
                }
                if job.showLog {
                    DetailsBox(job: job)
                }
            }
        }
        .contextMenu {
            if job.phase == .paused {
                Button("Resume") { store.resume(job) }
                if job.canUpdateLink {
                    Button("Update Link…") { session.sheet = .updateLink(job) }
                }
            } else {
                Button("Pause") { store.pause(job) }
            }
            Button("Show in Finder") { store.reveal(job) }
            Button("Copy Link") { copyToClipboard(job.url) }
            Divider()
            Button("Cancel and Delete Partial File", role: .destructive) { askCancel() }
        }
    }

    @ViewBuilder
    private var stateIcon: some View {
        if job.converting {
            Image(systemName: "wand.and.stars")
                .font(.system(size: 19)).foregroundColor(.purple)
        } else {
            phaseIcon
        }
    }

    @ViewBuilder
    private var phaseIcon: some View {
        switch job.phase {
        case .running:
            if job.total > 0 {
                Image(systemName: job.symbol)
                    .font(.system(size: 20)).foregroundColor(.accentColor)
            } else {
                ProgressView().controlSize(.small)
            }
        case .paused:
            Image(systemName: "pause.circle.fill")
                .font(.system(size: 22)).foregroundColor(.orange)
        default:
            Image(systemName: "clock")
                .font(.system(size: 19)).foregroundColor(.secondary)
        }
    }

    private var controls: some View {
        HStack(spacing: 4) {
            if job.converting {
                Button {
                    store.pause(job)          // stops the conversion; the download is kept
                } label: {
                    Label("Skip Conversion", systemImage: "forward.end")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Keep the file exactly as downloaded")
            } else if job.phase == .paused {
                Button {
                    store.resume(job)
                } label: {
                    Label("Resume", systemImage: "play.fill")
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.small)
                .help("Continue from where it stopped")
            } else {
                Button {
                    store.pause(job)
                } label: {
                    Label("Pause", systemImage: "pause.fill")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Pause and keep what is downloaded so far")
            }
            if !job.converting {
                Button {
                    askCancel()
                } label: {
                    Image(systemName: "xmark")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Cancel and delete the partial file")
                .accessibilityLabel("Cancel download")
            }
            Button {
                job.showLog.toggle()
            } label: {
                Image(systemName: job.showLog ? "chevron.up" : "chevron.down")
            }
            .buttonStyle(.borderless)
            .controlSize(.small)
            .help("Details")
            .accessibilityLabel("Details")
        }
    }

    private func askCancel() {
        // nothing downloaded yet: nothing to lose, no question
        guard job.done > 0 else {
            store.cancel(job)
            return
        }
        session.confirmation = Confirmation(
            title: "Cancel “\(job.title)”?",
            message: "The \(fmtBytes(job.done)) downloaded so far will be deleted.",
            actionLabel: "Cancel Download",
            action: { store.cancel(job) })
    }
}

/// Link, destination and the engine's recent output for one download.
struct DetailsBox: View {
    @ObservedObject var job: DownloadJob

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            detail("Link", job.url)
            if let path = job.finishedPath ?? job.outPath {
                detail("Saved to", tildePath(path))
            }
            if !job.log.isEmpty {
                ScrollView {
                    Text(String(job.log.suffix(1500)))
                        .font(.system(size: 10.5, design: .monospaced))
                        .foregroundColor(.secondary)
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)
                }
                .frame(maxHeight: 92)
            }
        }
        .padding(8)
        .background(RoundedRectangle(cornerRadius: 6)
            .fill(Color(NSColor.textBackgroundColor).opacity(0.6)))
        .padding(.leading, 68)
    }

    private func detail(_ label: String, _ value: String) -> some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text(label).font(.caption2.weight(.semibold)).foregroundColor(.secondary)
                .frame(width: 52, alignment: .trailing)
            Text(value).font(.caption).lineLimit(1).truncationMode(.middle)
                .textSelection(.enabled)
        }
    }
}

// MARK: - a finished download (done or failed)

struct FinishedJobRow: View {
    @ObservedObject var job: DownloadJob
    let isSelected: Bool
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session

    private var failed: Bool { job.phase == .failed }
    private var waitingToRetry: Bool {
        failed && job.connectionProblem && (job.retryAt != nil || !store.networkAvailable)
    }

    private var subtitle: String {
        if failed { return job.failureSummary }
        var parts: [String] = []
        let size = job.finishedSize > 0 ? job.finishedSize : job.total
        if size > 0 { parts.append(fmtBytes(size)) }
        let when = fmtWhen(job.finishedAt)
        if !when.isEmpty { parts.append(when) }
        if let path = job.finishedPath {
            parts.append(tildePath((path as NSString).deletingLastPathComponent))
        }
        return parts.joined(separator: " · ")
    }

    var body: some View {
        RowCard(selected: isSelected) {
            VStack(alignment: .leading, spacing: 6) {
                HStack(alignment: .center, spacing: 10) {
                    SelectDot(on: isSelected) { store.toggleSelection(job) }
                    Image(systemName: waitingToRetry ? "arrow.triangle.2.circlepath"
                          : failed ? "exclamationmark.triangle.fill" : job.symbol)
                        .font(.system(size: 20))
                        .foregroundColor(waitingToRetry ? .orange : failed ? .red : .accentColor)
                        .frame(width: 28)
                    VStack(alignment: .leading, spacing: 3) {
                        Text(job.title).lineLimit(1).truncationMode(.middle)
                            .font(.body.weight(.medium))
                        if failed && job.connectionProblem && (job.retryAt != nil || !store.networkAvailable) {
                            RetryLine(job: job, online: store.networkAvailable)
                        } else {
                            Text(subtitle)
                                .font(.caption)
                                .foregroundColor(failed ? .red.opacity(0.85) : .secondary)
                                .lineLimit(1).truncationMode(.middle)
                        }
                    }
                    Spacer(minLength: 6)
                    buttons
                }
                if job.showLog {
                    DetailsBox(job: job)
                }
            }
        }
        .onTapGesture(count: 2) {
            if !failed { store.open(job) }
        }
        .contextMenu {
            if failed {
                if store.canRestart(job) {
                    Button("Try Again") { store.retryNow(job) }
                }
                if job.canUpdateLink {
                    Button("Update Link…") { session.sheet = .updateLink(job) }
                }
            } else {
                Button("Open") { store.open(job) }
                Button("Show in Finder") { store.reveal(job) }
                if store.isVideoFile(job) {
                    Button("Make Mac-Ready") { store.makeMacReady([job]) }
                }
            }
            Button("Copy Link") { copyToClipboard(job.url) }
            Divider()
            Button("Remove from List") { failed ? store.cancel(job) : store.remove(job) }
            if !failed, job.finishedPath != nil {
                Button("Move File to Trash", role: .destructive) { store.trashFile(of: job) }
            }
        }
    }

    private var buttons: some View {
        HStack(spacing: 4) {
            if failed {
                if job.linkExpired && job.canUpdateLink {
                    Button {
                        session.sheet = .updateLink(job)
                    } label: {
                        Label("Update Link…", systemImage: "link.badge.plus")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .help("Paste a fresh link to this video (or its page): MediaGrab checks it is the same file and continues")
                } else if store.canRestart(job) {
                    Button {
                        store.retryNow(job)
                    } label: {
                        Label(job.retryAt != nil ? "Try Now" : "Try Again", systemImage: "arrow.clockwise")
                    }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.small)
                    .help("Start this download again (continues from what it already has)")
                }
                Button {
                    job.showLog.toggle()
                } label: {
                    Image(systemName: job.showLog ? "chevron.up" : "chevron.down")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .help("Why it failed")
                .accessibilityLabel("Details")
            } else {
                Button {
                    store.open(job)
                } label: {
                    Label("Open", systemImage: "play.fill")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Open with the default app")
                Button {
                    store.reveal(job)
                } label: {
                    Image(systemName: "folder")
                }
                .buttonStyle(.bordered)
                .controlSize(.small)
                .help("Show in Finder")
                .accessibilityLabel("Show in Finder")
            }
            Menu {
                if !failed {
                    Button("Open") { store.open(job) }
                    Button("Show in Finder") { store.reveal(job) }
                    if store.isVideoFile(job) {
                        Button("Make Mac-Ready") { store.makeMacReady([job]) }
                            .help("Convert it so it plays smoothly in QuickTime (only if needed)")
                    }
                    Divider()
                }
                Button("Copy Link") { copyToClipboard(job.url) }
                Button(job.showLog ? "Hide Details" : "Show Details") { job.showLog.toggle() }
                Divider()
                Button("Remove from List") { failed ? store.cancel(job) : store.remove(job) }
                if !failed, job.finishedPath != nil {
                    Button("Move File to Trash", role: .destructive) { store.trashFile(of: job) }
                }
            } label: {
                Image(systemName: "ellipsis")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .frame(width: 26)
            .help("More actions")
            .accessibilityLabel("More actions")
        }
    }
}

/// "Connection problem — retrying in 0:25" with a live countdown.
struct RetryLine: View {
    @ObservedObject var job: DownloadJob
    let online: Bool

    var body: some View {
        TimelineView(.periodic(from: .now, by: 1)) { context in
            Label(text(at: context.date), systemImage: online ? "arrow.triangle.2.circlepath" : "wifi.slash")
                .font(.caption)
                .foregroundColor(.orange)
                .lineLimit(1)
        }
    }

    private func text(at now: Date) -> String {
        let attempt = "attempt \(job.autoRetries) of \(JobStore.retryDelays.count)"
        guard online else {
            return "No internet connection — continues when it is back"
        }
        guard let at = job.retryAt else { return "Connection problem" }
        let left = max(0, Int(at.timeIntervalSince(now).rounded()))
        return left == 0 ? "Connection problem — retrying now (\(attempt))"
            : "Connection problem — retrying in \(fmtDuration(Double(left))) (\(attempt))"
    }
}
