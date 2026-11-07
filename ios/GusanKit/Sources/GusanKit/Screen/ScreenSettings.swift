import Foundation

/// Gusan Screen preferences, shared like `SettingsStore`: the App Group
/// defaults when they are shared, mirrored on a team pasteboard otherwise.
public final class ScreenSettings {
    private let defaults: UserDefaults
    private let shared: SessionBackend
    private let lock = NSLock()
    private let keys = ["screenIdleMinutes", "screenExplained"]
    public init(group: String?, pasteboardAccessAllowed: @escaping () -> Bool = { true }) {
        defaults = group.flatMap(UserDefaults.init(suiteName:)) ?? .standard
        shared = NamedSessionPasteboard(namespace: "com.aram2k.gusan.screen.settings", accessAllowed: pasteboardAccessAllowed)
    }
    init(defaults: UserDefaults, shared: SessionBackend) { self.defaults = defaults; self.shared = shared }
    private func synchronized<T>(_ body: () -> T) -> T {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.synchronized(body) } }
        #endif
        lock.lock(); defer { lock.unlock() }; return body()
    }
    private func loadShared() -> [String: Any] {
        let cached = shared.read("session").data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        if let timestamp = cached["updatedAt"] as? Double, timestamp > defaults.double(forKey: "screenSettingsUpdatedAt") {
            for key in keys { if let value = cached[key] { defaults.set(value, forKey: key) } }
            defaults.set(timestamp, forKey: "screenSettingsUpdatedAt")
        }
        return cached
    }
    private func preference(_ key: String) -> Any? {
        synchronized {
            let cached = loadShared()
            if (cached["updatedAt"] as? Double ?? 0) > defaults.double(forKey: "screenSettingsUpdatedAt") { return cached[key] ?? defaults.object(forKey: key) }
            return defaults.object(forKey: key)
        }
    }
    private func setPreference(_ value: Any, key: String) {
        synchronized {
            _ = loadShared()
            var values = Dictionary(uniqueKeysWithValues: keys.compactMap { key in defaults.object(forKey: key).map { (key, $0) } })
            values[key] = value
            let timestamp = Date().timeIntervalSince1970
            for (key, value) in values { defaults.set(value, forKey: key) }
            defaults.set(timestamp, forKey: "screenSettingsUpdatedAt")
            values["updatedAt"] = timestamp
            if let data = try? JSONSerialization.data(withJSONObject: values) { _ = shared.write(data, key: "session") }
        }
    }
    /// Minutes without a frame request before the broadcast stops itself.
    public var idleMinutes: Int {
        get { let value = preference("screenIdleMinutes") as? Int ?? ScreenIdleTimer.defaultMinutes; return ScreenIdleTimer.minuteOptions.contains(value) ? value : ScreenIdleTimer.defaultMinutes }
        set { setPreference(ScreenIdleTimer.minuteOptions.contains(newValue) ? newValue : ScreenIdleTimer.defaultMinutes, key: "screenIdleMinutes") }
    }
    /// The user has read, in the app, what Gusan Screen shares. The keyboard
    /// offers the screen only after that, even for Control Center starts.
    public var explained: Bool {
        get { preference("screenExplained") as? Bool ?? false }
        set { setPreference(newValue, key: "screenExplained") }
    }
}
