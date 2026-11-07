import Foundation
import Security

public struct Session: Codable, Equatable {
    public var accessToken: String
    public var expiresAt: Date
    public var refreshToken: String
    public var userID: String?
    public var email: String?
    public var updatedAt: Date
    public init(accessToken: String, expiresAt: Date, refreshToken: String, userID: String? = nil, email: String? = nil, updatedAt: Date = Date()) {
        self.accessToken = accessToken; self.expiresAt = expiresAt; self.refreshToken = refreshToken
        self.userID = userID; self.email = email; self.updatedAt = updatedAt
    }
}

public protocol SessionStorage: AnyObject {
    func read() -> Session?
    func write(_ session: Session)
    func clear()
}

public struct AppGroupResolution {
    public let candidatesTried: [String]
    public let chosen: String?
    public let container: URL?
}

public enum AppGroupResolver {
    public static func provisionGroups(_ data: Data) -> [String] {
        guard let start = data.range(of: Data("<?xml".utf8)),
              let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
              let plist = try? PropertyListSerialization.propertyList(from: data[start.lowerBound..<end.upperBound], format: nil) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any],
              let groups = entitlements["com.apple.security.application-groups"] as? [String] else { return [] }
        return groups
    }
    public static func resolve(bundle: Bundle = .main, container: (String) -> URL? = { FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: $0) }) -> AppGroupResolution {
        let data = bundle.url(forResource: "embedded", withExtension: "mobileprovision").flatMap { try? Data(contentsOf: $0) }
        return resolve(provision: data, fallback: bundle.object(forInfoDictionaryKey: "GusanAppGroup") as? String, container: container)
    }
    public static func resolve(provision: Data?, fallback: String?, container: (String) -> URL?) -> AppGroupResolution {
        let groups = provision.map(provisionGroups) ?? []
        let preferred = groups.filter { $0.localizedCaseInsensitiveContains("gusan") }
        var candidates = preferred + groups.filter { !preferred.contains($0) }
        if let fallback, !candidates.contains(fallback) { candidates.append(fallback) }
        var tried: [String] = []
        for group in candidates {
            tried.append(group)
            if let url = container(group) { return AppGroupResolution(candidatesTried: tried, chosen: group, container: url) }
        }
        return AppGroupResolution(candidatesTried: tried, chosen: nil, container: nil)
    }
}

public struct StoreHealth: Codable, Equatable {
    public let name: String
    public let readable: Bool
    public let writable: Bool
    public let hasSession: Bool
    public var summary: String { "\(name): read \(readable ? "yes" : "no"), write \(writable ? "yes" : "no"), session \(hasSession ? "yes" : "no")" }
}

public enum SessionRole: String, Codable { case app, keyboard }

public struct SessionReport: Codable {
    public let role: SessionRole
    public let timestamp: Date
    public let groupID: String?
    public let keychainGroupID: String?
    public let stores: [StoreHealth]
}

public struct SessionDiagnostics {
    public let local: SessionReport
    public let peer: SessionReport?
    public var groupID: String? { local.groupID }
    public var keychainOK: Bool { local.stores.contains { $0.name == "Keychain" && $0.readable && $0.writable } }
    public var fileOK: Bool { local.stores.contains { $0.name == "App-group file" && $0.readable && $0.writable } }
    public var pasteboardOK: Bool { local.stores.contains { $0.name == "Team pasteboard" && $0.readable && $0.writable } }
    public var isShared: Bool { local.stores.contains { $0.readable && $0.writable } }
    public var sharingStatus: String { isShared ? "shared" : "not shared" }
    public var summary: String { local.stores.map(\.summary).joined(separator: "\n") }
}

public final class SessionStore: SessionStorage {
    public let resolution: AppGroupResolution
    public let role: SessionRole
    private let backends: [SessionBackend]
    private let privateBackend: SessionBackend
    private let keychainGroup: String?
    private let lock = NSLock()
    public init(resolution: AppGroupResolution = AppGroupResolver.resolve(), role: SessionRole = .app, pasteboardAccessAllowed: @escaping () -> Bool = { true }) {
        self.role = role
        let pasteboard = NamedSessionPasteboard(accessAllowed: pasteboardAccessAllowed)
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--gusan-end-storage-test") {
            pasteboard.clear("debug-mode")
            let isolatedStore = NamedSessionPasteboard(namespace: "com.aram2k.gusan.debug.session", accessAllowed: pasteboardAccessAllowed)
            for key in ["session", "report.app", "report.keyboard"] { isolatedStore.clear(key) }
        }
        if ProcessInfo.processInfo.arguments.contains("--gusan-pasteboard-only") {
            _ = pasteboard.write(Data([1]), key: "debug-mode")
        }
        let isolated = pasteboard.read("debug-mode").data == Data([1])
        #else
        let isolated = false
        #endif
        self.resolution = isolated ? AppGroupResolution(candidatesTried: [], chosen: nil, container: nil) : resolution
        keychainGroup = isolated ? nil : resolution.chosen
        backends = [KeychainSessionBackend(group: keychainGroup, enabled: keychainGroup != nil), FileSessionBackend(container: self.resolution.container), NamedSessionPasteboard(namespace: isolated ? "com.aram2k.gusan.debug.session" : "com.aram2k.gusan.session", accessAllowed: pasteboardAccessAllowed)]
        privateBackend = KeychainSessionBackend(group: nil, enabled: !isolated)
    }
    init(resolution: AppGroupResolution, backends: [SessionBackend], privateBackend: SessionBackend, role: SessionRole = .app) {
        self.resolution = resolution; self.backends = backends; self.privateBackend = privateBackend; self.role = role
        keychainGroup = resolution.chosen
    }
    private func synchronized<T>(_ body: () -> T) -> T {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.synchronized(body) } }
        #endif
        lock.lock(); defer { lock.unlock() }; return body()
    }
    private func decode(_ data: Data?) -> Session? { data.flatMap { try? JSONDecoder().decode(Session.self, from: $0) } }
    public func read() -> Session? {
        synchronized {
            let sessions = backends.compactMap { decode($0.read("session").data) }
            if let newest = sessions.max(by: { $0.updatedAt == $1.updatedAt ? $0.expiresAt < $1.expiresAt : $0.updatedAt < $1.updatedAt }) { return newest }
            guard let session = decode(privateBackend.read("session").data) else { return nil }
            if let data = try? JSONEncoder().encode(session), writeShared(data) { privateBackend.clear("session") }
            return session
        }
    }
    static func sharedBackendAvailable(group: String?, keychainOK: Bool, fileOK: Bool) -> Bool {
        group != nil && (keychainOK || fileOK)
    }
    private func writeShared(_ data: Data) -> Bool {
        var saved = false
        for backend in backends { if backend.write(data, key: "session") { saved = true } }
        return saved
    }
    public func write(_ session: Session) {
        synchronized {
            guard let data = try? JSONEncoder().encode(session) else { return }
            if writeShared(data) { privateBackend.clear("session") }
            else { _ = privateBackend.write(data, key: "session") }
        }
    }
    public func clear() {
        synchronized {
            for backend in backends { backend.clear("session") }
            privateBackend.clear("session")
        }
    }
    public func diagnostics() -> SessionDiagnostics {
        synchronized {
            let reportKey = "report." + role.rawValue
            let stores = backends.map { backend -> StoreHealth in
                let value = backend.read("session")
                let writable = backend.write(Data([0]), key: "probe." + role.rawValue)
                backend.clear("probe." + role.rawValue)
                return StoreHealth(name: backend.name, readable: value.readable, writable: writable, hasSession: decode(value.data) != nil)
            }
            let report = SessionReport(role: role, timestamp: Date(), groupID: resolution.chosen, keychainGroupID: keychainGroup, stores: stores)
            if let data = try? JSONEncoder().encode(report) { for backend in backends { _ = backend.write(data, key: reportKey) } }
            let peerKey = "report." + (role == .app ? "keyboard" : "app")
            let peer = backends.compactMap { $0.read(peerKey).data.flatMap { try? JSONDecoder().decode(SessionReport.self, from: $0) } }.max { $0.timestamp < $1.timestamp }
            return SessionDiagnostics(local: report, peer: peer)
        }
    }
}

public final class SettingsStore {
    private let defaults: UserDefaults
    public init(group: String?) { defaults = group.flatMap(UserDefaults.init(suiteName:)) ?? .standard }
    public var defaultAction: SmartActionMode {
        get { defaults.string(forKey: "defaultAction").flatMap(SmartActionMode.init(rawValue:)) ?? .smartAuto }
        set { defaults.set(newValue.rawValue, forKey: "defaultAction") }
    }
    public var translateTarget: String {
        get { Prompts.normalizeTarget(defaults.string(forKey: "translateTarget") ?? "en") }
        set { defaults.set(Prompts.normalizeTarget(newValue), forKey: "translateTarget") }
    }
}
