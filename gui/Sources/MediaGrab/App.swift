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
                Button("Import Links from File…") { session.chooseLinkFile() }
                    .keyboardShortcut("i", modifiers: [.command, .shift])
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

        MenuBarExtra(isInserted: menuBarShown) {
            MenuBarContent()
                .environmentObject(store)
                .environmentObject(session)
        } label: {
            MenuBarLabel(store: store)
        }
        .menuBarExtraStyle(.window)
    }

    /// The icon follows the setting (and removing it from the menu bar by
    /// ⌘-dragging turns the setting off). Test runs keep out of the real
    /// menu bar unless a flow asks for it.
    private var menuBarShown: Binding<Bool> {
        #if DEBUG
        if UITestHooks.active && ProcessInfo.processInfo.environment["MEDIAGRAB_UITEST_MENUBAR"] == nil {
            return .constant(false)
        }
        #endif
        // write only real changes: the menu bar writes the value back as it
        // sets the icon up, and an unchanged write would still republish
        // the store and rebuild the scenes - endlessly
        let store = store
        return Binding(get: { store.showMenuBarIcon },
                       set: { if store.showMenuBarIcon != $0 { store.showMenuBarIcon = $0 } })
    }
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    func applicationDidFinishLaunching(_ notification: Notification) {
        #if DEBUG
        if UITestHooks.active {          // scripted run: no prompts, no focus
            JobStore.shared.activate()
            Session.shared.updateClipboardWatch()
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
        Session.shared.updateClipboardWatch()
        BrowserLink.repairIfMoved()
        BrowserLink.refreshExportedExtension()
    }

    /// Closing the window quits the app - unless downloads are still going
    /// (they keep running with the dock badge showing their count) or the
    /// menu bar icon is on (the app stays there).
    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        let store = JobStore.shared
        return !store.hasWorkInFlight && !store.showMenuBarIcon && store.whenDone == .nothing
            && store.startAt == nil
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
                Picker("Make videos play smoothly on this Mac", selection: Binding(
                    get: { store.macReadyMode },
                    set: { store.setMacReadyMode($0) })) {
                    Text("Convert when needed").tag("convert")
                    Text("Only fix the format").tag("fix")
                    Text("Off").tag("off")
                }
                Text(store.macReadyMode == "convert"
                     ? "Videos QuickTime cannot open, or that would play choppy (VP9, AV1, 10-bit H.264…), are converted with the Mac's hardware encoder right after downloading. Files that already play well are left untouched."
                     : store.macReadyMode == "fix"
                     ? "Only quick, lossless fixes (container, audio). Videos that would need re-encoding are left as they are."
                     : "Downloads are kept exactly as they arrive.")
                    .font(.caption).foregroundColor(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Section {
                Toggle("Ask for the file name before each download", isOn: $store.askBeforeDownload)
                Toggle("Notify when a download finishes", isOn: $store.notifyOnFinish)
                Toggle("Keep the Mac awake while downloading", isOn: $store.keepAwake)
                    .help("The display can still sleep; the Mac does not go to sleep until downloads are done")
                Toggle("Watch the clipboard for links", isOn: $store.watchClipboard)
                    .help("When you copy a link in another app, MediaGrab offers to download it")
                if store.watchClipboard {
                    Toggle("Download copied links without asking", isOn: $store.autoDownloadClipboard)
                        .padding(.leading, 16)
                    Text("macOS asks once before MediaGrab may read what other apps copy. To skip "
                         + "the question, allow MediaGrab in System Settings › Privacy & Security › "
                         + "Paste from Other Apps.")
                        .font(.caption).foregroundColor(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
                Toggle("Show MediaGrab in the menu bar", isOn: $store.showMenuBarIcon)
                    .help("Progress at a glance, pause/resume and Paste Link from the menu bar; closing the window keeps MediaGrab running there")
                Toggle("Retry by itself after connection problems", isOn: $store.autoRetry)
                    .help("Downloads that fail because the connection dropped continue on their own: a few times over the next minutes, and as soon as the Mac is back online or wakes up")
            }
        }
        .formStyle(.grouped)
        .frame(width: 480)
        .fixedSize(horizontal: false, vertical: true)
    }
}
