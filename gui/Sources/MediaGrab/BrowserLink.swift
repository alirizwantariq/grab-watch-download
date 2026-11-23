import Foundation

/// Links Chromium browsers to the engine: writes the native-messaging host
/// launcher + manifest the MediaGrab extension talks to (the same thing
/// `cli/script/install_chrome_host.sh` does, but pointing at the engine
/// inside this app so it keeps working wherever the app lives).
enum BrowserLink {
    static let hostName = "com.alirizwantariq.mediagrab"
    /// fixed by the "key" in the extension's manifest.json
    static let extensionID = "hgebmpanpoiebnbnmabafnpmkkmdgdmn"
    private static let marker = "# managed-by: MediaGrab.app"

    struct Browser: Identifiable {
        let id: String              // display name
        let supportDir: URL         // …/Application Support/<vendor>
        var hostsDir: URL { supportDir.appendingPathComponent("NativeMessagingHosts") }
        var manifest: URL { hostsDir.appendingPathComponent(BrowserLink.hostName + ".json") }
        var installed: Bool { FileManager.default.fileExists(atPath: supportDir.path) }
    }

    static var browsers: [Browser] {
        let base = FileManager.default
            .urls(for: .applicationSupportDirectory, in: .userDomainMask)[0]
        let known: [(String, String)] = [
            ("Google Chrome", "Google/Chrome"),
            ("Brave", "BraveSoftware/Brave-Browser"),
            ("Microsoft Edge", "Microsoft Edge"),
            ("Arc", "Arc/User Data"),
            ("Vivaldi", "Vivaldi"),
            ("Chromium", "Chromium"),
        ]
        return known.map { Browser(id: $0.0, supportDir: base.appendingPathComponent($0.1)) }
    }

    static var launcherURL: URL {
        Engine.configDirectory.appendingPathComponent("mgd-native-host")
    }

    enum State: Equatable {
        case linked                 // manifest + launcher point at a working engine
        case stale(String)          // registered, but something it needs is gone
        case notLinked
    }

    static func state(of browser: Browser) -> State {
        let fm = FileManager.default
        guard let data = fm.contents(atPath: browser.manifest.path),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let path = obj["path"] as? String else {
            return .notLinked
        }
        guard fm.isExecutableFile(atPath: path) else {
            return .stale("its launcher is missing")
        }
        if let engine = engineInLauncher(at: path), !fm.fileExists(atPath: engine) {
            return .stale("the engine it points to has moved")
        }
        let origins = obj["allowed_origins"] as? [String] ?? []
        if !origins.contains("chrome-extension://\(extensionID)/") {
            return .stale("it allows a different extension")
        }
        return .linked
    }

    /// The `…/bin/mgd` path a launcher script runs.
    static func engineInLauncher(at path: String) -> String? {
        guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        for line in text.split(separator: "\n") where line.contains(" native-host") {
            // "$PY" "/path/to/bin/mgd" native-host …
            let parts = line.components(separatedBy: "\"")
            if let engine = parts.first(where: { $0.hasSuffix("/mgd") }) { return engine }
        }
        return nil
    }

    private static var launcherIsOurs: Bool {
        (try? String(contentsOf: launcherURL, encoding: .utf8))?.contains(marker) ?? false
    }

    /// Write the launcher + a manifest for every installed browser.
    /// Returns the browsers that are linked afterwards.
    @discardableResult
    static func connect() throws -> [String] {
        let fm = FileManager.default
        let engine = Engine.launcher
        guard engine.hasPrefix("/"), fm.fileExists(atPath: engine) else {
            throw NSError(domain: "MediaGrab", code: 1, userInfo: [NSLocalizedDescriptionKey:
                "The download engine could not be located (\(engine))."])
        }
        try fm.createDirectory(at: Engine.configDirectory, withIntermediateDirectories: true)
        let script = """
        #!/bin/sh
        # MediaGrab native-messaging launcher
        \(marker)
        # Logs every spawn/exit so browser-side failures are diagnosable.
        DIR="${XDG_CONFIG_HOME:-$HOME/.config}/mgd"
        LOG="$DIR/native-host.log"
        mkdir -p "$DIR" 2>/dev/null
        PATH="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
        export PATH
        PY="/opt/homebrew/bin/python3"
        [ -x "$PY" ] || PY="/usr/local/bin/python3"
        [ -x "$PY" ] || PY="/usr/bin/python3"
        echo "$(date '+%F %T') spawn pid=$$ py=$PY" >> "$LOG"
        "$PY" "\(engine)" native-host 2>>"$LOG"
        rc=$?
        echo "$(date '+%F %T') exit pid=$$ rc=$rc" >> "$LOG"
        exit $rc

        """
        try script.write(to: launcherURL, atomically: true, encoding: .utf8)
        try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: launcherURL.path)

        let manifest: [String: Any] = [
            "name": hostName,
            "description": "MediaGrab download integration (mgd)",
            "path": launcherURL.path,
            "type": "stdio",
            "allowed_origins": ["chrome-extension://\(extensionID)/"],
        ]
        let data = try JSONSerialization.data(withJSONObject: manifest,
                                              options: [.prettyPrinted, .sortedKeys])
        var linked: [String] = []
        for browser in browsers where browser.installed {
            try fm.createDirectory(at: browser.hostsDir, withIntermediateDirectories: true)
            try data.write(to: browser.manifest, options: .atomic)
            linked.append(browser.id)
        }
        return linked
    }

    /// A launcher this app wrote earlier keeps pointing at where the app
    /// was back then; fix it quietly when the app has moved. Launchers set
    /// up by hand (the install script) are left alone.
    static func repairIfMoved() {
        guard launcherIsOurs, Engine.bundledLauncher != nil,
              let current = engineInLauncher(at: launcherURL.path),
              current != Engine.launcher else { return }
        _ = try? connect()
    }

    /// A copy of the bundled extension outside the app bundle, so the folder
    /// the browser loads from survives app updates. Returns that folder.
    static func exportExtension() throws -> URL {
        guard let source = Engine.bundledExtension else {
            throw NSError(domain: "MediaGrab", code: 2, userInfo: [NSLocalizedDescriptionKey:
                "This build does not include the browser extension."])
        }
        let fm = FileManager.default
        let target = JobStore.dataDirectory.appendingPathComponent("chrome-extension",
                                                                   isDirectory: true)
        if fm.fileExists(atPath: target.path) {
            try fm.removeItem(at: target)
        }
        try fm.copyItem(at: source, to: target)
        return target
    }

    /// Keep an already-exported copy in step with the app (new versions
    /// reach the browser on its next restart).
    static func refreshExportedExtension() {
        let target = JobStore.dataDirectory.appendingPathComponent("chrome-extension")
        guard FileManager.default.fileExists(atPath: target.path),
              let source = Engine.bundledExtension else { return }
        func version(_ dir: URL) -> String? {
            guard let data = try? Data(contentsOf: dir.appendingPathComponent("manifest.json")),
                  let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return nil }
            return obj["version"] as? String
        }
        if version(source) != version(target) {
            _ = try? exportExtension()
        }
    }
}
