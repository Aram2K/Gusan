import Foundation

public struct BusyBlock: Codable, Equatable {
    public var start: Date
    public var end: Date
    public var isAllDay: Bool
    public var title: String?
    public init(start: Date, end: Date, isAllDay: Bool = false, title: String? = nil) {
        self.start = start; self.end = end; self.isAllDay = isAllDay; self.title = title
    }
}

public struct CalendarSnapshot: Codable, Equatable {
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
            if !newValue, let saved { write(saved) }
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
        if defaults?.bool(forKey: key) != enabled { defaults?.removeObject(forKey: self.key) }
        defaults?.set(enabled, forKey: key)
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
        var snapshot = snapshot
        if !calendarIncludeTitles { snapshot.blocks = snapshot.blocks.map { BusyBlock(start: $0.start, end: $0.end, isAllDay: $0.isAllDay) } }
        guard let data = try? JSONEncoder().encode(snapshot) else { return }
        defaults?.set(data, forKey: key)
    }
    public func clear() {
        for key in [key, "calendarIncludeTitles", "calendarIPhoneEnabled", "calendarGoogleEnabled"] { defaults?.removeObject(forKey: key) }
    }
}
