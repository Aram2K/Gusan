import Foundation
import Security
#if canImport(UIKit)
import UIKit
#endif

public struct SessionBackendRead {
    public let data: Data?
    public let readable: Bool
    public init(data: Data?, readable: Bool) { self.data = data; self.readable = readable }
}

public protocol SessionBackend: AnyObject {
    var name: String { get }
    func read(_ key: String) -> SessionBackendRead
    func write(_ data: Data, key: String) -> Bool
    func clear(_ key: String)
}

final class KeychainSessionBackend: SessionBackend {
    let name = "Keychain"
    private let group: String?
    private let enabled: Bool
    private let service: String
    init(group: String?, enabled: Bool, service: String = "Gusan.") { self.group = group; self.enabled = enabled; self.service = service }
    private func query(_ key: String) -> [String: Any] {
        var result: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: service + key, kSecAttrAccount as String: key]
        if let group { result[kSecAttrAccessGroup as String] = group }
        return result
    }
    func read(_ key: String) -> SessionBackendRead {
        guard enabled else { return SessionBackendRead(data: nil, readable: false) }
        var q = query(key); q[kSecReturnData as String] = true; q[kSecMatchLimit as String] = kSecMatchLimitOne
        var result: CFTypeRef?
        let status = SecItemCopyMatching(q as CFDictionary, &result)
        return SessionBackendRead(data: result as? Data, readable: status == errSecSuccess || status == errSecItemNotFound)
    }
    func write(_ data: Data, key: String) -> Bool {
        guard enabled else { return false }
        let q = query(key)
        let attributes: [String: Any] = [kSecValueData as String: data, kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        var status = SecItemUpdate(q as CFDictionary, attributes as CFDictionary)
        if status == errSecItemNotFound { status = SecItemAdd(q.merging(attributes) { _, new in new } as CFDictionary, nil) }
        return status == errSecSuccess
    }
    func clear(_ key: String) { if enabled { SecItemDelete(query(key) as CFDictionary) } }
}

final class FileSessionBackend: SessionBackend {
    let name = "App-group file"
    private let container: URL?
    init(container: URL?) { self.container = container }
    func read(_ key: String) -> SessionBackendRead {
        guard let container else { return SessionBackendRead(data: nil, readable: false) }
        let url = container.appendingPathComponent(key + ".json")
        do { return SessionBackendRead(data: try Data(contentsOf: url), readable: true) }
        catch { return SessionBackendRead(data: nil, readable: !FileManager.default.fileExists(atPath: url.path) && FileManager.default.isReadableFile(atPath: container.path)) }
    }
    func write(_ data: Data, key: String) -> Bool {
        guard let url = container?.appendingPathComponent(key + ".json") else { return false }
        #if os(iOS)
        let options: Data.WritingOptions = [.atomic, .completeFileProtectionUntilFirstUserAuthentication]
        #else
        let options: Data.WritingOptions = .atomic
        #endif
        return (try? data.write(to: url, options: options)) != nil
    }
    func clear(_ key: String) { if let url = container?.appendingPathComponent(key + ".json") { try? FileManager.default.removeItem(at: url) } }
}

final class NamedSessionPasteboard: SessionBackend {
    let name = "Team pasteboard"
    private let namespace: String
    private let accessAllowed: () -> Bool
    private let lifetime: TimeInterval?
    private let keyLifetimes: [String: TimeInterval]
    init(namespace: String = "com.aram2k.gusan.session", lifetime: TimeInterval? = nil, keyLifetimes: [String: TimeInterval] = [:], accessAllowed: @escaping () -> Bool = { true }) {
        self.namespace = namespace; self.accessAllowed = accessAllowed; self.lifetime = lifetime; self.keyLifetimes = keyLifetimes
    }
    #if canImport(UIKit)
    private func board(_ key: String, create: Bool) -> UIPasteboard? {
        guard accessAllowed() else { return nil }
        return UIPasteboard(name: UIPasteboard.Name(rawValue: key == "session" ? namespace : namespace + "." + key), create: create)
    }
    #endif
    func read(_ key: String) -> SessionBackendRead {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.read(key) } }
        guard accessAllowed() else { return SessionBackendRead(data: nil, readable: false) }
        let value = board(key, create: true)
        return SessionBackendRead(data: value?.data(forPasteboardType: namespace), readable: value != nil)
        #else
        return SessionBackendRead(data: nil, readable: false)
        #endif
    }
    func write(_ data: Data, key: String) -> Bool {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.write(data, key: key) } }
        guard let value = board(key, create: true) else { return false }
        value.setPersistent(true)
        var options: [UIPasteboard.OptionsKey: Any] = [.localOnly: true]
        if let lifetime = keyLifetimes[key] ?? lifetime { options[.expirationDate] = Date().addingTimeInterval(lifetime) }
        value.setItems([[namespace: data]], options: options)
        return value.data(forPasteboardType: namespace) == data
        #else
        return false
        #endif
    }
    func clear(_ key: String) {
        #if canImport(UIKit)
        if !Thread.isMainThread { DispatchQueue.main.sync { self.clear(key) }; return }
        board(key, create: false)?.items = []
        #endif
    }
}

/// The signing team's keychain prefix ("TEAMID"), learned from the keychain itself so it survives re-signing
/// (Sideloadly/AltStore change the team). Items in "TEAMID.<name>" groups are shared by the app and its extensions
/// whenever the signature allows that group (sideloading tools sign free-account builds with a "TEAMID.*" wildcard;
/// the unsigned IPA declares no keychain groups, so Xcode-signed builds simply fall back to the App Group route).
public enum SharedKeychain {
    private static var cached: String??
    public static func teamPrefix() -> String? {
        if let cached { return cached }
        let query: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "Gusan.team-probe",
                                    kSecAttrAccount as String: "team-probe", kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly]
        SecItemAdd(query.merging([kSecValueData as String: Data([1])]) { _, new in new } as CFDictionary, nil)
        var result: CFTypeRef?
        var lookup = query; lookup[kSecReturnAttributes as String] = true; lookup[kSecMatchLimit as String] = kSecMatchLimitOne
        let status = SecItemCopyMatching(lookup as CFDictionary, &result)
        let group = status == errSecSuccess ? (result as? [String: Any])?[kSecAttrAccessGroup as String] as? String : nil
        let prefix = parsePrefix(group)
        cached = .some(prefix)
        return prefix
    }
    /// "ABCDE12345.com.example.app" → "ABCDE12345"; nil for anything that is not a team-prefixed group.
    static func parsePrefix(_ group: String?) -> String? {
        guard let group, let dot = group.firstIndex(of: "."), dot > group.startIndex else { return nil }
        let prefix = String(group[..<dot])
        return prefix.count == 10 && prefix.allSatisfy({ $0.isLetter || $0.isNumber }) ? prefix : nil
    }
    /// The keyboard extension's own keychain group ("TEAMID.<app bundle>.keyboard"). The keyboard can always read
    /// it (no entitlement beyond its default), and the app/screen extension can write to it when their signature
    /// carries the team wildcard, which free-account sideloading tools grant.
    static func keyboardBundleID(from bundleID: String?) -> String? {
        guard let bundleID, !bundleID.isEmpty else { return nil }
        if bundleID.hasSuffix(".keyboard") { return bundleID }
        for suffix in [".screen", ".keyboardpreview", ".activity"] where bundleID.hasSuffix(suffix) {
            return String(bundleID.dropLast(suffix.count)) + ".keyboard"
        }
        return bundleID + ".keyboard"
    }
    /// Keychain backends shared with the extensions, best first; empty when the team prefix is unknown.
    /// Values for `name` live under their own service so they never collide with the keyboard's private items.
    public static func backend(named name: String, maxBytes: Int? = nil) -> SessionBackend? {
        guard let team = teamPrefix() else { return nil }
        var groups = ["\(team).com.aram2k.gusan.\(name)"]
        if let keyboard = keyboardBundleID(from: Bundle.main.bundleIdentifier) { groups.insert("\(team).\(keyboard)", at: 0) }
        let layers: [SessionBackend] = groups.map { group in
            let keychain = KeychainSessionBackend(group: group, enabled: true, service: "Gusan.shared.\(name).")
            return maxBytes.map { SizeLimitedSessionBackend(wrapping: keychain, maxBytes: $0) } ?? keychain
        }
        return layers.count == 1 ? layers[0] : LayeredSessionBackend(layers)
    }
}

/// Writes go to the first backend that accepts them; reads take the newest-looking value (first hit wins).
final class LayeredSessionBackend: SessionBackend {
    let name: String
    private let layers: [SessionBackend]
    init(_ layers: [SessionBackend]) { self.layers = layers; name = layers.map(\.name).joined(separator: "/") }
    func read(_ key: String) -> SessionBackendRead {
        var readable = false
        for layer in layers {
            let value = layer.read(key)
            readable = readable || value.readable
            if let data = value.data { return SessionBackendRead(data: data, readable: true) }
        }
        return SessionBackendRead(data: nil, readable: readable)
    }
    func write(_ data: Data, key: String) -> Bool {
        for (index, layer) in layers.enumerated() where layer.write(data, key: key) {
            // Only one layer may hold a key, or a reader could pick up a stale packet from another layer.
            for (other, backend) in layers.enumerated() where other != index { backend.clear(key) }
            return true
        }
        return false
    }
    func clear(_ key: String) { layers.forEach { $0.clear(key) } }
}

/// Refuses payloads above `maxBytes` (keychain items should stay small); reads/clears pass through.
final class SizeLimitedSessionBackend: SessionBackend {
    let name: String
    private let inner: SessionBackend
    private let maxBytes: Int
    init(wrapping inner: SessionBackend, maxBytes: Int) { self.inner = inner; self.maxBytes = maxBytes; name = inner.name }
    func read(_ key: String) -> SessionBackendRead { inner.read(key) }
    func write(_ data: Data, key: String) -> Bool { data.count <= maxBytes && inner.write(data, key: key) }
    func clear(_ key: String) { inner.clear(key) }
}
