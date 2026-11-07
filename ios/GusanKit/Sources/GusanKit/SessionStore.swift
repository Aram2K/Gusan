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

public struct SessionDiagnostics {
    public let groupID: String?
    public let keychainOK: Bool
    public let fileOK: Bool
    public var isShared: Bool { groupID != nil && (keychainOK || fileOK) }
    public var sharingStatus: String { isShared ? "shared" : "not shared" }
}

public final class SessionStore: SessionStorage {
    public let resolution: AppGroupResolution
    private var keychainOK = false
    private var fileOK = false
    private let lock = NSLock()
    private var fileURL: URL? { resolution.container?.appendingPathComponent("session.json") }
    public init(resolution: AppGroupResolution = AppGroupResolver.resolve()) { self.resolution = resolution }
    private func query(shared: Bool) -> [String: Any] {
        var q: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "Gusan.session", kSecAttrAccount as String: "session"]
        if shared, let group = resolution.chosen { q[kSecAttrAccessGroup as String] = group }
        return q
    }
    private func loadKeychain(shared: Bool) -> Session? {
        var q = query(shared: shared)
        q[kSecReturnData as String] = true
        q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        if shared { keychainOK = status == errSecSuccess || status == errSecItemNotFound }
        guard status == errSecSuccess, let data = result as? Data else { return nil }
        return try? JSONDecoder().decode(Session.self, from: data)
    }
    public func read() -> Session? {
        lock.lock(); defer { lock.unlock() }
        var sessions: [Session] = []
        if resolution.chosen != nil, let session = loadKeychain(shared: true) { sessions.append(session) }
        fileOK = fileURL.map { FileManager.default.isWritableFile(atPath: $0.deletingLastPathComponent().path) } ?? false
        if let url = fileURL, let data = try? Data(contentsOf: url), let session = try? JSONDecoder().decode(Session.self, from: data) {
            fileOK = true; sessions.append(session)
        }
        if !sharedBackendAvailable, let session = loadKeychain(shared: false) { sessions.append(session) }
        return sessions.max { $0.updatedAt < $1.updatedAt }
    }
    static func sharedBackendAvailable(group: String?, keychainOK: Bool, fileOK: Bool) -> Bool {
        group != nil && (keychainOK || fileOK)
    }
    private var sharedBackendAvailable: Bool {
        Self.sharedBackendAvailable(group: resolution.chosen, keychainOK: keychainOK, fileOK: fileOK)
    }
    private func saveKeychain(_ data: Data, shared: Bool) -> Bool {
        let q = query(shared: shared)
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock]
        var status = SecItemUpdate(q as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound { status = SecItemAdd(q.merging(attributes) { _, new in new } as CFDictionary, nil) }
        return status == errSecSuccess
    }
    public func write(_ session: Session) {
        lock.lock(); defer { lock.unlock() }
        guard let data = try? JSONEncoder().encode(session) else { return }
        keychainOK = resolution.chosen != nil && saveKeychain(data, shared: true)
        fileOK = false
        if let url = fileURL {
            do {
                #if os(iOS)
                try data.write(to: url, options: [.atomic, .completeFileProtectionUntilFirstUserAuthentication])
                #else
                try data.write(to: url, options: .atomic)
                #endif
                fileOK = true
            } catch { fileOK = false }
        }
        if sharedBackendAvailable { SecItemDelete(query(shared: false) as CFDictionary) }
        else { _ = saveKeychain(data, shared: false) }
    }
    public func clear() {
        lock.lock(); defer { lock.unlock() }
        SecItemDelete(query(shared: false) as CFDictionary)
        if resolution.chosen != nil { SecItemDelete(query(shared: true) as CFDictionary) }
        if let url = fileURL { try? FileManager.default.removeItem(at: url) }
    }
    public func diagnostics() -> SessionDiagnostics {
        lock.lock(); defer { lock.unlock() }
        return SessionDiagnostics(groupID: resolution.chosen, keychainOK: keychainOK, fileOK: fileOK)
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
