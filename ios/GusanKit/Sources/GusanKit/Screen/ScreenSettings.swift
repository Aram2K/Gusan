import Foundation

/// Gusan Screen preferences, shared like `SettingsStore`: the App Group
/// defaults when they are shared, mirrored on a team pasteboard otherwise.
public final class ScreenSettings {
    /// The explanation is shown again after this long, and after every app update.
    public static let consentValidity: TimeInterval = 30 * 24 * 3600
    private let defaults: UserDefaults
    private let shared: SessionBackend
    private let version: String
    private let now: () -> Date
    private let lock = NSLock()
    private let keys = ["screenIdleMinutes", "screenExplainedAt", "screenExplainedVersion", "screenArmReminderShown", "screenLastStopAt", "screenLastStopMinutes"]
    public init(group: String?, pasteboardAccessAllowed: @escaping () -> Bool = { true }, version: String = ScreenSettings.appVersion) {
        defaults = group.flatMap(UserDefaults.init(suiteName:)) ?? .standard
        shared = NamedSessionPasteboard(namespace: "com.aram2k.gusan.screen.settings", accessAllowed: pasteboardAccessAllowed)
        self.version = version; now = Date.init
    }
    init(defaults: UserDefaults, shared: SessionBackend, version: String = "test", now: @escaping () -> Date = Date.init) {
        self.defaults = defaults; self.shared = shared; self.version = version; self.now = now
    }
    /// The app's marketing version; extensions share it with the app, so a consent given in one counts in all.
    public static var appVersion: String { (Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String) ?? "0" }
    private func synchronized<T>(_ body: () -> T) -> T {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.synchronized(body) } }
        #endif
        lock.lock(); defer { lock.unlock() }; return body()
    }
    private func loadShared() -> [String: Any] {
        let cached = shared.read("session").data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        if let timestamp = cached["updatedAt"] as? Double, timestamp > defaults.double(forKey: "screenSettingsUpdatedAt") {
            for key in keys { if let value = cached[key] { defaults.set(value, forKey: key) } else { defaults.removeObject(forKey: key) } }
            defaults.set(timestamp, forKey: "screenSettingsUpdatedAt")
        }
        return cached
    }
    private func preference(_ key: String) -> Any? {
        synchronized {
            let cached = loadShared()
            if (cached["updatedAt"] as? Double ?? 0) > defaults.double(forKey: "screenSettingsUpdatedAt") { return cached[key] }
            return defaults.object(forKey: key)
        }
    }
    private func setPreferences(_ changes: [String: Any?]) {
        synchronized {
            _ = loadShared()
            var values = Dictionary(uniqueKeysWithValues: keys.compactMap { key in defaults.object(forKey: key).map { (key, $0) } })
            for (key, value) in changes { values[key] = value }
            let timestamp = max(now().timeIntervalSince1970, defaults.double(forKey: "screenSettingsUpdatedAt") + 0.001)
            for key in keys { if let value = values[key] { defaults.set(value, forKey: key) } else { defaults.removeObject(forKey: key) } }
            defaults.set(timestamp, forKey: "screenSettingsUpdatedAt")
            var payload = values; payload["updatedAt"] = timestamp
            if let data = try? JSONSerialization.data(withJSONObject: payload) { _ = shared.write(data, key: "session") }
        }
    }
    private func setPreference(_ value: Any, key: String) { setPreferences([key: value]) }
    /// Minutes without a frame request before the broadcast stops itself.
    public var idleMinutes: Int {
        get { let value = preference("screenIdleMinutes") as? Int ?? ScreenIdleTimer.defaultMinutes; return ScreenIdleTimer.minuteOptions.contains(value) ? value : ScreenIdleTimer.defaultMinutes }
        set { setPreference(ScreenIdleTimer.minuteOptions.contains(newValue) ? newValue : ScreenIdleTimer.defaultMinutes, key: "screenIdleMinutes") }
    }
    /// When the user last read, in the app, what Gusan Screen shares.
    public var explainedAt: Date? { (preference("screenExplainedAt") as? Double).map(Date.init(timeIntervalSince1970:)) }
    /// The explanation counts for 30 days and for the app version it was read in.
    /// The keyboard offers the screen only while this is true, even for Control Center starts.
    public var explained: Bool {
        get {
            guard let explainedAt, preference("screenExplainedVersion") as? String == version else { return false }
            let age = now().timeIntervalSince(explainedAt)
            return age > -60 && age < Self.consentValidity
        }
        set {
            if newValue { setPreferences(["screenExplainedAt": now().timeIntervalSince1970, "screenExplainedVersion": version]) }
            else { setPreferences(["screenExplainedAt": nil, "screenExplainedVersion": nil]) }
        }
    }
    /// The keyboard reminded the user once that arming the chip sends a picture of the screen.
    public var armReminderShown: Bool {
        get { preference("screenArmReminderShown") as? Bool ?? false }
        set { setPreference(newValue, key: "screenArmReminderShown") }
    }
    /// The last time the broadcast stopped itself for lack of questions, for the Gusan Screen page.
    public var lastIdleStop: (at: Date, minutes: Int)? {
        guard let at = preference("screenLastStopAt") as? Double, let minutes = preference("screenLastStopMinutes") as? Int else { return nil }
        return (Date(timeIntervalSince1970: at), minutes)
    }
    public func recordIdleStop(minutes: Int) { setPreferences(["screenLastStopAt": now().timeIntervalSince1970, "screenLastStopMinutes": minutes]) }
    public func clearIdleStop() { setPreferences(["screenLastStopAt": nil, "screenLastStopMinutes": nil]) }
}
