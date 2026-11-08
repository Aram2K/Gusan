import Foundation

public protocol VaultService: AnyObject {
    func list() async throws -> VaultList
    func add(hostname: String, usernameHint: String?, hasTOTP: Bool) async throws -> VaultLogin
    func delete(loginID: UUID) async throws
    func pending() async throws -> [VaultPendingLogin]
    func release(_ pending: VaultPendingLogin, credential: VaultCredential) async throws -> VaultTaskResponse
    func deny(_ pending: VaultPendingLogin) async throws -> VaultTaskResponse
}

public struct VaultClientError: LocalizedError {
    public let code: String
    public let status: Int
    public var errorDescription: String? {
        switch code {
        case "unauthorized": return "Sign in again to use Vault."
        case "pro_required": return "Vault requires Gusan Pro."
        case "invalid_nonce", "not_awaiting_login", "not_found": return "This sign-in request is no longer available. Refresh Vault."
        case "login_mismatch": return "This login doesn't match the requested site. Deny the request and try again."
        case "login_limit": return "You can save up to 50 logins. Delete one to add another."
        case "vault_error": return "The secure login couldn't be shared. Refresh Vault and try again."
        default: return "Couldn't update Vault. Check your connection and try again."
        }
    }
}

public final class VaultClient: VaultService {
    public static let functionURL = URL(string: AuthClient.baseURL + "/functions/v1/agent-vault")!
    public typealias TokenProvider = () async throws -> String
    private let token: TokenProvider
    private let transport: HTTPTransport
    public init(tokenProvider: @escaping TokenProvider, transport: HTTPTransport = URLSession.shared) {
        self.token = tokenProvider; self.transport = transport
    }
    public convenience init(auth: AuthClient, transport: HTTPTransport = URLSession.shared) {
        self.init(tokenProvider: {
            guard let session = try await auth.refresh() else { throw VaultClientError(code: "unauthorized", status: 401) }
            return session.accessToken
        }, transport: transport)
    }
    public static func actionRequest(_ body: [String: Any], token: String) throws -> URLRequest {
        var request = URLRequest(url: functionURL)
        request.httpMethod = "POST"; request.timeoutInterval = 30
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(AuthClient.publishableKey, forHTTPHeaderField: "apikey")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return request
    }
    public static func releaseRequest(_ pending: VaultPendingLogin, credential: VaultCredential, token: String) throws -> URLRequest {
        var fields: [String: String] = [:]
        if !credential.username.isEmpty { fields["username"] = credential.username }
        if !credential.password.isEmpty { fields["password"] = credential.password }
        if let secret = credential.totp, !secret.isEmpty { fields["totp"] = secret }
        guard !fields.isEmpty, fields.values.allSatisfy({ $0.count <= 4096 }) else { throw VaultValidationError.credential }
        return try actionRequest(["action": "release", "task_id": pending.taskID.uuidString.lowercased(),
                                  "login_id": pending.loginID.uuidString.lowercased(),
                                  "release_nonce": pending.releaseNonce.uuidString.lowercased(), "values": fields], token: token)
    }
    private func send(_ request: URLRequest) async throws -> Data {
        let (data, response) = try await transport.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw VaultClientError(code: "bad_response", status: 0) }
        if !(200...299).contains(http.statusCode) {
            let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            let allowed = ["unauthorized", "pro_required", "invalid_nonce", "not_awaiting_login", "not_found", "login_mismatch", "login_limit", "vault_error"]
            let code = object?["error"] as? String ?? "unknown"
            throw VaultClientError(code: allowed.contains(code) ? code : "unknown", status: http.statusCode)
        }
        return data
    }
    private func send(_ body: [String: Any]) async throws -> Data {
        try await send(Self.actionRequest(body, token: try await token()))
    }
    public func list() async throws -> VaultList {
        try AgentTask.dateDecoder.decode(VaultList.self, from: await send(["action": "list"]))
    }
    public func add(hostname: String, usernameHint: String?, hasTOTP: Bool) async throws -> VaultLogin {
        var body: [String: Any] = ["action": "add", "hostname": try VaultHostname.normalize(hostname), "has_totp": hasTOTP]
        if let usernameHint { body["username_hint"] = String(usernameHint.prefix(64)) }
        return try AgentTask.dateDecoder.decode(VaultLogin.self, from: await send(body))
    }
    public func delete(loginID: UUID) async throws { _ = try await send(["action": "delete", "login_id": loginID.uuidString.lowercased()]) }
    public func pending() async throws -> [VaultPendingLogin] {
        struct Envelope: Decodable { let pending: [VaultPendingLogin] }
        return try AgentTask.dateDecoder.decode(Envelope.self, from: await send(["action": "pending"])).pending
    }
    public func release(_ pending: VaultPendingLogin, credential: VaultCredential) async throws -> VaultTaskResponse {
        let request = try Self.releaseRequest(pending, credential: credential, token: await token())
        return try AgentTask.dateDecoder.decode(VaultTaskResponse.self, from: await send(request))
    }
    public func deny(_ pending: VaultPendingLogin) async throws -> VaultTaskResponse {
        try AgentTask.dateDecoder.decode(VaultTaskResponse.self, from: await send(["action": "deny", "task_id": pending.taskID.uuidString.lowercased(), "release_nonce": pending.releaseNonce.uuidString.lowercased()]))
    }
}

public final class StubVaultService: VaultService {
    private var logins: [VaultLogin]
    private var events: [VaultEvent]
    private var requests: [VaultPendingLogin]
    private let lock = NSLock()
    public init(logins: [VaultLogin] = [], events: [VaultEvent] = [], pending: [VaultPendingLogin] = []) {
        self.logins = logins; self.events = events; self.requests = pending
    }
    public static func demo() -> StubVaultService {
        let login = VaultLogin(loginID: UUID(), hostname: "booking.com", usernameHint: "a•••@gmail.com", hasTOTP: true, lastUsedAt: Date().addingTimeInterval(-3600))
        let request = VaultPendingLogin(taskID: UUID(), title: "Find my upcoming hotel booking", hostname: login.hostname, pageURL: URL(string: "https://account.booking.com/sign-in"), loginID: login.id, releaseNonce: UUID(), expiresAt: Date().addingTimeInterval(900))
        return StubVaultService(logins: [login], events: [VaultEvent(event: "released", hostname: login.hostname)], pending: [request])
    }
    private func locked<T>(_ body: () throws -> T) rethrows -> T { lock.lock(); defer { lock.unlock() }; return try body() }
    public func list() async throws -> VaultList { locked { VaultList(logins: logins, events: events) } }
    public func pending() async throws -> [VaultPendingLogin] { locked { requests } }
    public func add(hostname: String, usernameHint: String?, hasTOTP: Bool) async throws -> VaultLogin {
        let host = try VaultHostname.normalize(hostname)
        return locked {
            let login = VaultLogin(loginID: logins.first(where: { $0.hostname == host })?.id ?? UUID(), hostname: host, usernameHint: usernameHint, hasTOTP: hasTOTP)
            logins.removeAll { $0.hostname == host }; logins.append(login)
            events.insert(VaultEvent(event: "added", hostname: host), at: 0)
            return login
        }
    }
    public func delete(loginID: UUID) async throws {
        locked {
            if let login = logins.first(where: { $0.id == loginID }) { events.insert(VaultEvent(event: "deleted", hostname: login.hostname), at: 0) }
            logins.removeAll { $0.id == loginID }
        }
    }
    public func release(_ pending: VaultPendingLogin, credential: VaultCredential) async throws -> VaultTaskResponse { finish(pending, event: "released") }
    public func deny(_ pending: VaultPendingLogin) async throws -> VaultTaskResponse { finish(pending, event: "denied") }
    private func finish(_ pending: VaultPendingLogin, event: String) -> VaultTaskResponse {
        locked {
            requests.removeAll { $0.id == pending.id }
            events.insert(VaultEvent(event: event, hostname: pending.hostname, taskID: pending.id, loginID: pending.loginID), at: 0)
            return VaultTaskResponse(taskID: pending.id, status: .running)
        }
    }
}
