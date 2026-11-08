import Foundation
import CoreFoundation

public enum VoiceAction: Hashable, Codable {
    case automatic, transcribeOnly, smart(SmartActionMode)
    public static var all: [VoiceAction] { [.automatic, .transcribeOnly] + SmartActionMode.allCases.map { .smart($0) } }
    public var label: String { if case .smart(let mode) = self { return mode.label }; return self == .automatic ? "Smart voice" : "Transcribe only" }
    public var rawValue: String { if case .smart(let mode) = self { return mode.rawValue }; return self == .automatic ? "voice_smart" : "transcribe" }
    public init(from decoder: Decoder) throws {
        let value = try decoder.singleValueContainer().decode(String.self)
        if value == "voice_smart" { self = .automatic }
        else if value == "transcribe" { self = .transcribeOnly }
        else if let mode = SmartActionMode(rawValue: value) { self = .smart(mode) }
        else { throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Unknown voice action")) }
    }
    public func encode(to encoder: Encoder) throws { var container = encoder.singleValueContainer(); try container.encode(rawValue) }
}

public final class VoiceSettings {
    private let defaults: UserDefaults
    public static let languages: [(code: String, name: String)] = [("auto", "Detect automatically"), ("en", "English"), ("hy", "Armenian"), ("ru", "Russian"), ("fr", "French"), ("de", "German"), ("es", "Spanish"), ("it", "Italian"), ("pt", "Portuguese"), ("ar", "Arabic")]
    public static let idleOptions = [1, 2, 5, 15, 30, 60]
    public init(group: String?) { defaults = group.flatMap(UserDefaults.init(suiteName:)) ?? .standard }
    public var language: String {
        get { let code = defaults.string(forKey: "voiceLanguage") ?? "auto"; return Self.languages.contains { $0.code == code } ? code : "auto" }
        set { defaults.set(newValue, forKey: "voiceLanguage") }
    }
    public var defaultAction: VoiceAction {
        get { defaults.data(forKey: "voiceDefaultAction").flatMap { try? JSONDecoder().decode(VoiceAction.self, from: $0) } ?? .automatic }
        set { defaults.set(try? JSONEncoder().encode(newValue), forKey: "voiceDefaultAction") }
    }
    public var idleMinutes: Int {
        get { let value = defaults.integer(forKey: "voiceIdleMinutes"); return Self.idleOptions.contains(value) ? value : 2 }
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
    /// What the app does with the take; nil = the app's default (Smart). The keyboard sends
    /// `.transcribeOnly` when it finishes the request itself (Agent mode, screen frames).
    public var action: VoiceAction?
    public init(id: UUID = UUID(), kind: Kind, requestID: UUID, context: VoiceContext? = nil, sentAt: Date = Date(), action: VoiceAction? = nil) { self.id = id; self.kind = kind; self.requestID = requestID; self.context = context; self.sentAt = sentAt; self.action = action }
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
    public var resultKind: JobResult.Kind?
    public init(phase: VoicePhase = .off, requestID: UUID? = nil, heartbeat: Date = Date(), sessionEndsAt: Date? = nil, level: Float = 0, message: String? = nil, result: String? = nil, replacesSelection: Bool = false, resultKind: JobResult.Kind? = nil) {
        self.phase = phase; self.requestID = requestID; self.heartbeat = heartbeat; self.sessionEndsAt = sessionEndsAt; self.level = level; self.message = message; self.result = result; self.replacesSelection = replacesSelection
        self.resultKind = resultKind
    }
    public func isAlive(now: Date = Date()) -> Bool { phase != .off && now.timeIntervalSince(heartbeat) < 3 }
}

public struct VoiceLaunch: Codable {
    public let nonce: UUID
    public let requestID: UUID
    public let issuedAt: Date
    public init(requestID: UUID, nonce: UUID = UUID(), issuedAt: Date = Date()) {
        self.requestID = requestID; self.nonce = nonce; self.issuedAt = issuedAt
    }
}

public struct VoiceTranscript: Codable, Equatable {
    public let requestID: UUID
    public let text: String
    public init(requestID: UUID, text: String) { self.requestID = requestID; self.text = text }
}

public struct VoiceChannel {
    enum Route { case file, pasteboard, unavailable, disabled, fullAccessRequired }
    private struct Packet<Value: Codable>: Codable { let issuedAt: Date; let value: Value }
    public enum Notification: String { case command, status }
    public let container: URL?
    public let group: String
    private let pasteboard: SessionBackend?
    private let route: () -> Route
    private let keys = ["command", "status", "keyboard", "launch", "transcript", "receipt", "probe"]
    public init(container: URL?, group: String = "group.com.aram2k.gusan") {
        self.container = container; self.group = group; pasteboard = nil; route = { container == nil ? .unavailable : .file }
    }
    init(container: URL?, group: String, pasteboard: SessionBackend, route: @escaping () -> Route) {
        self.container = container; self.group = group; self.pasteboard = pasteboard; self.route = route
    }
    public var unavailableError: VoiceError {
        switch route() {
        case .disabled: return .fallbackOff
        case .fullAccessRequired: return .fullAccessRequired
        default: return .notShared
        }
    }
    public var diagnosticsDescription: String {
        switch route() {
        case .file: return "App Group"
        case .pasteboard: return available ? "Pasteboard fallback" : "Unavailable (pasteboard read/write denied)"
        case .disabled: return "Unavailable (pasteboard fallback is off)"
        case .fullAccessRequired: return "Unavailable (Full Access is off)"
        case .unavailable: return "Unavailable (no shared voice storage)"
        }
    }
    public var available: Bool {
        switch storage() {
        case .file: return container != nil
        case .pasteboard:
            let readable = pasteboard?.read("probe").readable == true
            let writable = pasteboard?.write(Data([0]), key: "probe") == true
            pasteboard?.clear("probe"); return readable && writable
        default: return false
        }
    }
    private func storage() -> Route {
        let selected = route()
        if selected != .pasteboard { for key in keys { pasteboard?.clear(key) } }
        if selected != .file { for key in keys { if let url = file(key) { try? FileManager.default.removeItem(at: url) } } }
        return selected
    }
    private func file(_ name: String) -> URL? { container?.appendingPathComponent("voice", isDirectory: true).appendingPathComponent(name + ".json") }
    private func read<T: Codable>(_ name: String, as: T.Type) -> T? {
        if storage() == .pasteboard {
            guard let data = pasteboard?.read(name).data, let packet = try? JSONDecoder().decode(Packet<T>.self, from: data) else { return nil }
            guard (0..<600).contains(Date().timeIntervalSince(packet.issuedAt)) else { pasteboard?.clear(name); return nil }
            return packet.value
        }
        guard storage() == .file else { return nil }
        return file(name).flatMap { try? Data(contentsOf: $0) }.flatMap { try? JSONDecoder().decode(T.self, from: $0) }
    }
    private func write<T: Codable>(_ value: T, name: String) throws {
        let selected = storage()
        if selected == .pasteboard {
            let data = try JSONEncoder().encode(Packet(issuedAt: Date(), value: value))
            guard pasteboard?.write(data, key: name) == true else { throw VoiceError.notShared }; return
        }
        guard selected == .file else { throw unavailableError }
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
    public func readStatusSnapshot() async -> VoiceStatus? {
        let source = await MainActor.run { () -> (URL?, Data?, Bool) in
            switch storage() {
            case .file: return (file("status"), nil, false)
            case .pasteboard: return (nil, pasteboard?.read("status").data, true)
            default: return (nil, nil, false)
            }
        }
        let decoded = await Task.detached { () -> (VoiceStatus?, Bool) in
            guard let data = source.1 ?? source.0.flatMap({ try? Data(contentsOf: $0) }) else { return (nil, false) }
            if source.2 {
                guard let packet = try? JSONDecoder().decode(Packet<VoiceStatus>.self, from: data) else { return (nil, false) }
                guard (0..<600).contains(Date().timeIntervalSince(packet.issuedAt)) else { return (nil, true) }
                return (packet.value, false)
            }
            return (try? JSONDecoder().decode(VoiceStatus.self, from: data), false)
        }.value
        if decoded.1 {
            await MainActor.run { if pasteboard?.read("status").data == source.1 { pasteboard?.clear("status") } }
        }
        return decoded.0
    }
    public func readCommand() -> VoiceCommand? { read("command", as: VoiceCommand.self) }
    public func failureStatus(message: String) -> VoiceStatus {
        VoiceStatus(phase: .failed, requestID: readCommand()?.requestID ?? readKeyboardState()?.requestID, heartbeat: .distantPast, message: message)
    }
    public func consumeCommand() -> VoiceCommand? { let command = readCommand(); if command != nil { _ = remove("command") }; return command }
    public func readTranscript() -> VoiceTranscript? { read("transcript", as: VoiceTranscript.self) }
    public func write(transcript: VoiceTranscript) throws { try write(transcript, name: "transcript") }
    public func readKeyboardState() -> KeyboardVoiceState? { read("keyboard", as: KeyboardVoiceState.self) }
    public func write(launch: VoiceLaunch) throws { try write(launch, name: "launch") }
    public func consumeLaunch(nonce: String?, now: Date = Date()) -> Bool {
        guard let nonce, let launch = read("launch", as: VoiceLaunch.self), UUID(uuidString: nonce) == launch.nonce,
              now.timeIntervalSince(launch.issuedAt) >= 0, now.timeIntervalSince(launch.issuedAt) < 60,
              let command = readCommand(), command.kind == .start, command.requestID == launch.requestID,
              now.timeIntervalSince(command.sentAt) >= 0, now.timeIntervalSince(command.sentAt) < 60,
              remove("launch") else { return false }
        return true
    }
    @discardableResult private func remove(_ name: String) -> Bool {
        if storage() == .pasteboard { pasteboard?.clear(name); return pasteboard?.read(name).data == nil }
        guard let url = file(name) else { return false }
        return (try? FileManager.default.removeItem(at: url)) != nil
    }
    public func clear() {
        for key in keys { pasteboard?.clear(key); if let url = file(key) { try? FileManager.default.removeItem(at: url) } }
    }
    public func clearPasteboard() { for key in keys { pasteboard?.clear(key) } }
    public func consumeResult(requestID: UUID) throws {
        guard readStatus()?.requestID == requestID else { return }
        try write(requestID, name: "receipt")
        discardRequest(requestID)
        if readStatus()?.requestID == requestID { _ = remove("status") }
    }
    public func discardRequest(_ id: UUID) {
        if readCommand()?.requestID == id { _ = remove("command") }
        if readKeyboardState()?.requestID == id { _ = remove("keyboard") }
        if readTranscript()?.requestID == id { _ = remove("transcript") }
        if read("launch", as: VoiceLaunch.self)?.requestID == id { _ = remove("launch") }
    }
    public func consumeReceipt() -> UUID? { let id = read("receipt", as: UUID.self); if id != nil { _ = remove("receipt") }; return id }
    public func write(status: VoiceStatus) throws {
        if status.phase == .off { clear(); return }
        if [.done, .failed].contains(status.phase), let id = status.requestID, read("receipt", as: UUID.self) == id { return }
        if status.phase == .failed, let id = status.requestID {
            discardRequest(id)
            try write(keyboard: KeyboardVoiceState(requestID: id))
        }
        try write(status, name: "status")
    }
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
