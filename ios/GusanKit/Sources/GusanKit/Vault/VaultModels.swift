import Foundation

public struct VaultLogin: Codable, Equatable, Identifiable {
    public let loginID: UUID
    public let hostname: String
    public let usernameHint: String?
    public let hasTOTP: Bool
    public let createdAt: Date
    public let lastUsedAt: Date?
    public var id: UUID { loginID }
    enum CodingKeys: String, CodingKey {
        case loginID = "login_id", hostname, usernameHint = "username_hint", hasTOTP = "has_totp"
        case createdAt = "created_at", lastUsedAt = "last_used_at"
    }
    public init(loginID: UUID, hostname: String, usernameHint: String?, hasTOTP: Bool, createdAt: Date = Date(), lastUsedAt: Date? = nil) {
        self.loginID = loginID; self.hostname = hostname; self.usernameHint = usernameHint
        self.hasTOTP = hasTOTP; self.createdAt = createdAt; self.lastUsedAt = lastUsedAt
    }
}

public struct VaultEvent: Codable, Equatable, Identifiable {
    public let event: String
    public let hostname: String
    public let taskID: UUID?
    public let loginID: UUID?
    public let at: Date
    public var id: String { "\(event):\(hostname):\(taskID?.uuidString ?? ""):\(loginID?.uuidString ?? ""):\(at.timeIntervalSince1970)" }
    enum CodingKeys: String, CodingKey { case event, hostname, taskID = "task_id", loginID = "login_id", at }
    public init(event: String, hostname: String, taskID: UUID? = nil, loginID: UUID? = nil, at: Date = Date()) {
        self.event = event; self.hostname = hostname; self.taskID = taskID; self.loginID = loginID; self.at = at
    }
    public var label: String {
        switch event {
        case "released": return "Used for \(hostname)"
        case "denied": return "Declined for \(hostname)"
        case "expired": return "Request expired for \(hostname)"
        case "added": return "Added \(hostname)"
        case "deleted": return "Deleted \(hostname)"
        case "release_requested": return "Login requested for \(hostname)"
        case "vault_created": return "Secure task vault created"
        case "vault_deleted": return "Secure task vault deleted"
        case "vault_delete_failed": return "Task vault cleanup needs a retry"
        default: return "Vault updated"
        }
    }
}

public struct VaultPendingLogin: Codable, Equatable, Identifiable {
    public let taskID: UUID
    public let title: String
    public let hostname: String
    public let pageURL: URL?
    public let loginID: UUID
    public let releaseNonce: UUID
    public let expiresAt: Date
    public var id: UUID { taskID }
    enum CodingKeys: String, CodingKey {
        case taskID = "task_id", title, hostname, pageURL = "page_url", loginID = "login_id"
        case releaseNonce = "release_nonce", expiresAt = "expires_at"
    }
    public init(taskID: UUID, title: String, hostname: String, pageURL: URL?, loginID: UUID, releaseNonce: UUID, expiresAt: Date) {
        self.taskID = taskID; self.title = title; self.hostname = hostname; self.pageURL = pageURL
        self.loginID = loginID; self.releaseNonce = releaseNonce; self.expiresAt = expiresAt
    }
    public var pageMatches: Bool {
        guard let pageURL, let host = try? VaultHostname.normalize(pageURL.absoluteString) else { return false }
        return host == hostname || host.hasSuffix("." + hostname)
    }
}

public struct VaultCredential: Codable, CustomStringConvertible, CustomDebugStringConvertible {
    public let username: String
    public let password: String
    public let totp: String?
    public init(username: String, password: String, totp: String? = nil) {
        self.username = username; self.password = password; self.totp = totp
    }
    public var description: String { "VaultCredential(<redacted>)" }
    public var debugDescription: String { description }
}

public struct VaultList: Decodable {
    public let logins: [VaultLogin]
    public let events: [VaultEvent]
    public init(logins: [VaultLogin], events: [VaultEvent]) { self.logins = logins; self.events = events }
}

public struct VaultTaskResponse: Decodable {
    public let taskID: UUID
    public let status: AgentStatus
    public let errorCode: String?
    enum CodingKeys: String, CodingKey { case taskID = "task_id", status, errorCode = "error_code" }
    public init(taskID: UUID, status: AgentStatus, errorCode: String? = nil) { self.taskID = taskID; self.status = status; self.errorCode = errorCode }
}

public enum VaultValidationError: LocalizedError {
    case hostname, authenticator, credential
    public var errorDescription: String? {
        switch self {
        case .hostname: return "Enter a site hostname or web URL, without an IP address or port."
        case .authenticator: return "Enter a base32 authenticator secret or an otpauth://totp URL."
        case .credential: return "Enter a password. Each login field must be no longer than 4096 characters."
        }
    }
}

public enum VaultHostname {
    public static func normalize(_ value: String) throws -> String {
        let input = value.trimmingCharacters(in: .whitespacesAndNewlines)
        guard input.count <= 2048 else { throw VaultValidationError.hostname }
        let address = input.contains("://") ? input : "https://" + input
        let authority = address.components(separatedBy: "://").dropFirst().joined(separator: "://").prefix { !"/?#".contains($0) }
        guard !authority.contains(":") else { throw VaultValidationError.hostname }
        guard let components = URLComponents(string: address),
              ["http", "https"].contains(components.scheme?.lowercased() ?? ""),
              components.port == nil, components.user == nil, components.password == nil,
              var host = components.url?.host?.lowercased() else { throw VaultValidationError.hostname }
        if host.hasSuffix(".") { host.removeLast() }
        let pattern = #"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z](?:[a-z0-9-]{0,61}[a-z0-9])?$"#
        guard host.range(of: pattern, options: .regularExpression) != nil else { throw VaultValidationError.hostname }
        return host
    }

    public static func usernameHint(_ username: String) -> String? {
        let value = username.trimmingCharacters(in: .whitespacesAndNewlines)
        guard let first = value.first else { return nil }
        if let at = value.lastIndex(of: "@") {
            return String((String(first) + "•••" + String(value[at...])).prefix(64))
        }
        return String(first) + "•••" + (value.count > 2 ? String(value.suffix(1)) : "")
    }

    public static func authenticatorSecret(_ value: String) throws -> String? {
        var secret = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if secret.isEmpty { return nil }
        if secret.lowercased().hasPrefix("otpauth://") {
            guard let url = URLComponents(string: secret), url.host?.lowercased() == "totp",
                  let extracted = url.queryItems?.first(where: { $0.name == "secret" })?.value else { throw VaultValidationError.authenticator }
            secret = extracted
        }
        secret = secret.replacingOccurrences(of: " ", with: "").uppercased()
        guard secret.count <= 4096, secret.range(of: #"^[A-Z2-7]+={0,6}$"#, options: .regularExpression) != nil else { throw VaultValidationError.authenticator }
        return secret
    }
}
