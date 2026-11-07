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
    init(group: String?, enabled: Bool) { self.group = group; self.enabled = enabled }
    private func query(_ key: String) -> [String: Any] {
        var result: [String: Any] = [kSecClass as String: kSecClassGenericPassword, kSecAttrService as String: "Gusan." + key, kSecAttrAccount as String: key]
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
    init(namespace: String = "com.aram2k.gusan.session", accessAllowed: @escaping () -> Bool = { true }) {
        self.namespace = namespace; self.accessAllowed = accessAllowed
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
        value.setItems([[namespace: data]], options: [.localOnly: true])
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
