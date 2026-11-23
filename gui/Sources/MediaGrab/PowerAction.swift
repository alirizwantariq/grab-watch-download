import AppKit
import Foundation

/// Quit / sleep / shut down once every download has finished.
enum PowerAction {
    /// The command that sleeps or shuts down the Mac (nil: quit, nothing).
    static func command(for action: JobStore.WhenDone) -> [String]? {
        switch action {
        case .sleep:
            return ["/usr/bin/pmset", "sleepnow"]
        case .shutDown:
            // System Events asks apps to save their documents first; macOS
            // asks once whether MediaGrab may control System Events
            return ["/usr/bin/osascript", "-e", "tell application \"System Events\" to shut down"]
        case .quit, .nothing:
            return nil
        }
    }

    static func perform(_ action: JobStore.WhenDone, onProblem: @escaping (String) -> Void) {
        let command = command(for: action)
        // tests record what would happen instead of doing it
        if let log = ProcessInfo.processInfo.environment["MEDIAGRAB_POWER_LOG"], !log.isEmpty {
            let line = action.rawValue + " " + (command ?? []).joined(separator: " ") + "\n"
            if let handle = FileHandle(forWritingAtPath: log) {
                handle.seekToEndOfFile()
                handle.write(Data(line.utf8))
                try? handle.close()
            } else {
                try? line.write(toFile: log, atomically: true, encoding: .utf8)
            }
            return
        }
        #if DEBUG
        if UITestHooks.active { return }      // never the machine running the tests
        #endif
        guard let command else {
            if action == .quit { NSApp.terminate(nil) }
            return
        }
        let process = Process()
        process.executableURL = URL(fileURLWithPath: command[0])
        process.arguments = Array(command.dropFirst())
        let errors = Pipe()
        process.standardError = errors
        process.standardOutput = FileHandle.nullDevice
        process.terminationHandler = { p in
            guard p.terminationStatus != 0 else { return }
            let text = String(decoding: errors.fileHandleForReading.readDataToEndOfFile(), as: UTF8.self)
            let denied = text.contains("-1743") || text.lowercased().contains("not allowed")
            DispatchQueue.main.async {
                onProblem(denied
                    ? "macOS did not let MediaGrab shut down the Mac — allow it in System Settings › "
                      + "Privacy & Security › Automation › MediaGrab › System Events"
                    : "Could not \(action == .sleep ? "put the Mac to sleep" : "shut down the Mac"): "
                      + text.trimmingCharacters(in: .whitespacesAndNewlines))
            }
        }
        do {
            try process.run()
        } catch {
            onProblem("Could not \(action.label.lowercased()): \(error.localizedDescription)")
        }
    }
}
