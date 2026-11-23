import Foundation

/// Where an installed `mgd` may live when the app is not running from a
/// bundle that carries its own engine (e.g. `swift run` during development).
/// Engine.launcher checks MEDIAGRAB_MGD and the bundled copy first.
enum MGD {
    static let candidates = ["/opt/homebrew/bin/mgd", "/usr/local/bin/mgd"]
}
