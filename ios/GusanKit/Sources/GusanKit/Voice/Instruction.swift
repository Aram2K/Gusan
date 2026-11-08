import Foundation

extension SmartActionClient {
    public func request(instruction: String, text: String, accessToken: String) throws -> URLRequest {
        guard !instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty, !text.isEmpty else { throw GusanError.emptyInput }
        var request = URLRequest(url: Self.url, timeoutInterval: 45)
        request.httpMethod = "POST"
        request.setValue("Bearer \(accessToken)", forHTTPHeaderField: "Authorization")
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        let messages = [ChatMessage(role: "system", content: "You edit text for the user. Apply the user's spoken instruction to the text and output only the resulting text - no commentary, no quotes. Keep the text's language unless the instruction says otherwise."), ChatMessage(role: "user", content: "Instruction:\n\(instruction)\n\nText:\n\(text)")]
        let calendar = CalendarContext.schedulingSummary(texts: [instruction, text], store: calendarStore)
        struct Body: Encodable { let messages: [ChatMessage]; let max_tokens: Int; let mode: String; let calendar: String? }
        request.httpBody = try JSONEncoder().encode(Body(messages: messages, max_tokens: 2000, mode: "smart_auto", calendar: calendar))
        return request
    }
    public func run(instruction: String, text: String, accessToken: String) async throws -> String {
        let req = try request(instruction: instruction, text: text, accessToken: accessToken)
        do {
            let (data, response) = try await transport.data(for: req)
            guard let response = response as? HTTPURLResponse else { throw GusanError.invalidResponse }
            return try Self.parse(data, response: response)
        } catch let error as GusanError { throw error }
        catch let error as URLError { throw error.code == .timedOut ? GusanError.timeout : GusanError.network }
        catch { throw GusanError.network }
    }
}
