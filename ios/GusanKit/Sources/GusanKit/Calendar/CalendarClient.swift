import Foundation

public struct CalendarClientError: LocalizedError {
    public let code: String
    public let status: Int
    public init(code: String, status: Int = 0) { self.code = code; self.status = status }
    public var errorDescription: String? {
        switch code {
        case "google_not_configured": return "Not set up yet"
        case "google_reconnect": return "Reconnect Google Calendar"
        case "unauthorized": return "Sign in to connect Google Calendar."
        default: return "Google Calendar couldn't be updated. Try again."
        }
    }
}

public final class CalendarClient {
    public static let functionURL = URL(string: AuthClient.baseURL + "/functions/v1/calendar")!
    private let token: () async throws -> String
    private let transport: HTTPTransport
    public init(tokenProvider: @escaping () async throws -> String, transport: HTTPTransport = URLSession.shared) {
        token = tokenProvider; self.transport = transport
    }
    public convenience init(auth: AuthClient, transport: HTTPTransport = URLSession.shared) {
        self.init(tokenProvider: {
            guard let session = try await auth.refresh() else { throw CalendarClientError(code: "unauthorized", status: 401) }
            return session.accessToken
        }, transport: transport)
    }
    public static func actionRequest(_ body: [String: Any], token: String) throws -> URLRequest {
        var request = URLRequest(url: functionURL)
        request.httpMethod = "POST"; request.timeoutInterval = 45
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(AuthClient.publishableKey, forHTTPHeaderField: "apikey")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return request
    }
    private func send(_ body: [String: Any]) async throws -> Data {
        let request = try Self.actionRequest(body, token: await token())
        let (data, response) = try await transport.data(for: request)
        guard let http = response as? HTTPURLResponse else { throw CalendarClientError(code: "bad_response") }
        guard (200...299).contains(http.statusCode) else {
            let code = ((try? JSONSerialization.jsonObject(with: data)) as? [String: Any])?["error"] as? String ?? "unknown"
            let allowed = ["unauthorized", "google_not_configured", "google_reconnect", "rate_limited"]
            throw CalendarClientError(code: allowed.contains(code) ? code : "unknown", status: http.statusCode)
        }
        return data
    }
    public func start() async throws -> URL {
        struct Result: Decodable { let url: String }
        let value = try JSONDecoder().decode(Result.self, from: await send(["action": "google_start"]))
        guard let url = URL(string: value.url), url.scheme == "https", url.host == "accounts.google.com", url.user == nil, url.password == nil else { throw CalendarClientError(code: "bad_response") }
        return url
    }
    public func status() async throws -> Bool {
        struct Status: Decodable { struct Google: Decodable { let connected: Bool }; let google: Google }
        return try JSONDecoder().decode(Status.self, from: await send(["action": "status"])).google.connected
    }
    public func disconnect() async throws {
        struct Result: Decodable { let ok: Bool }
        guard try JSONDecoder().decode(Result.self, from: await send(["action": "disconnect"])).ok else { throw CalendarClientError(code: "bad_response") }
    }
    public func busy(start: Date, end: Date, includeTitles: Bool, timeZone: TimeZone = .current) async throws -> [BusyBlock] {
        let format = ISO8601DateFormatter()
        let data = try await send(["action": "busy", "start": format.string(from: start), "end": format.string(from: end), "include_titles": includeTitles])
        struct Block: Decodable { let start: String; let end: String; let all_day: Bool; let title: String? }
        struct Result: Decodable { let blocks: [Block] }
        let dateOnly = DateFormatter(); dateOnly.locale = Locale(identifier: "en_US_POSIX"); dateOnly.calendar = Calendar(identifier: .gregorian); dateOnly.timeZone = timeZone; dateOnly.dateFormat = "yyyy-MM-dd"; dateOnly.isLenient = false
        func date(_ value: String) -> Date? {
            if value.count == 10 { return dateOnly.date(from: value) }
            format.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
            if let date = format.date(from: value) { return date }
            format.formatOptions = [.withInternetDateTime]
            return format.date(from: value)
        }
        return try JSONDecoder().decode(Result.self, from: data).blocks.map { block in
            guard let from = date(block.start), let to = date(block.end), from < to else { throw CalendarClientError(code: "bad_response") }
            return BusyBlock(start: from, end: to, isAllDay: block.all_day, title: includeTitles ? block.title : nil)
        }
    }
    public static func callbackConnected(_ url: URL) -> Bool {
        guard url.scheme == "gusan", url.host == "calendar" else { return false }
        return URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first(where: { $0.name == "google" })?.value == "connected"
    }
}
