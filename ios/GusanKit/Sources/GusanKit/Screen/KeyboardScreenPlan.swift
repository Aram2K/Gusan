import Foundation

public enum KeyboardScreenIconState: Equatable {
    case off, needsBroadcast, live
    public static func resolve(enabled: Bool, live: Bool) -> Self {
        enabled ? (live ? .live : .needsBroadcast) : .off
    }
    public static func resolve(enabled: Bool, status: ScreenStatus?, now: Date = Date()) -> Self {
        resolve(enabled: enabled, live: status?.isLive(now: now) == true)
    }
    public var accessibilityValue: String {
        switch self {
        case .off: return "Off"
        case .needsBroadcast: return "Needs screen broadcast"
        case .live: return "Live"
        }
    }
}

public struct KeyboardScreenPlan {
    public static let unavailableMessage = "Screen unavailable — sent without it"
    public static let partialMessage = "Screen: only 1 frame"
    public let frames: [ScreenFrame]
    public let message: String?
    public static func shouldRequest(enabled: Bool, live: Bool, secure: Bool, fullAccess: Bool) -> Bool {
        enabled && live && !secure && fullAccess
    }
    public init(enabled: Bool, live: Bool, secure: Bool, fullAccess: Bool, start: ScreenFrame?, finish: ScreenFrame?) {
        if Self.shouldRequest(enabled: enabled, live: live, secure: secure, fullAccess: fullAccess) {
            var start = start, finish = finish
            start?.phase = .start; finish?.phase = .finish
            frames = [start, finish].compactMap { $0 }
        } else { frames = [] }
        if !enabled || secure || !fullAccess || frames.count == 2 { message = nil }
        else { message = frames.isEmpty ? Self.unavailableMessage : Self.partialMessage }
    }
}
