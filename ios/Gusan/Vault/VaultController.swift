import Foundation
import SwiftUI
import LocalAuthentication
import GusanKit

@MainActor
final class VaultController: ObservableObject {
    @Published private(set) var logins: [VaultLogin] = []
    @Published private(set) var events: [VaultEvent] = []
    @Published private(set) var pending: [VaultPendingLogin] = []
    @Published private(set) var loading = false
    @Published private(set) var busy = false
    @Published var error: String?
    private let store = VaultStore()
    private var service: VaultService?
    private var account: String?
    private var generation = 0
    private var refreshing = false
    private var loaded = false
    var isDemo: Bool { ProcessInfo.processInfo.arguments.contains("--gusan-vault-demo") }

    func configure(model: AppModel) {
        let key = isDemo ? "vault-demo" : model.session?.userID ?? model.session?.accessToken
        guard key != account || service == nil else { return }
        generation += 1; account = key
        loaded = false
        logins = []; events = []; pending = []; error = nil
        guard key != nil else { service = nil; return }
        if isDemo { service = StubVaultService.demo(); return }
        let expectedUser = model.session?.userID
        let auth = model.auth
        service = VaultClient(tokenProvider: {
            guard let session = try await auth.refresh(), session.userID == expectedUser else {
                throw VaultClientError(code: "unauthorized", status: 401)
            }
            return session.accessToken
        })
    }

    func refresh() async {
        guard !refreshing, !busy, let service else { return }
        refreshing = true
        if !loaded { loading = true }
        let version = generation
        defer { refreshing = false; if loading { loading = false } }
        do {
            let list = try await service.list()
            let requests = try await service.pending()
            guard version == generation, !Task.isCancelled else { return }
            if logins != list.logins { logins = list.logins }
            if events != list.events { events = list.events }
            if pending != requests { pending = requests }
            loaded = true
            if error != nil { error = nil }
        } catch {
            if version == generation, !Task.isCancelled {
                let message = Self.message(error)
                if self.error != message { self.error = message }
            }
        }
    }

    func approve(_ request: VaultPendingLogin) async {
        guard !busy, request.pageMatches, request.expiresAt > Date(), let service else { return }
        busy = true; error = nil
        let version = generation
        let context = LAContext()
        context.localizedFallbackTitle = ""
        defer { context.invalidate(); busy = false }
        do {
            var authError: NSError?
            guard context.canEvaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, error: &authError) else {
                error = "Enable Face ID and set a device passcode in Settings to use Vault."
                return
            }
            let authenticated = try await context.evaluatePolicy(.deviceOwnerAuthenticationWithBiometrics, localizedReason: "Use your saved login for \(request.hostname)")
            guard authenticated, version == generation, !Task.isCancelled else { return }
            let response = try await release(request, service: service, context: context)
            guard version == generation else { return }
            if response.errorCode != nil { error = "The secure login couldn't be shared. The task has stopped." }
            pending.removeAll { $0.id == request.id }
        } catch {
            if version == generation { self.error = Self.message(error) }
        }
    }

    private func release(_ request: VaultPendingLogin, service: VaultService, context: LAContext) async throws -> VaultTaskResponse {
        let credential = try store.read(loginID: request.loginID, context: context)
        return try await service.release(request, credential: credential)
    }

    func deny(_ request: VaultPendingLogin) async {
        guard !busy, let service else { return }
        busy = true; error = nil
        let version = generation
        defer { busy = false }
        do {
            let response = try await service.deny(request)
            guard version == generation else { return }
            pending.removeAll { $0.id == request.id }
            if response.errorCode != nil { error = "The task has stopped. Refresh Vault for its latest status." }
        } catch { if version == generation { self.error = Self.message(error) } }
    }

    func add(hostname: String, username: String, password: String, authenticator: String) async -> Bool {
        guard !busy, let service else { return false }
        busy = true; error = nil
        let version = generation
        defer { busy = false }
        do {
            let host = try VaultHostname.normalize(hostname)
            let secret = try VaultHostname.authenticatorSecret(authenticator)
            guard !password.isEmpty, password.count <= 4096, username.count <= 4096 else { throw VaultValidationError.credential }
            let previousLogins = logins
            let login = try await service.add(hostname: host, usernameHint: VaultHostname.usernameHint(username), hasTOTP: secret != nil)
            guard version == generation else { return false }
            try await VaultLoginChanges.save(VaultCredential(username: username, password: password, totp: secret),
                                             loginID: login.id, previousLogins: previousLogins, store: store, service: service)
            guard version == generation else { return false }
            logins.removeAll { $0.id == login.id }; logins.insert(login, at: 0)
            return true
        } catch { if version == generation { self.error = Self.message(error) }; return false }
    }

    func delete(_ login: VaultLogin) async {
        guard !busy, let service else { return }
        busy = true; error = nil
        let version = generation
        defer { busy = false }
        do {
            try await VaultLoginChanges.delete(loginID: login.id, store: store, service: service)
            guard version == generation else { return }
            logins.removeAll { $0.id == login.id }
        } catch { if version == generation { self.error = Self.message(error) } }
    }

    static func message(_ error: Error) -> String {
        if error as? GusanError == .unauthorized { return "Sign in again to use Vault." }
        if let error = error as? VaultValidationError { return error.localizedDescription }
        if let error = error as? VaultStoreError { return error.localizedDescription }
        if let error = error as? VaultLoginChangeError { return error.localizedDescription }
        if let error = error as? VaultClientError { return error.localizedDescription }
        if let error = error as? LAError {
            switch error.code {
            case .userCancel, .appCancel, .systemCancel: return "Login sharing cancelled. Nothing was shared."
            default: return "Face ID couldn't unlock your login. Try again or check Face ID in Settings."
            }
        }
        return "Couldn't update Vault. Check your connection and try again."
    }
}
