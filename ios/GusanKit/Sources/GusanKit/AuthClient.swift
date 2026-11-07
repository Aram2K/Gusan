import Foundation
import Security

public struct PKCE {
    public let verifier: String
    public var challenge: String { Self.base64URL(Data(SHA256.digest(Array(verifier.utf8)))) }
    public init(verifier: String) { self.verifier = verifier }
    public static func generate() throws -> PKCE {
        var bytes = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, bytes.count, &bytes) == errSecSuccess else { throw GusanError.failed }
        return PKCE(verifier: base64URL(Data(bytes)))
    }
    private static func base64URL(_ data: Data) -> String {
        data.base64EncodedString().replacingOccurrences(of: "+", with: "-").replacingOccurrences(of: "/", with: "_").replacingOccurrences(of: "=", with: "")
    }
}

public struct Entitlement: Decodable, Equatable {
    public let isPro: Bool
    public let plan: String?
    enum CodingKeys: String, CodingKey { case isPro = "is_pro", plan }
}

public final class AuthClient {
    public static let baseURL = "https://hftcelxzfoubheqeoool.supabase.co"
    public static let publishableKey = "sb_publishable_aVAVHwDgUygeu_1QHyxg5w_3GGP_k5p"
    public let transport: HTTPTransport
    public let store: SessionStorage
    public init(store: SessionStorage, transport: HTTPTransport = URLSession.shared) { self.store = store; self.transport = transport }
    public func request(path: String, body: [String: String] = [:], token: String? = nil) throws -> URLRequest {
        var request = URLRequest(url: URL(string: Self.baseURL + path)!, timeoutInterval: 10)
        request.httpMethod = "POST"
        request.setValue(Self.publishableKey, forHTTPHeaderField: "apikey")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        if let token { request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization") }
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return request
    }
    public static func authorizeURL(pkce: PKCE) -> URL {
        var url = URLComponents(string: baseURL + "/auth/v1/authorize")!
        url.queryItems = [URLQueryItem(name: "provider", value: "google"), URLQueryItem(name: "redirect_to", value: "gusan://auth-callback"), URLQueryItem(name: "code_challenge", value: pkce.challenge), URLQueryItem(name: "code_challenge_method", value: "s256")]
        return url.url!
    }
    public static func decodeSession(_ data: Data, previousRefreshToken: String? = nil, now: Date = Date()) throws -> Session {
        struct Payload: Decodable {
            struct User: Decodable {
                struct Metadata: Decodable { let email: String? }
                let id: String?; let email: String?; let user_metadata: Metadata?
            }
            let access_token: String
            let expires_in: Double?
            let refresh_token: String?
            let user: User?
        }
        guard let value = try? JSONDecoder().decode(Payload.self, from: data), !value.access_token.isEmpty,
              let refresh = value.refresh_token ?? previousRefreshToken, !refresh.isEmpty else { throw GusanError.invalidResponse }
        return Session(accessToken: value.access_token, expiresAt: now.addingTimeInterval((value.expires_in ?? 3600) - 60), refreshToken: refresh, userID: value.user?.id, email: value.user?.email ?? value.user?.user_metadata?.email, updatedAt: now)
    }
    private func send(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        do {
            let (data, response) = try await transport.data(for: request)
            guard let response = response as? HTTPURLResponse else { throw GusanError.invalidResponse }
            return (data, response)
        } catch let error as GusanError { throw error }
        catch let error as URLError { throw error.code == .timedOut ? GusanError.timeout : GusanError.network }
        catch { throw GusanError.network }
    }
    private func signIn(grant: String, body: [String: String]) async throws -> Session {
        let (data, response) = try await send(request(path: "/auth/v1/token?grant_type=" + grant, body: body))
        guard response.statusCode == 200 else { throw GusanError.signInFailed(Self.signInMessage(data)) }
        let session = try Self.decodeSession(data)
        store.write(session)
        return session
    }
    /// Same wording as auth.py sign_in_email.
    public static func signInMessage(_ data: Data) -> String {
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let message = ["msg", "error_description", "message", "error"].lazy.compactMap { json?[$0] as? String }.first { !$0.isEmpty } ?? "Sign-in failed. Try again."
        let low = message.lowercased()
        if low.contains("not confirmed") || low.contains("not been confirmed") { return "Please verify your email first - check your inbox." }
        if low.contains("invalid") || low.contains("credential") { return "Incorrect email or password." }
        return message
    }
    public func signIn(email: String, password: String) async throws -> Session {
        try await signIn(grant: "password", body: ["email": email, "password": password])
    }
    public func exchange(code: String, pkce: PKCE) async throws -> Session {
        try await signIn(grant: "pkce", body: ["auth_code": code, "code_verifier": pkce.verifier])
    }
    public static func refreshTokenRejected(status: Int, data: Data) -> Bool {
        guard [400, 401].contains(status) else { return false }
        let dead = ["refresh_token_not_found", "refresh_token_already_used", "session_not_found", "session_expired", "invalid_grant"]
        let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        return ["error_code", "error"].contains { dead.contains(json?[$0] as? String ?? "") }
            || String(decoding: data, as: UTF8.self).lowercased().contains("invalid refresh token")
    }
    public func refresh(force: Bool = false) async throws -> Session? {
        guard let before = store.read() else { return nil }
        if !force && before.expiresAt > Date() { return before }
        let (data, response) = try await send(request(path: "/auth/v1/token?grant_type=refresh_token", body: ["refresh_token": before.refreshToken]))
        let latest = store.read()
        guard latest?.refreshToken == before.refreshToken else { return latest }
        if response.statusCode != 200 {
            if Self.refreshTokenRejected(status: response.statusCode, data: data) { store.clear(); throw GusanError.unauthorized }
            throw GusanError.failed
        }
        let session = try Self.decodeSession(data, previousRefreshToken: before.refreshToken)
        store.write(session)
        return session
    }
    public func entitlement(accessToken: String) async throws -> Entitlement {
        let (data, response) = try await send(request(path: "/rest/v1/rpc/my_entitlement", token: accessToken))
        guard response.statusCode == 200 else { throw GusanError.failed }
        guard let rows = try? JSONDecoder().decode([Entitlement].self, from: data) else { throw GusanError.invalidResponse }
        return rows.first ?? Entitlement(isPro: false, plan: nil)
    }
    public func signOut() async {
        let session = store.read()
        if let token = session?.accessToken { _ = try? await send(request(path: "/auth/v1/logout", token: token)) }
        store.clear()
    }
}
