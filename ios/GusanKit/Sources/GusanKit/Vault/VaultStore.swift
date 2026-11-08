import Foundation
import LocalAuthentication
import Security

public protocol VaultKeychain {
    func add(_ query: [String: Any]) -> OSStatus
    func copy(_ query: [String: Any]) -> (OSStatus, Data?)
    func delete(_ query: [String: Any]) -> OSStatus
}

public struct SystemVaultKeychain: VaultKeychain {
    public init() {}
    public func add(_ query: [String: Any]) -> OSStatus { SecItemAdd(query as CFDictionary, nil) }
    public func copy(_ query: [String: Any]) -> (OSStatus, Data?) {
        var result: CFTypeRef?
        let status = SecItemCopyMatching(query as CFDictionary, &result)
        return (status, result as? Data)
    }
    public func delete(_ query: [String: Any]) -> OSStatus { SecItemDelete(query as CFDictionary) }
}

public enum VaultStoreError: LocalizedError {
    case passcodeRequired, missing, authentication, storage
    public var errorDescription: String? {
        switch self {
        case .passcodeRequired: return "Set a device passcode to use Vault"
        case .missing: return "This login isn't on this iPhone. Add it again; re-enrolling Face ID removes saved logins."
        case .authentication: return "Face ID couldn't unlock this login. Try again."
        case .storage: return "Couldn't save or read this login securely. Try again."
        }
    }
}

public final class VaultStore {
    public static let service = "Gusan.vault.login"
    private let keychain: VaultKeychain
    public init(keychain: VaultKeychain = SystemVaultKeychain()) { self.keychain = keychain }

    private func query(_ loginID: UUID? = nil) -> [String: Any] {
        var query: [String: Any] = [kSecClass as String: kSecClassGenericPassword,
                                    kSecAttrService as String: Self.service,
                                    kSecAttrSynchronizable as String: false]
        if let loginID { query[kSecAttrAccount as String] = loginID.uuidString.lowercased() }
        return query
    }
    public func save(_ credential: VaultCredential, loginID: UUID) throws {
        guard let access = SecAccessControlCreateWithFlags(nil, kSecAttrAccessibleWhenPasscodeSetThisDeviceOnly, .biometryCurrentSet, nil) else { throw VaultStoreError.passcodeRequired }
        var attributes = query(loginID)
        attributes[kSecAttrAccessControl as String] = access
        attributes[kSecValueData as String] = try JSONEncoder().encode(credential)
        var status = keychain.add(attributes)
        if status == errSecDuplicateItem {
            try delete(loginID: loginID)
            status = keychain.add(attributes)
        }
        try check(status)
    }
    public func read(loginID: UUID, context: LAContext) throws -> VaultCredential {
        var attributes = query(loginID)
        attributes[kSecUseAuthenticationContext as String] = context
        attributes[kSecReturnData as String] = true
        attributes[kSecMatchLimit as String] = kSecMatchLimitOne
        let (status, data) = keychain.copy(attributes)
        try check(status)
        guard let data, let credential = try? JSONDecoder().decode(VaultCredential.self, from: data) else { throw VaultStoreError.storage }
        return credential
    }
    public func delete(loginID: UUID) throws { try check(keychain.delete(query(loginID)), missingOK: true) }
    public func deleteAll() throws { try check(keychain.delete(query()), missingOK: true) }
    private func check(_ status: OSStatus, missingOK: Bool = false) throws {
        if status == errSecSuccess || (missingOK && status == errSecItemNotFound) { return }
        switch status {
        case errSecDecode, errSecNotAvailable, errSecAuthFailed: throw VaultStoreError.passcodeRequired
        case errSecItemNotFound: throw VaultStoreError.missing
        case errSecUserCanceled, errSecInteractionNotAllowed: throw VaultStoreError.authentication
        default: throw VaultStoreError.storage
        }
    }
}
