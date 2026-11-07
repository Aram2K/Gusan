import Foundation

public enum VoiceError: Error, LocalizedError, Equatable {
    case micPermissionDenied, fullAccessRequired, fallbackOff, notSignedIn, notShared, proRequired, unauthorized, dailyLimit, serverNotConfigured, tooLong, noEngine, transcriptionFailed, nothingHeard, offline, network, timeout, invalidResponse, sessionEnded
    case audioFailed(String)
    public var errorDescription: String? {
        switch self {
        case .micPermissionDenied: return "Allow microphone access in Settings to dictate."
        case .notSignedIn: return "Open the Gusan app and sign in."
        case .fullAccessRequired: return "Turn on Full Access"
        case .fallbackOff: return "Voice sharing is off. Enable pasteboard fallback in Keyboard settings."
        case .notShared: return "Open Gusan to reconnect voice sharing."
        case .proRequired: return "Voice needs Gusan Pro."
        case .unauthorized: return "Sign in again in the Gusan app."
        case .dailyLimit: return "Daily voice limit reached. Try again tomorrow."
        case .serverNotConfigured: return "Voice isn't set up on the server yet."
        case .tooLong: return "Recording is too long. Try a shorter dictation."
        case .noEngine: return "No transcription engine is available. Try again later."
        case .transcriptionFailed: return "Transcription failed. Try again."
        case .nothingHeard: return "Nothing heard. Try speaking again."
        case .offline: return "No internet connection."
        case .network: return "Network error reaching voice transcription."
        case .timeout: return "Transcription timed out. Try again."
        case .invalidResponse: return "The server returned an invalid response."
        case .audioFailed(let message): return "Audio failed: \(message)"
        case .sessionEnded: return "Voice session ended. Tap the mic to start again."
        }
    }
}

public struct TranscribeClient {
    public static let url = URL(string: "https://hftcelxzfoubheqeoool.supabase.co/functions/v1/transcribe-proxy")!
    public let transport: HTTPTransport
    public init(transport: HTTPTransport = URLSession.shared) { self.transport = transport }
    public func request(wav: Data, language: String, accessToken: String) throws -> URLRequest {
        guard wav.count > 44 else { throw VoiceError.nothingHeard }
        let audio = wav.base64EncodedString()
        guard audio.count <= 24 * 1024 * 1024 else { throw VoiceError.tooLong }
        let code = language.trimmingCharacters(in: .whitespacesAndNewlines).lowercased()
        let lang = VoiceSettings.languages.contains { $0.code == code } ? code : "auto"
        var request = URLRequest(url: Self.url, timeoutInterval: 90)
        request.httpMethod = "POST"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["audio": audio, "sample_rate": 16000, "language": lang, "provider": "gemini", "languages": lang == "auto" ? [] : [lang], "model": "fast"])
        return request
    }
    public static func parse(_ data: Data, response: HTTPURLResponse) throws -> String {
        switch response.statusCode {
        case 200: break
        case 401: throw VoiceError.unauthorized
        case 403: throw VoiceError.proRequired
        case 413: throw VoiceError.tooLong
        case 429: throw VoiceError.dailyLimit
        case 503: throw VoiceError.serverNotConfigured
        case 502:
            let body = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
            if (body?["detail"] as? String)?.localizedCaseInsensitiveContains("no engine") == true { throw VoiceError.noEngine }
            throw VoiceError.transcriptionFailed
        default: throw VoiceError.transcriptionFailed
        }
        struct Answer: Decodable { let text: String }
        guard let answer = try? JSONDecoder().decode(Answer.self, from: data) else { throw VoiceError.invalidResponse }
        let text = answer.text.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { throw VoiceError.nothingHeard }
        return text
    }
    public func run(wav: Data, language: String, accessToken: String) async throws -> String {
        let req = try request(wav: wav, language: language, accessToken: accessToken)
        do {
            let (data, response) = try await transport.data(for: req)
            guard let response = response as? HTTPURLResponse else { throw VoiceError.invalidResponse }
            return try Self.parse(data, response: response)
        } catch let error as VoiceError { throw error }
        catch is CancellationError { throw CancellationError() }
        catch let error as URLError {
            switch error.code {
            case .timedOut: throw VoiceError.timeout
            case .notConnectedToInternet, .networkConnectionLost, .dataNotAllowed: throw VoiceError.offline
            default: throw VoiceError.network
            }
        } catch { throw VoiceError.network }
    }
}
