import Foundation

public enum SmartActionMode: String, Codable, CaseIterable {
    case smartAuto = "smart_auto", writeEmail = "write_email", makeTodoList = "make_todo_list"
    case translate, summarize

    public var label: String {
        switch self {
        case .smartAuto: return "Smart Auto"
        case .writeEmail: return "Write Email"
        case .makeTodoList: return "Make Todo List"
        case .translate: return "Translate"
        case .summarize: return "Summarize"
        }
    }
    public var maxTokens: Int {
        switch self {
        case .smartAuto, .writeEmail: return 2000
        case .makeTodoList: return 1200
        case .summarize: return 1500
        case .translate: return 1000
        }
    }
}

public struct ChatMessage: Codable, Equatable {
    public let role: String
    public let content: String
}

public enum Prompts {
    public static let translateTargets = ["en": "English", "hy": "Armenian", "ru": "Russian", "fr": "French", "de": "German", "es": "Spanish", "it": "Italian", "pt": "Portuguese", "ar": "Arabic"]
    public static func normalizeTarget(_ target: String) -> String {
        translateTargets[target] == nil ? "en" : target
    }
    static let templates: [String: [ChatMessage]] = {
        let url = Bundle.module.url(forResource: "prompts", withExtension: "json")!
        return try! JSONDecoder().decode([String: [ChatMessage]].self, from: Data(contentsOf: url))
    }()
    public static func messages(text: String, mode: SmartActionMode, target: String = "en") -> [ChatMessage] {
        var messages = templates[mode.rawValue]!
        let last = messages.removeLast()
        messages.append(ChatMessage(role: last.role, content: last.content
            .replacingOccurrences(of: "{target}", with: normalizeTarget(target))
            .replacingOccurrences(of: "{text}", with: text.trimmingCharacters(in: .whitespacesAndNewlines))))
        return messages
    }
}

public enum GusanError: Error, LocalizedError, Equatable {
    case emptyInput, unauthorized, proRequired, dailyLimit, serverNotConfigured, failed, tooLong
    case network, timeout, invalidResponse, thoughtOnly
    public var errorDescription: String? {
        switch self {
        case .emptyInput: return "Enter some text first."
        case .unauthorized: return "Sign in again in the Gusan app"
        case .proRequired: return "Gusan Pro is required for this cloud action."
        case .dailyLimit: return "Daily safety limit reached (1,000 cloud actions a day). It resets at midnight UTC."
        case .serverNotConfigured: return "Managed Smart Actions aren't set up on the server yet."
        case .failed: return "Smart Action failed, try again"
        case .tooLong: return "Text is too long. Shorten it and try again."
        case .network: return "Network error reaching Smart Actions."
        case .timeout: return "The request timed out. Try again."
        case .invalidResponse: return "The server returned an invalid response."
        case .thoughtOnly: return "The AI model spent its whole answer thinking and never answered - ask again."
        }
    }
}

public protocol HTTPTransport {
    func data(for request: URLRequest) async throws -> (Data, URLResponse)
}
extension URLSession: HTTPTransport {
    public func data(for request: URLRequest) async throws -> (Data, URLResponse) {
        try await data(for: request, delegate: nil)
    }
}

public struct SmartActionClient {
    public static let url = URL(string: "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/smart-action")!
    public let transport: HTTPTransport
    public init(transport: HTTPTransport = URLSession.shared) { self.transport = transport }
    public func request(text: String, mode: SmartActionMode, accessToken: String, target: String = "en") throws -> URLRequest {
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw GusanError.emptyInput }
        var request = URLRequest(url: Self.url, timeoutInterval: 45)
        request.httpMethod = "POST"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        struct Body: Encodable { let messages: [ChatMessage]; let max_tokens: Int; let mode: String }
        request.httpBody = try JSONEncoder().encode(Body(messages: Prompts.messages(text: text, mode: mode, target: target), max_tokens: mode.maxTokens, mode: mode.rawValue))
        return request
    }
    public func run(text: String, mode: SmartActionMode, accessToken: String, target: String = "en") async throws -> String {
        let req = try request(text: text, mode: mode, accessToken: accessToken, target: target)
        do {
            let (data, response) = try await transport.data(for: req)
            guard let response = response as? HTTPURLResponse else { throw GusanError.invalidResponse }
            return try Self.parse(data, response: response)
        } catch let error as GusanError { throw error }
        catch let error as URLError { throw error.code == .timedOut ? GusanError.timeout : GusanError.network }
        catch { throw GusanError.network }
    }
    public static func parse(_ data: Data, response: HTTPURLResponse) throws -> String {
        switch response.statusCode {
        case 200: break
        case 401: throw GusanError.unauthorized
        case 402, 403: throw GusanError.proRequired
        case 413: throw GusanError.tooLong
        case 429: throw GusanError.dailyLimit
        case 503: throw GusanError.serverNotConfigured
        default: throw GusanError.failed
        }
        struct Answer: Decodable { let text: String }
        guard let answer = try? JSONDecoder().decode(Answer.self, from: data) else { throw GusanError.invalidResponse }
        let family = response.value(forHTTPHeaderField: "X-Answer-Family") ?? ""
        let text = stripThink(answer.text, templateOpened: ["deepseek", "qwen"].contains(family))
        if !answer.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && text.isEmpty { throw GusanError.thoughtOnly }
        return text
    }
    public static func stripThink(_ text: String, templateOpened: Bool = false) -> String {
        let lead = text.trimmingCharacters(in: .whitespacesAndNewlines)
        if lead.hasPrefix("<think>") {
            guard let close = lead.range(of: "</think>") else { return "" }
            return String(lead[close.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
        }
        if templateOpened {
            var start = text.startIndex
            while let close = text.range(of: "</think>", range: start..<text.endIndex) {
                let before = String(text[..<close.lowerBound])
                let fences = before.components(separatedBy: "\n").filter { $0.range(of: "^[ \\t]*```", options: .regularExpression) != nil }.count
                let line = (before.components(separatedBy: "\n").last ?? "").replacingOccurrences(of: "```", with: "")
                if fences % 2 == 0 && line.filter({ $0 == "`" }).count % 2 == 0 {
                    return String(text[close.upperBound...]).trimmingCharacters(in: .whitespacesAndNewlines)
                }
                start = close.upperBound
            }
        }
        return lead
    }
}
