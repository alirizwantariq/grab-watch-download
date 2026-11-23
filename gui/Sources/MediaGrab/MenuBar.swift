import AppKit
import SwiftUI

// MARK: - menu bar icon

/// The icon in the menu bar: filled while downloading, with the overall
/// progress next to it.
struct MenuBarLabel: View {
    @ObservedObject var store: JobStore

    var body: some View {
        if let percent = store.overallPercent {
            Text("\(Image(systemName: "arrow.down.circle.fill")) \(percent)%")
                .monospacedDigit()
        } else {
            Image(systemName: store.hasWorkInFlight ? "arrow.down.circle.fill" : "arrow.down.circle")
        }
    }
}

/// What the menu bar icon opens: the downloads in progress (pause/resume),
/// a few finished ones, and the main actions - usable with the window closed.
struct MenuBarContent: View {
    @EnvironmentObject var store: JobStore
    @EnvironmentObject var session: Session
    @Environment(\.openWindow) private var openWindow

    private static let shown = 6

    private var active: [DownloadJob] {
        store.jobs.filter { $0.phase.isActive || $0.converting }
    }
    private var recent: [DownloadJob] {
        store.jobs.filter { $0.phase == .done && !$0.converting }
            .sorted { ($0.finishedAt ?? .distantPast) > ($1.finishedAt ?? .distantPast) }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            header
                .padding(.horizontal, 14).padding(.top, 12).padding(.bottom, 8)
            if let at = store.startAt {
                notice(symbol: "clock", text: "Downloads start \(JobStore.whenText(at))",
                       button: "Start Now") { store.startScheduledNow() }
            }
            if let left = store.whenDoneCountdown {
                notice(symbol: store.whenDone.symbol, tint: .orange,
                       text: "All done — \(store.whenDone.doing) in \(left) s",
                       button: "Cancel") { store.whenDone = .nothing }
            } else if store.whenDone != .nothing {
                notice(symbol: store.whenDone.symbol, tint: .secondary,
                       text: "When all downloads finish: \(store.whenDone.label)",
                       button: "Don't") { store.whenDone = .nothing }
            }
            Divider()
            if active.isEmpty {
                Text("Nothing downloading. Copy a link, then Paste Link.")
                    .font(.callout).foregroundColor(.secondary)
                    .padding(.horizontal, 14).padding(.vertical, 12)
            } else {
                VStack(spacing: 0) {
                    ForEach(active.prefix(Self.shown)) { job in
                        MenuBarJobRow(job: job)
                    }
                }
                .padding(.vertical, 4)
                if active.count > Self.shown {
                    Text("and \(active.count - Self.shown) more")
                        .font(.caption).foregroundColor(.secondary)
                        .padding(.horizontal, 14).padding(.bottom, 6)
                }
            }
            if !recent.isEmpty {
                Divider()
                Text("RECENTLY FINISHED")
                    .font(.caption2.weight(.semibold)).foregroundColor(.secondary)
                    .padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 2)
                ForEach(recent.prefix(3)) { job in
                    Button {
                        store.reveal(job)
                    } label: {
                        HStack(spacing: 8) {
                            Image(systemName: fileSymbol(forPath: job.finishedPath ?? job.title))
                                .foregroundColor(.accentColor).frame(width: 16)
                            Text(job.title).lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Text(fmtWhen(job.finishedAt)).font(.caption).foregroundColor(.secondary)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .padding(.horizontal, 14).padding(.vertical, 4)
                    .help("Show in Finder")
                }
                .padding(.bottom, 4)
            }
            Divider()
            HStack(spacing: 8) {
                Button {
                    showWindow()
                    session.pasteAndGrab()
                } label: {
                    Label("Paste Link", systemImage: "doc.on.clipboard")
                }
                if store.hasWorkInFlight {
                    Button {
                        store.pauseAll()
                    } label: {
                        Label("Pause All", systemImage: "pause.fill")
                    }
                } else if store.jobs.contains(where: { $0.phase == .paused }) {
                    Button {
                        store.resumeAll()
                    } label: {
                        Label("Resume All", systemImage: "play.fill")
                    }
                }
                Spacer()
                Button("Open MediaGrab") { showWindow() }
                    .keyboardShortcut("o")
            }
            .controlSize(.small)
            .padding(.horizontal, 14).padding(.vertical, 10)
            Divider()
            HStack {
                if #available(macOS 14.0, *) {
                    SettingsLink { Text("Settings…") }
                        .simultaneousGesture(TapGesture().onEnded {
                            NSApp.activate(ignoringOtherApps: true)
                        })
                } else {
                    Button("Settings…") {
                        NSApp.activate(ignoringOtherApps: true)
                        NSApp.sendAction(Selector(("showPreferencesWindow:")), to: nil, from: nil)
                    }
                }
                Spacer()
                Button("Quit MediaGrab") { NSApp.terminate(nil) }
                    .keyboardShortcut("q")
            }
            .buttonStyle(.borderless)
            .font(.callout)
            .padding(.horizontal, 14).padding(.vertical, 8)
        }
        .frame(width: 340)
        .onAppear { session.openMainWindow = { openWindow(id: "main") } }
    }

    private var header: some View {
        HStack(alignment: .firstTextBaseline) {
            Text("MediaGrab").font(.headline)
            Spacer()
            Text(summary).font(.caption).foregroundColor(.secondary).monospacedDigit()
        }
    }

    private var summary: String {
        let running = store.runningCount
        let queued = store.jobs.filter { $0.phase == .queued }.count
        var parts: [String] = []
        if running > 0 { parts.append("\(running) downloading") }
        if queued > 0 { parts.append("\(queued) waiting") }
        if store.totalSpeed > 0 { parts.append(fmtSpeed(store.totalSpeed)) }
        return parts.isEmpty ? "Idle" : parts.joined(separator: " · ")
    }

    private func notice(symbol: String, tint: Color = .accentColor, text: String,
                        button: String, action: @escaping () -> Void) -> some View {
        HStack(spacing: 8) {
            Image(systemName: symbol).foregroundColor(tint)
            Text(text).font(.callout).lineLimit(1).monospacedDigit()
            Spacer()
            Button(button, action: action).controlSize(.small)
        }
        .padding(.horizontal, 14).padding(.vertical, 6)
        .background(tint.opacity(0.10))
    }

    private func showWindow() {
        openWindow(id: "main")
        NSApp.activate(ignoringOtherApps: true)
    }
}

/// One download in the menu: name, a thin progress bar, status, and
/// pause / resume.
struct MenuBarJobRow: View {
    @ObservedObject var job: DownloadJob
    @EnvironmentObject var store: JobStore

    var body: some View {
        HStack(spacing: 10) {
            VStack(alignment: .leading, spacing: 3) {
                Text(job.title).lineLimit(1).truncationMode(.middle)
                if job.converting {
                    ProgressView(value: min(1, max(0, job.convertProgress)))
                        .tint(.purple)
                } else if job.total > 0 || job.phase != .running {
                    ProgressView(value: min(1, max(0, job.percent / 100)))
                        .tint(job.phase == .paused ? .orange : .accentColor)
                } else {
                    ProgressView().progressViewStyle(.linear)
                }
                Text(status).font(.caption).foregroundColor(.secondary).monospacedDigit()
                    .lineLimit(1)
            }
            if !job.converting {
                if job.phase == .paused {
                    Button {
                        store.resume(job)
                    } label: {
                        Image(systemName: "play.circle.fill").font(.title3)
                    }
                    .buttonStyle(.borderless)
                    .help("Resume")
                    .accessibilityLabel("Resume \(job.title)")
                } else {
                    Button {
                        store.pause(job)
                    } label: {
                        Image(systemName: "pause.circle.fill").font(.title3)
                    }
                    .buttonStyle(.borderless)
                    .help("Pause")
                    .accessibilityLabel("Pause \(job.title)")
                }
            }
        }
        .controlSize(.small)
        .padding(.horizontal, 14).padding(.vertical, 5)
    }

    private var status: String {
        if job.converting {
            return "Making it Mac-ready · \(Int(job.convertProgress * 100))%"
        }
        var parts: [String] = []
        if job.total > 0 {
            parts.append("\(Int(max(0, job.percent)))% of \(fmtBytes(job.total))")
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
                parts.append("starts \(JobStore.whenText(at))")
            } else {
                parts.append("waiting")
            }
        case .paused:
            parts.append("paused")
        default:
            break
        }
        return parts.joined(separator: " · ")
    }
}
