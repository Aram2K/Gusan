import XCTest
import LocalAuthentication
import Security
@testable import GusanKit

final class VaultTests: XCTestCase {
    final class Keychain: VaultKeychain {
        var added: [String: Any] = [:]
        var read: [String: Any] = [:]
        var deleted: [[String: Any]] = []
        var updates: [[String: Any]] = []
        var updateQuery: [String: Any] = [:]
        var addCount = 0
        var updateStatus: OSStatus?
        var addStatus: OSStatus?
        var status: OSStatus = errSecSuccess
        var data: Data?
        func add(_ query: [String: Any]) -> OSStatus {
            added = query; addCount += 1
            let result = addStatus ?? status
            if result == errSecSuccess { data = query[kSecValueData as String] as? Data }
            return result
        }
        func update(_ query: [String: Any], attributes: [String: Any]) -> OSStatus {
            updateQuery = query; updates.append(attributes)
            let result = updateStatus ?? (status == errSecSuccess && data == nil ? errSecItemNotFound : status)
            if result == errSecSuccess { data = attributes[kSecValueData as String] as? Data }
            return result
        }
        func copy(_ query: [String: Any]) -> (OSStatus, Data?) { read = query; return (status, data) }
        func delete(_ query: [String: Any]) -> OSStatus {
            deleted.append(query)
            if status == errSecSuccess { data = nil }
            return status
        }
    }
    final class Service: VaultService {
        let stub = StubVaultService()
        var deleted: [UUID] = []
        var deleteFails = false
        var onDelete: (() -> Void)?
        func list() async throws -> VaultList { try await stub.list() }
        func add(hostname: String, usernameHint: String?, hasTOTP: Bool) async throws -> VaultLogin {
            try await stub.add(hostname: hostname, usernameHint: usernameHint, hasTOTP: hasTOTP)
        }
        func delete(loginID: UUID) async throws {
            deleted.append(loginID); onDelete?()
            if deleteFails { throw VaultClientError(code: "unknown", status: 500) }
            try await stub.delete(loginID: loginID)
        }
        func pending() async throws -> [VaultPendingLogin] { try await stub.pending() }
        func release(_ pending: VaultPendingLogin, credential: VaultCredential) async throws -> VaultTaskResponse {
            try await stub.release(pending, credential: credential)
        }
        func deny(_ pending: VaultPendingLogin) async throws -> VaultTaskResponse { try await stub.deny(pending) }
    }
    func testReplacementUpdatesWithoutDeletingOldItem() throws {
        let fake = Keychain(), store = VaultStore(keychain: fake), id = UUID()
        try store.save(VaultCredential(username: "u", password: "old"), loginID: id)
        try store.save(VaultCredential(username: "u", password: "new"), loginID: id)
        XCTAssertEqual(fake.addCount, 1)
        XCTAssertTrue(fake.deleted.isEmpty)
        XCTAssertEqual(try store.read(loginID: id, context: LAContext()).password, "new")
        XCTAssertNotNil(fake.updates.last?[kSecAttrAccessControl as String])
        XCTAssertNil(fake.updates.last?[kSecAttrAccessGroup as String])
        XCTAssertEqual(fake.updateQuery[kSecAttrAccount as String] as? String, id.uuidString.lowercased())
        XCTAssertEqual(fake.updateQuery[kSecAttrSynchronizable as String] as? Bool, false)
    }
    func testFailedReplacementPreservesOldCredentialAndCloudRow() async throws {
        let fake = Keychain(), store = VaultStore(keychain: fake), service = Service()
        let login = try await service.add(hostname: "example.com", usernameHint: nil, hasTOTP: false)
        try store.save(VaultCredential(username: "u", password: "old"), loginID: login.id)
        fake.updateStatus = errSecParam
        do {
            try await VaultLoginChanges.save(VaultCredential(username: "u", password: "new"), loginID: login.id,
                                             existingLoginIDs: [login.id], store: store, service: service)
            XCTFail("Expected replacement failure")
        } catch { XCTAssertTrue(error is VaultStoreError) }
        XCTAssertTrue(service.deleted.isEmpty)
        XCTAssertTrue(fake.deleted.isEmpty)
        XCTAssertEqual(fake.addCount, 1)
        XCTAssertEqual(try store.read(loginID: login.id, context: LAContext()).password, "old")
        let list = try await service.list()
        XCTAssertEqual(list.logins.map(\.id), [login.id])
    }
    func testNewLoginSaveRollsBackOnlyOnFailure() async throws {
        let fake = Keychain(), store = VaultStore(keychain: fake), service = Service()
        let login = try await service.add(hostname: "example.com", usernameHint: nil, hasTOTP: false)
        fake.addStatus = errSecNotAvailable
        do {
            try await VaultLoginChanges.save(VaultCredential(username: "u", password: "p"), loginID: login.id,
                                             existingLoginIDs: [], store: store, service: service)
            XCTFail("Expected save failure")
        } catch { XCTAssertEqual(error.localizedDescription, "Set a device passcode to use Vault") }
        XCTAssertEqual(service.deleted, [login.id])
        let rolledBack = try await service.list()
        XCTAssertTrue(rolledBack.logins.isEmpty)
        XCTAssertNil(fake.data)
        XCTAssertTrue(fake.deleted.isEmpty)
        fake.addStatus = nil
        let saved = try await service.add(hostname: "example.com", usernameHint: nil, hasTOTP: false)
        try await VaultLoginChanges.save(VaultCredential(username: "u", password: "p"), loginID: saved.id,
                                         existingLoginIDs: [], store: store, service: service)
        XCTAssertEqual(service.deleted, [login.id])
        XCTAssertEqual(try store.read(loginID: saved.id, context: LAContext()).password, "p")
    }
    func testCloudDeleteMustSucceedBeforeKeychainDelete() async throws {
        let fake = Keychain(), store = VaultStore(keychain: fake), service = Service()
        let login = try await service.add(hostname: "example.com", usernameHint: nil, hasTOTP: false)
        try store.save(VaultCredential(username: "u", password: "p"), loginID: login.id)
        service.onDelete = { XCTAssertNotNil(fake.data); XCTAssertTrue(fake.deleted.isEmpty) }
        service.deleteFails = true
        do {
            try await VaultLoginChanges.delete(loginID: login.id, store: store, service: service)
            XCTFail("Expected cloud delete failure")
        } catch { XCTAssertTrue(error is VaultClientError) }
        XCTAssertTrue(fake.deleted.isEmpty)
        XCTAssertEqual(try store.read(loginID: login.id, context: LAContext()).password, "p")
        let unchanged = try await service.list()
        XCTAssertEqual(unchanged.logins.map(\.id), [login.id])
        service.deleteFails = false
        try await VaultLoginChanges.delete(loginID: login.id, store: store, service: service)
        XCTAssertEqual(fake.deleted.count, 1)
        XCTAssertNil(fake.data)
        let removed = try await service.list()
        XCTAssertTrue(removed.logins.isEmpty)
    }
    func testRollbackFailureSurfacesStorageAndCloudError() async throws {
        let fake = Keychain(), store = VaultStore(keychain: fake), service = Service()
        let login = try await service.add(hostname: "example.com", usernameHint: nil, hasTOTP: false)
        fake.addStatus = errSecNotAvailable; service.deleteFails = true
        do {
            try await VaultLoginChanges.save(VaultCredential(username: "u", password: "p"), loginID: login.id,
                                             existingLoginIDs: [], store: store, service: service)
            XCTFail("Expected rollback failure")
        } catch {
            XCTAssertTrue(error is VaultLoginChangeError)
            XCTAssertTrue(error.localizedDescription.contains("Set a device passcode to use Vault"))
            XCTAssertTrue(error.localizedDescription.contains("cloud entry couldn't be removed"))
        }
        XCTAssertNil(fake.data)
        let retained = try await service.list()
        XCTAssertEqual(retained.logins.map(\.id), [login.id])
    }
    func testPrivateBiometricKeychainQueries() throws {
        let fake = Keychain(), store = VaultStore(keychain: fake), id = UUID()
        try store.save(VaultCredential(username: "private-user", password: "private-password"), loginID: id)
        XCTAssertNil(fake.added[kSecAttrAccessGroup as String])
        XCTAssertNotNil(fake.added[kSecAttrAccessControl as String])
        XCTAssertEqual(fake.added[kSecAttrService as String] as? String, "Gusan.vault.login")
        XCTAssertTrue(VaultStore.service.hasPrefix("Gusan.vault."))
        XCTAssertEqual(fake.added[kSecAttrSynchronizable as String] as? Bool, false)
        XCTAssertEqual(fake.added[kSecAttrAccount as String] as? String, id.uuidString.lowercased())
        XCTAssertEqual(fake.added[kSecClass as String] as? String, kSecClassGenericPassword as String)
        let context = LAContext()
        XCTAssertEqual(try store.read(loginID: id, context: context).password, "private-password")
        XCTAssertTrue(fake.read[kSecUseAuthenticationContext as String] as? LAContext === context)
        XCTAssertNil(fake.read[kSecAttrAccessGroup as String])
        try store.delete(loginID: id); try store.deleteAll()
        XCTAssertEqual(fake.deleted.count, 2)
        XCTAssertNil(fake.deleted[1][kSecAttrAccount as String])
        XCTAssertEqual(fake.deleted[1][kSecAttrService as String] as? String, VaultStore.service)
    }
    func testPasscodeErrorsAreActionable() {
        let fake = Keychain(), store = VaultStore(keychain: fake)
        for status in [errSecDecode, errSecNotAvailable, errSecAuthFailed] {
            fake.status = status
            XCTAssertThrowsError(try store.save(VaultCredential(username: "u", password: "p"), loginID: UUID())) { error in
                XCTAssertEqual(error.localizedDescription, "Set a device passcode to use Vault")
            }
        }
    }
    func testSessionServicesCannotOverlapVault() throws {
        let sourceRoot = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("Sources/GusanKit")
        for file in ["SessionStore.swift", "SessionBackend.swift"] {
            let source = try String(contentsOf: sourceRoot.appendingPathComponent(file))
            XCTAssertFalse(source.contains("Gusan.vault"))
        }
        let backend = KeychainSessionBackend(group: nil, enabled: false)
        XCTAssertFalse(backend.name.hasPrefix("Gusan.vault"))
        for key in ["session", "report.app", "report.keyboard", "probe.app", "probe.keyboard"] {
            XCTAssertFalse(("Gusan." + key).hasPrefix(VaultStore.service))
        }
    }
    func testCredentialDescriptionsAreRedacted() {
        let credential = VaultCredential(username: "private-user", password: "private-password", totp: "PRIVATESECRET")
        for description in [String(describing: credential), String(reflecting: credential)] {
            XCTAssertTrue(description.contains("redacted"))
            for secret in [credential.username, credential.password, credential.totp!] { XCTAssertFalse(description.contains(secret)) }
        }
    }
    func testHostnamesHintsAndAuthenticator() throws {
        XCTAssertEqual(try VaultHostname.normalize("HTTPS://Example.COM/login?secret=1#anchor"), "example.com")
        XCTAssertEqual(try VaultHostname.normalize(" sub.example.com. "), "sub.example.com")
        for value in ["127.0.0.1", "127.1", "https://[::1]", "example.com:443", "https://example.com:443", "https://example.com:/", "localhost", "bad_host.com", "ftp://example.com", "https://u:p@example.com", "-bad.com", ""] {
            XCTAssertThrowsError(try VaultHostname.normalize(value), value)
        }
        XCTAssertEqual(VaultHostname.usernameHint("alex@gmail.com"), "a•••@gmail.com")
        XCTAssertEqual(VaultHostname.usernameHint("john"), "j•••n")
        XCTAssertNil(VaultHostname.usernameHint(""))
        XCTAssertEqual(try VaultHostname.authenticatorSecret("otpauth://totp/Test?secret=JBSWY3DPEHPK3PXP"), "JBSWY3DPEHPK3PXP")
        XCTAssertEqual(try VaultHostname.authenticatorSecret("jbsw y3dp"), "JBSWY3DP")
        XCTAssertThrowsError(try VaultHostname.authenticatorSecret("not-a-secret!"))
    }
    func testReleaseBodyAllowlist() throws {
        let pending = samplePending()
        let request = try VaultClient.releaseRequest(pending, credential: VaultCredential(username: "u", password: "p", totp: "JBSWY3DP"), token: "test-token")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any])
        XCTAssertEqual(Set(body.keys), Set(["action", "task_id", "login_id", "release_nonce", "values"]))
        XCTAssertEqual(body["login_id"] as? String, pending.loginID.uuidString.lowercased())
        XCTAssertEqual(body["task_id"] as? String, pending.taskID.uuidString.lowercased())
        XCTAssertEqual(body["release_nonce"] as? String, pending.releaseNonce.uuidString.lowercased())
        XCTAssertEqual(Set((body["values"] as? [String: String])?.keys.map { $0 } ?? []), Set(["username", "password", "totp"]))
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer test-token")
        XCTAssertEqual(request.url, VaultClient.functionURL)
        let without = try VaultClient.releaseRequest(pending, credential: VaultCredential(username: "", password: "p"), token: "t")
        let minimal = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(without.httpBody)) as? [String: Any])
        XCTAssertEqual(minimal["values"] as? [String: String], ["password": "p"])
    }
    func testDecodingAndDomainWarning() throws {
        let loginID = UUID(), taskID = UUID(), nonce = UUID()
        let list = Data("""
        {"logins":[{"login_id":"\(loginID)","hostname":"example.com","username_hint":"a•••","has_totp":true,"created_at":"2026-11-08T14:02:00Z","last_used_at":null}],"events":[{"event":"released","hostname":"example.com","task_id":"\(taskID)","login_id":"\(loginID)","at":"2026-11-08T14:02:00.123Z"}]}
        """.utf8)
        XCTAssertEqual(try AgentTask.dateDecoder.decode(VaultList.self, from: list).logins[0].id, loginID)
        struct Pending: Decodable { let pending: [VaultPendingLogin] }
        let data = Data("""
        {"pending":[{"task_id":"\(taskID)","title":"Booking","hostname":"example.com","page_url":"https://app.example.com/login","login_id":"\(loginID)","release_nonce":"\(nonce)","expires_at":"2026-11-08T14:17:00Z"}]}
        """.utf8)
        let decoded = try AgentTask.dateDecoder.decode(Pending.self, from: data).pending[0]
        XCTAssertEqual(decoded.releaseNonce, nonce); XCTAssertTrue(decoded.pageMatches)
        XCTAssertFalse(samplePending(url: "https://example.com.attacker.net/login").pageMatches)
        XCTAssertFalse(samplePending(url: "https://notexample.com").pageMatches)
        let status = try JSONDecoder().decode(AgentStatus.self, from: Data("\"awaiting_login\"".utf8))
        XCTAssertEqual(status.label, "Needs a login"); XCTAssertFalse(status.isTerminal)
    }
    private func samplePending(url: String = "https://example.com/login") -> VaultPendingLogin {
        VaultPendingLogin(taskID: UUID(), title: "Booking", hostname: "example.com", pageURL: URL(string: url), loginID: UUID(), releaseNonce: UUID(), expiresAt: Date().addingTimeInterval(900))
    }
}
