import Foundation

public struct BusyBlock: Codable, Equatable, Sendable {
    public var start: Date
    public var end: Date
    public var isAllDay: Bool
    public var title: String?
    public init(start: Date, end: Date, isAllDay: Bool = false, title: String? = nil) {
        self.start = start; self.end = end; self.isAllDay = isAllDay; self.title = title
    }
}

public struct CalendarSnapshot: Codable, Equatable, Sendable {
    public var generatedAt: Date
    public var windowStart: Date
    public var windowEnd: Date
    public var timeZoneID: String
    public var sources: [String]
    public var blocks: [BusyBlock]
    public init(generatedAt: Date = Date(), windowStart: Date, windowEnd: Date, timeZoneID: String = TimeZone.current.identifier, sources: [String], blocks: [BusyBlock]) {
        self.generatedAt = generatedAt; self.windowStart = windowStart; self.windowEnd = windowEnd
        self.timeZoneID = timeZoneID; self.sources = sources; self.blocks = blocks
    }
}

public final class CalendarSnapshotStore {
    private let defaults: UserDefaults?
    private let key = "gusan.calendar.snapshot"
    public init(defaults: UserDefaults?) { self.defaults = defaults }
    public convenience init(group: String?) {
        self.init(defaults: group.flatMap(UserDefaults.init(suiteName:)) ?? .standard)
    }
    public var calendarIncludeTitles: Bool {
        get { defaults?.bool(forKey: "calendarIncludeTitles") ?? false }
        set {
            let saved = snapshot
            defaults?.set(newValue, forKey: "calendarIncludeTitles")
            if !newValue {
                if let saved { write(saved) }
                for source in ["iphone", "google"] {
                    if let value = sourceSnapshot(source) { save(value, key: sourceKey(source)) }
                }
            }
        }
    }
    public var calendarIPhoneEnabled: Bool {
        get { defaults?.bool(forKey: "calendarIPhoneEnabled") ?? false }
        set { setConnection("calendarIPhoneEnabled", enabled: newValue) }
    }
    public var calendarGoogleEnabled: Bool {
        get { defaults?.bool(forKey: "calendarGoogleEnabled") ?? false }
        set { setConnection("calendarGoogleEnabled", enabled: newValue) }
    }
    private func setConnection(_ key: String, enabled: Bool) {
        if defaults?.bool(forKey: key) != enabled {
            defaults?.removeObject(forKey: self.key)
            if !enabled { defaults?.removeObject(forKey: sourceKey(key == "calendarIPhoneEnabled" ? "iphone" : "google")) }
        }
        defaults?.set(enabled, forKey: key)
        rebuild()
    }
    public var snapshot: CalendarSnapshot? {
        guard let data = defaults?.data(forKey: key),
              var snapshot = try? JSONDecoder().decode(CalendarSnapshot.self, from: data) else { return nil }
        guard !snapshot.sources.isEmpty, snapshot.sources.allSatisfy({
            ($0 == "iphone" && calendarIPhoneEnabled) || ($0 == "google" && calendarGoogleEnabled)
        }) else { return nil }
        if !calendarIncludeTitles { snapshot.blocks = snapshot.blocks.map { BusyBlock(start: $0.start, end: $0.end, isAllDay: $0.isAllDay) } }
        return snapshot
    }
    public func write(_ snapshot: CalendarSnapshot) {
        save(snapshot, key: key)
    }
    private func save(_ snapshot: CalendarSnapshot, key: String) {
        var snapshot = snapshot
        if !calendarIncludeTitles { snapshot.blocks = snapshot.blocks.map { BusyBlock(start: $0.start, end: $0.end, isAllDay: $0.isAllDay) } }
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults?.set(data, forKey: key)
    }
    private func sourceKey(_ source: String) -> String { "gusan.calendar.source." + source }
    private func sourceSnapshot(_ source: String) -> CalendarSnapshot? {
        guard let data = defaults?.data(forKey: sourceKey(source)) else { return nil }
        return try? JSONDecoder().decode(CalendarSnapshot.self, from: data)
    }
    public func replaceSource(_ source: String, snapshot: CalendarSnapshot?) {
        guard ["iphone", "google"].contains(source) else { return }
        defaults?.removeObject(forKey: key)
        if let snapshot, snapshot.sources == [source] { save(snapshot, key: sourceKey(source)) }
        else { defaults?.removeObject(forKey: sourceKey(source)) }
        rebuild()
    }
    private func rebuild() {
        let snapshots = ["iphone", "google"].compactMap { source -> CalendarSnapshot? in
            guard source == "iphone" ? calendarIPhoneEnabled : calendarGoogleEnabled else { return nil }
            return sourceSnapshot(source)
        }
        guard let first = snapshots.first else { return }
        let combined = CalendarSnapshot(generatedAt: snapshots.map(\.generatedAt).min()!, windowStart: snapshots.map(\.windowStart).max()!,
                                        windowEnd: snapshots.map(\.windowEnd).min()!, timeZoneID: first.timeZoneID,
                                        sources: snapshots.flatMap(\.sources), blocks: snapshots.flatMap(\.blocks))
        write(combined)
    }
    public func clear() {
        for key in [key, sourceKey("iphone"), sourceKey("google"), "calendarIncludeTitles", "calendarIPhoneEnabled", "calendarGoogleEnabled"] { defaults?.removeObject(forKey: key) }
    }
}
