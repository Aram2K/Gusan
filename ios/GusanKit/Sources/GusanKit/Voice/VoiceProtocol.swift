import Foundation
import CoreFoundation

public enum VoiceAction: Hashable, Codable {
    case transcribeOnly, smart(SmartActionMode)
    public static var all: [VoiceAction] { [.transcribeOnly] + SmartActionMode.allCases.map { .smart($0) } }
    public var label: String { if case .smart(let mode) = self { return mode.label }; return "Transcribe only" }
    public var rawValue: String { if case .smart(let mode) = self { return mode.rawValue }; return "transcribe" }
    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        if value == "transcribe" { self = .transcribeOnly }
        else if let mode = SmartActionMode(rawValue: value) { self = .smart(mode) }
        else { throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unknown voice action")) }
    }
    public func encode(to encoder: Encoder) throws { var container = encoder.singleValueContainer(); try container.encode(rawValue) }
}

public final class VoiceSettings {
    private let defaults: UserDefaults
    public static let languages: [(code: String, name: String)] = [("auto", "Detect automatically"), ("en", "English"), ("hy", "Armenian"), ("ru", "Russian"), ("fr", "French"), ("de", "German"), ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("ar", "Arabic")]
    public static let idleOptions = [1, 5, 15, 30, 60]
    public init(group: String?) { defaults = group.flatMap(UserDefaults.init(suiteName:)) ?? .standard }
    public var language: String {
        get { let code = defaults.string(forKey: "voiceLanguage") ?? "auto"; return Self.languages.contains { $0.code == code } ? code : "auto" }
        set { defaults.set(newValue, forKey: "voiceLanguage") }
    }
    public var defaultAction: VoiceAction {
        get { defaults.data(forKey: "voiceDefaultAction").flatMap { try? JSONDecoder().decode(VoiceAction.self, from: $0) } ?? .transcribeOnly }
        set { defaults.set(try? JSONEncoder().encode(newValue), forKey: "voiceDefaultAction") }
    }
    public var idleMinutes: Int {
        get { let value = defaults.integer(forKey: "voiceIdleMinutes"); return Self.idleOptions.contains(value) ? value : 5 }
        set { defaults.set(newValue, forKey: "voiceIdleMinutes") }
    }
    public var autoReturn: Bool {
        get { defaults.object(forKey: "voiceAutoReturn") as? Bool ?? true }
        set { defaults.set(newValue, forKey: "voiceAutoReturn") }
    }
}

public struct VoiceContext: Codable, Equatable {
    public var selected, before, after: String
    public var documentID: String?
    public init(selected: String = "", before: String = "", after: String = "", documentID: String? = nil) { self.selected = selected; self.before = before; self.after = after; self.documentID = documentID }
}
public struct VoiceCommand: Codable, Equatable {
    public enum Kind: String, Codable { case start, stop, cancel, endSession }
    public var id: UUID
    public var kind: Kind
    public var requestID: UUID
    public var context: VoiceContext?
    public var sentAt: Date
    public init(id: UUID = UUID(), kind: Kind, requestID: UUID, context: VoiceContext? = nil, sentAt: Date = Date()) { self.id = id; self.kind = kind; self.requestID = requestID; self.context = context; self.sentAt = sentAt }
}
public enum VoicePhase: String, Codable { case off, ready, recording, transcribing, thinking, done, failed }
public struct VoiceStatus: Codable, Equatable {
    public var phase: VoicePhase
    public var requestID: UUID?
    public var heartbeat: Date
    public var sessionEndsAt: Date?
    public var level: Float
    public var message, result: String?
    public var replacesSelection: Bool
    public init(phase: VoicePhase = .off, requestID: UUID? = nil, heartbeat: Date = Date(), sessionEndsAt: Date? = nil, level: Float = 0, message: String? = nil, result: String? = nil, replacesSelection: Bool = false) {
        self.phase = phase; self.requestID = requestID; self.heartbeat = heartbeat; self.sessionEndsAt = sessionEndsAt; self.level = level; self.message = message; self.result = result; self.replacesSelection = replacesSelection
    }
    public func isAlive(now: Date = Date()) -> Bool { phase != .off && now.timeIntervalSince(heartbeat) < 3 }
}

public struct VoiceChannel {
    public enum Notification: String { case command, status }
    public let container: URL?
    public let group: String
    public init(container: URL?, group: String = "group.com.aram2k.gusan") { self.container = container; self.group = group }
    public var available: Bool { container != nil }
    private func file(_ name: String) -> URL? { container?.appendingPathComponent("voice", isDirectory: true).appendingPathComponent(name + ".json") }
    private func read<T: Decodable>(_ name: String, as: T.Type) -> T? { file(name).flatMap { try? Data(contentsOf: $0) }.flatMap { try? JSONDecoder().decode(T.self, from: $0) } }
    private func write<T: Encodable>(_ value: T, name: String) throws {
        guard let url = file(name) else { throw VoiceError.notShared }
        try FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        #if os(iOS)
        let options: Data.WritingOptions = [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        #else
        let options: Data.WritingOptions = .atomic
        #endif
        try JSONEncoder().encode(value).write(to: url, options: options)
    }
    public func readStatus() -> VoiceStatus? { read("status", as: VoiceStatus.self) }
    public func readCommand() -> VoiceCommand? { read("command", as: VoiceCommand.self) }
    public func readKeyboardState() -> KeyboardVoiceState? { read("keyboard", as: KeyboardVoiceState.self) }
    public func write(status: VoiceStatus) throws { try write(status, name: "status") }
    public func write(command: VoiceCommand) throws { try write(command, name: "command") }
    public func write(keyboard: KeyboardVoiceState) throws { try write(keyboard, name: "keyboard") }
    public func name(_ notification: Notification) -> String { "gusan.voice.\(notification.rawValue).\(group)" }
    public func post(_ notification: Notification) { CFNotificationCenterPostNotification(CFNotificationCenterGetDarwinNotifyCenter(), CFNotificationName(name(notification) as CFString), nil, nil, true) }
    public func observe(_ notification: Notification, handler: @escaping () -> Void) -> DarwinObserver { DarwinObserver(name: name(notification), handler: handler) }
}

public final class DarwinObserver {
    private let handler: () -> Void
    public init(name: String, handler: @escaping () -> Void) {
        self.handler = handler
        CFNotificationCenterAddObserver(CFNotificationCenterGetDarwinNotifyCenter(), Unmanaged.passUnretained(self).toOpaque(), { _, observer, _, _, _ in
            guard let observer else { return }
            let instance = Unmanaged<DarwinObserver>.fromOpaque(observer).takeUnretainedValue()
            let handler = instance.handler
            DispatchQueue.main.async { handler() }
        }, name as CFString, nil, .deliverImmediately)
    }
    deinit { CFNotificationCenterRemoveEveryObserver(CFNotificationCenterGetDarwinNotifyCenter(), Unmanaged.passUnretained(self).toOpaque()) }
}
