import Foundation

public struct CalendarEventRecord: Equatable, Sendable {
    public var start: Date
    public var end: Date
    public var isAllDay: Bool
    public var isFree: Bool
    public var isDeclined: Bool
    public var isCancelled: Bool
    public var title: String?
    public init(start: Date, end: Date, isAllDay: Bool = false, isFree: Bool = false, isDeclined: Bool = false, isCancelled: Bool = false, title: String? = nil) {
        self.start = start; self.end = end; self.isAllDay = isAllDay; self.isFree = isFree
        self.isDeclined = isDeclined; self.isCancelled = isCancelled; self.title = title
    }
}

public enum CalendarEventFilter {
    public static func blocks(from events: [CalendarEventRecord], windowStart: Date, windowEnd: Date, includeTitles: Bool) -> [BusyBlock] {
        guard windowStart < windowEnd else { return [] }
        return events.compactMap { event in
            let start = max(event.start, windowStart), end = min(event.end, windowEnd)
            guard !event.isFree, !event.isDeclined, !event.isCancelled, start < end else { return nil }
            return BusyBlock(start: start, end: end, isAllDay: event.isAllDay, title: includeTitles ? event.title : nil)
        }.sorted { $0.start < $1.start }
    }
}
