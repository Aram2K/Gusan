import Foundation

public enum VaultLoginChangeError: LocalizedError {
    case rollbackFailed(VaultStoreError)
    public var errorDescription: String? {
        switch self {
        case .rollbackFailed(let error):
            return error.localizedDescription + " The cloud entry couldn't be removed. Delete it from Logins and try again."
        }
    }
}

public enum VaultLoginChanges {
    public static func save(_ credential: VaultCredential, loginID: UUID, existingLoginIDs: Set<UUID>,
                            store: VaultStore, service: VaultService) async throws {
        do {
            try store.save(credential, loginID: loginID)
        } catch {
            let storageError = error
            if !existingLoginIDs.contains(loginID) {
                do { try await service.delete(loginID: loginID) }
                catch { throw VaultLoginChangeError.rollbackFailed(storageError as? VaultStoreError ?? .storage) }
            }
            throw storageError
        }
    }

    public static func delete(loginID: UUID, store: VaultStore, service: VaultService) async throws {
        try await service.delete(loginID: loginID)
        try store.delete(loginID: loginID)
    }
}
