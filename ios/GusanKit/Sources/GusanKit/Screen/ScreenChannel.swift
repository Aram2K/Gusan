import Foundation
import CoreFoundation

/// Heartbeat the broadcast extension publishes while "Gusan Screen" runs.
public struct ScreenStatus: Codable, Equatable {
    public var startedAt: Date
    public var heartbeat: Date
    public var idleDeadline: Date
    public var width: Int
    public var height: Int
    public var requests: Int
    public init(startedAt: Date, heartbeat: Date = Date(), idleDeadline: Date, width: Int = 0, height: Int = 0, requests: Int = 0) {
        self.startedAt = startedAt; self.heartbeat = heartbeat; self.idleDeadline = idleDeadline; self.width = width; self.height = height; self.requests = requests
    }
    /// Heartbeats come every 2 s; a missed few mean the broadcast is gone.
    public static let liveWindow: TimeInterval = 7
    public func isLive(now: Date = Date()) -> Bool { now.timeIntervalSince(heartbeat) < Self.liveWindow && now.timeIntervalSince(heartbeat) > -60 }
    public var hasFrame: Bool { width > 0 && height > 0 }
}

/// The keyboard asks for the current frame; nothing is captured without it.
public struct ScreenRequest: Codable, Equatable {
    public let id: UUID
    public let issuedAt: Date
    public init(id: UUID = UUID(), issuedAt: Date = Date()) { self.id = id; self.issuedAt = issuedAt }
}

/// Keyboard ↔ broadcast extension transport. Every packet is written to every
/// store that works (App Group file, team pasteboard) and read back newest-first,
/// so it keeps working when Sideloadly breaks the App Group. Frames are removed
/// as soon as the keyboard takes them and expire on their own otherwise.
public struct ScreenChannel {
    public enum Notification: String { case status, request, frame }
    struct Packet<Value: Codable>: Codable { let issuedAt: Date; let value: Value }
    public static let keys = ["status", "request", "frame"]
    public static let lifetimes: [String: TimeInterval] = ["status": 60, "request": 30, "frame": 45]
    let stores: [SessionBackend]
    public let group: String
    private let now: () -> Date
    public init(container: URL?, group: String = "com.aram2k.gusan", pasteboardAllowed: @escaping () -> Bool = { true }) {
        let directory = container?.appendingPathComponent("screen", isDirectory: true)
        if let directory { try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        self.init(stores: [FileSessionBackend(container: directory), NamedSessionPasteboard(namespace: "com.aram2k.gusan.screen", lifetime: 60, accessAllowed: pasteboardAllowed)], group: group)
    }
    init(stores: [SessionBackend], group: String = "com.aram2k.gusan", now: @escaping () -> Date = Date.init) {
        self.stores = stores; self.group = group; self.now = now
    }
    public var available: Bool {
        stores.contains { store in
            let readable = store.read("probe").readable
            let writable = store.write(Data([0]), key: "probe")
            store.clear("probe")
            return readable && writable
        }
    }
    public var diagnosticsDescription: String {
        let names = stores.filter { $0.write(Data([0]), key: "probe") }.map { store -> String in store.clear("probe"); return store.name }
        return names.isEmpty ? "Unavailable" : names.joined(separator: " + ")
    }
    func read<T: Codable>(_ key: String, as: T.Type) -> T? {
        let lifetime = Self.lifetimes[key] ?? 60
        var newest: Packet<T>?
        for store in stores {
            guard let data = store.read(key).data, let packet = try? JSONDecoder().decode(Packet<T>.self, from: data) else { continue }
            let age = now().timeIntervalSince(packet.issuedAt)
            guard age < lifetime, age > -60 else { store.clear(key); continue }
            if newest == nil || packet.issuedAt > newest!.issuedAt { newest = packet }
        }
        return newest?.value
    }
    @discardableResult func write<T: Codable>(_ value: T, key: String) -> Bool {
        guard let data = try? JSONEncoder().encode(Packet(issuedAt: now(), value: value)) else { return false }
        var saved = false
        for store in stores where store.write(data, key: key) { saved = true }
        return saved
    }
    func remove(_ key: String) { for store in stores { store.clear(key) } }
    public func clear() { for key in Self.keys { remove(key) } }

    public func readStatus() -> ScreenStatus? { read("status", as: ScreenStatus.self) }
    public func isLive() -> Bool { readStatus()?.isLive(now: now()) == true }
    @discardableResult public func write(status: ScreenStatus) -> Bool { write(status, key: "status") }
    public func clearStatus() { remove("status") }

    public func readRequest() -> ScreenRequest? { read("request", as: ScreenRequest.self) }
    public func consumeRequest() -> ScreenRequest? { let request = readRequest(); if request != nil { remove("request") }; return request }
    @discardableResult public func write(request: ScreenRequest) -> Bool { write(request, key: "request") }

    public func readFrame() -> ScreenFrame? { read("frame", as: ScreenFrame.self) }
    /// The frame answering `requestID`, removed from every store once taken.
    public func consumeFrame(requestID: UUID) -> ScreenFrame? {
        guard let frame = readFrame(), frame.requestID == requestID else { return nil }
        remove("frame"); return frame
    }
    @discardableResult public func write(frame: ScreenFrame) -> Bool { write(frame, key: "frame") }
    public func clearFrame() { remove("frame") }

    public func name(_ notification: Notification) -> String { "gusan.screen.\(notification.rawValue).\(group)" }
    public func post(_ notification: Notification) {
        CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(), CFNotificationName(name(notification) as CFString), nil, nil, true)
    }
    public func observe(_ notification: Notification, handler: @escaping () -> Void) -> DarwinObserver { DarwinObserver(name: name(notification), handler: handler) }
}

/// Something that can produce the current screen frame for a request.
public protocol ScreenFrameProvider {
    func frame(timeout: TimeInterval) async throws -> ScreenFrame
}

/// The keyboard's provider: posts a request and waits for the extension's answer.
public struct ScreenChannelFrameProvider: ScreenFrameProvider {
    public let channel: ScreenChannel
    public let pollInterval: TimeInterval
    public init(channel: ScreenChannel, pollInterval: TimeInterval = 0.2) { self.channel = channel; self.pollInterval = pollInterval }
    public func frame(timeout: TimeInterval = 5) async throws -> ScreenFrame {
        guard channel.isLive() else { throw ScreenError.notLive }
        let request = ScreenRequest()
        channel.clearFrame()
        guard channel.write(request: request) else { throw ScreenError.channelUnavailable }
        channel.post(.request)
        let deadline = Date().addingTimeInterval(timeout)
        while Date() < deadline {
            try Task.checkCancellation()
            if let frame = channel.consumeFrame(requestID: request.id) { return frame }
            try await Task.sleep(nanoseconds: UInt64(pollInterval * 1_000_000_000))
        }
        _ = channel.consumeRequest()
        throw channel.isLive() ? ScreenError.noFrame : ScreenError.notLive
    }
}

public enum ScreenError: Error, LocalizedError, Equatable {
    case notLive, channelUnavailable, noFrame, secureField, notExplained
    public var errorDescription: String? {
        switch self {
        case .notLive: return "Gusan Screen isn't running. Start it in the Gusan app or from Control Center."
        case .channelUnavailable: return "The keyboard can't reach Gusan Screen. Check Diagnostics in the Gusan app."
        case .noFrame: return "Gusan Screen didn't send a picture in time. Try again."
        case .secureField: return "Gusan never looks at the screen while you type in a secure field."
        case .notExplained: return "Open Gusan Screen in the Gusan app once to read what is shared."
        }
    }
}
