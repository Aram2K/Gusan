import Foundation

public enum VaultLoginChangeError: LocalizedError {
    case rollbackFailed(VaultStoreError)
    case restoreFailed(VaultStoreError)
    public var errorDescription: String? {
        switch self {
        case .rollbackFailed(let error):
            return error.localizedDescription + " The cloud entry couldn't be removed. Delete it from Logins and try again."
        case .restoreFailed(let error):
            return error.localizedDescription + " The saved login was kept, but its cloud details couldn't be restored. Try adding it again."
        }
    }
}

public enum VaultLoginChanges {
    public static func save(_ credential: VaultCredential, loginID: UUID, previousLogins: [VaultLogin],
                            store: VaultStore, service: VaultService) async throws {
        do {
            try store.save(credential, loginID: loginID)
        } catch {
            let storageError = error
            if let previous = previousLogins.first(where: { $0.id == loginID }) {
                do { _ = try await service.add(hostname: previous.hostname, usernameHint: previous.usernameHint, hasTOTP: previous.hasTOTP) }
                catch { throw VaultLoginChangeError.restoreFailed(storageError as? VaultStoreError ?? .storage) }
            } else {
                do { try await service.delete(loginID: loginID) }
                catch { throw VaultLoginChangeError.rollbackFailed(storageError as? VaultStoreError ?? .storage) }
            }
            throw storageError
        }
    }

    public static func delete(loginID: UUID, store: VaultStore, service: VaultService) async throws {
        do { try await service.delete(loginID: loginID) }
        catch let error as VaultClientError where error.code == "not_found" {}
        try store.delete(loginID: loginID)
    }
}
