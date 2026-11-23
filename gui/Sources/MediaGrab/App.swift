import AppKit
import SwiftUI

@main
struct MediaGrabApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) var appDelegate
    @StateObject private var store = JobStore.shared
    @StateObject private var session = Session.shared

    var body: some Scene {
        // One window, one download list. (A WindowGroup would open a new
        // window - each with its own copy of the list - for every link
        // handed over by the browser.)
        Window("MediaGrab", id: "main") {
            ContentView()
                .environmentObject(store)
                .environmentObject(session)
                .frame(minWidth: 820, minHeight: 540)
                .onOpenURL { session.handleOpenURL($0) }
        }
        .defaultSize(width: 980, height: 660)
        .handlesExternalEvents(matching: ["*"])
        .commands {
            CommandGroup(replacing: .newItem) {
                Button("Paste Link and Download") { session.pasteAndGrab() }
                    .keyboardShortcut("v", modifiers: [.command, .shift])
                Divider()
                Button("Open Downloads Folder") {
                    NSWorkspace.shared.open(URL(fileURLWithPath: store.downloadRoot))
                }
                .keyboardShortcut("o", modifiers: [.command, .shift])
            }
            CommandMenu("Downloads") {
                Button("Resume All") { store.resumeAll() }
                    .keyboardShortcut("r", modifiers: [.command, .shift])
                    .disabled(!store.jobs.contains { $0.phase == .paused })
                Button("Pause All") { store.pauseAll() }
                    .keyboardShortcut("p", modifiers: [.command, .shift])
                    .disabled(!store.hasWorkInFlight)
                Divider()
                Button("Select All") { store.select(store.jobs) }
                    .keyboardShortcut("a", modifiers: [.command, .shift])
                    .disabled(store.jobs.isEmpty)
                Button("Clear Finished from List") { store.clearFinished() }
                    .disabled(store.finishedJobs.isEmpty)
                Divider()
                Button("Browser Extension…") { session.sheet = .extensionSetup }
                Button("Diagnostics…") { session.sheet = .diagnostics }
            }
        }

        Settings {
            SettingsView()
                .environmentObject(store)
        }
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        #if DEBUG
        if UITestHooks.active {          // scripted run: no prompts, no focus
            JobStore.shared.activate()
            UITestHooks.run()
            return
        }
        #endif
        if Bundle.main.bundleIdentifier == nil {
            // `swift run`: no bundle, so become a regular foreground app by hand
            NSApp.setActivationPolicy(.regular)
            NSApp.activate(ignoringOtherApps: true)
        }
        JobStore.shared.activate()
        BrowserLink.repairIfMoved()
        BrowserLink.refreshExportedExtension()
    }

    /// Closing the window quits the app - unless downloads are still going,
    /// which keep running with the dock badge showing their count.
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        !JobStore.shared.hasWorkInFlight
    }

    func applicationShouldHandleReopen(_ sender: NSApplication,
                                       hasVisibleWindows flag: Bool) -> Bool {
        if !flag {
            for window in sender.windows where window.canBecomeMain {
                window.makeKeyAndOrderFront(nil)
                return false
            }
        }
        return true
    }

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        let store = JobStore.shared
        let live = store.jobs.filter { $0.process?.isRunning == true }
        guard !live.isEmpty else {
            store.saveHistory()
            return .terminateNow
        }
        let alert = NSAlert()
        alert.messageText = live.count == 1
            ? "A download is still running"
            : "\(live.count) downloads are still running"
        alert.informativeText = "Quitting pauses "
            + (live.count == 1 ? "it" : "them")
            + ". Progress is saved and you can resume next time you open MediaGrab."
        alert.addButton(withTitle: "Pause and Quit")
        alert.addButton(withTitle: "Keep Downloading")
        guard alert.runModal() == .alertFirstButtonReturn else { return .terminateCancel }
        store.pauseForQuit { NSApp.reply(toApplicationShouldTerminate: true) }
        return .terminateLater
    }
}

// MARK: - settings window

struct SettingsView: View {
    @EnvironmentObject var store: JobStore

    var body: some View {
        Form {
            Section {
                Picker("Download at the same time", selection: $store.maxConcurrent) {
                    ForEach([1, 2, 3, 4, 5, 6, 8], id: \.self) { Text("\($0)").tag($0) }
                    Text("No limit").tag(0)
                }
                .help("Downloads beyond this wait in the queue and start as others finish")
                Picker("Speed limit", selection: Binding(
                    get: { store.speedLimit },
                    set: { bytes in
                        if let choice = JobStore.speedChoices.first(where: { $0.bytes == bytes }) {
                            store.setSpeedLimit(choice)
                        }
                    })) {
                    ForEach(JobStore.speedChoices, id: \.bytes) { Text($0.label).tag($0.bytes) }
                    if !JobStore.speedChoices.contains(where: { $0.bytes == store.speedLimit }) {
                        Text(store.speedLimitLabel).tag(store.speedLimit)
                    }
                }
                .help("Shared by every running download")
            }
            Section {
                LabeledContent("Save downloads in") {
                    HStack(spacing: 8) {
                        Text(tildePath(store.downloadRoot))
                            .lineLimit(1).truncationMode(.middle)
                            .foregroundColor(.secondary)
                        Button("Change…") {
                            if let path = Session.shared.chooseFolder(startingAt: store.downloadRoot) {
                                store.setDownloadRoot(path)
                            }
                        }
                    }
                }
                Text("Files are sorted into video, audio, image… folders inside it.")
                    .font(.caption).foregroundColor(.secondary)
            }
            Section {
                Toggle("Ask for the file name before each download", isOn: $store.askBeforeDownload)
                Toggle("Notify when a download finishes", isOn: $store.notifyOnFinish)
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
    }
}
