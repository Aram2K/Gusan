import Foundation

public struct JobCard: Codable, Equatable, Sendable {
    public let title: String
    public let body: String
    public let insertableText: String?
    public init(title: String, body: String, insertableText: String? = nil) {
        self.title = title; self.body = body; self.insertableText = insertableText
    }
}

public struct JobResult: Codable, Equatable, Sendable {
    public enum Kind: String, Codable, Sendable { case replaceSelection, replaceContext, insert, cardOnly }
    public let text: String
    public let kind: Kind
    public let card: JobCard?
    public init(text: String, kind: Kind) { self.text = text; self.kind = kind; card = nil }
    public init(card: JobCard) { self.card = card; text = card.insertableText ?? ""; kind = .cardOnly }
}

public enum JobEvent: Equatable, Sendable {
    case progress(String)
    case needsConfirmation(JobConfirmation)
    case result(JobResult)
}

public protocol GusanJob {
    func run() -> AsyncThrowingStream<JobEvent, Error>
}

public extension GusanJob {
    func result() async throws -> JobResult {
        try await JobRunner().result(of: self)
    }
}

public struct SmartActionJob: GusanJob {
    private let text: String
    private let action: SmartAction
    private let accessToken: String
    private let target: String
    private let kind: JobResult.Kind
    private let client: SmartActionClient
    public init(text: String, action: SmartAction, accessToken: String, target: String = "en", kind: JobResult.Kind, client: SmartActionClient = SmartActionClient()) {
        self.text = text; self.action = action; self.accessToken = accessToken; self.target = target; self.kind = kind; self.client = client
    }
    public func run() -> AsyncThrowingStream<JobEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task.detached {
                do {
                    try Task.checkCancellation()
                    continuation.yield(.progress("Thinking…"))
                    let answer = try await client.run(text: text, action: action, accessToken: accessToken, target: target)
                    try Task.checkCancellation()
                    let result: JobResult
                    if case .voiceSmart(let transcript) = action { result = VoiceSmartResponse.parse(answer, transcript: transcript) }
                    else { result = JobResult(text: answer, kind: kind) }
                    guard !result.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw GusanError.invalidResponse }
                    continuation.yield(.result(result)); continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}

public struct VoiceJob: GusanJob {
    private let transcript: String
    private let context: VoiceContext?
    private let action: VoiceAction
    private let accessToken: String
    private let target: String
    private let client: SmartActionClient
    public init(transcript: String, context: VoiceContext?, action: VoiceAction = .automatic, accessToken: String, target: String = "en", client: SmartActionClient = SmartActionClient()) {
        self.transcript = transcript; self.context = context; self.action = action; self.accessToken = accessToken; self.target = target; self.client = client
    }
    public func run() -> AsyncThrowingStream<JobEvent, Error> {
        let plan = VoicePlan.plan(transcript: transcript, context: context, action: action)
        guard !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else {
            return AsyncThrowingStream { $0.finish(throwing: GusanError.emptyInput) }
        }
        let smartAction: SmartAction
        let text: String
        let kind: JobResult.Kind
        switch plan {
        case .insert(let value):
            return AsyncThrowingStream { $0.yield(.result(JobResult(text: value, kind: .insert))); $0.finish() }
        case .smart(let mode, let value): smartAction = .mode(mode); text = value; kind = .insert
        case .instruction(let instruction, let value): smartAction = .custom(instruction: instruction); text = value; kind = .replaceSelection
        case .automatic(let action, let value, let destination): smartAction = action; text = value; kind = destination
        }
        return SmartActionJob(text: text, action: smartAction, accessToken: accessToken, target: target, kind: kind, client: client).run()
    }
}

public enum VoiceSmartResponse {
    public static func parse(_ answer: String, transcript: String) -> JobResult {
        struct Response: Decodable { let kind: String; let text: String }
        var start: String.Index?
        var depth = 0
        var quoted = false
        var escaped = false
        for index in answer.indices {
            let character = answer[index]
            if start == nil {
                if character == "{" { start = index; depth = 1 }
                continue
            }
            if quoted {
                if escaped { escaped = false }
                else if character == "\\" { escaped = true }
                else if character == "\"" { quoted = false }
            } else if character == "\"" { quoted = true }
            else if character == "{" { depth += 1 }
            else if character == "}" {
                depth -= 1
                if depth == 0, let start {
                    let data = Data(answer[start...index].utf8)
                    if let response = try? JSONDecoder().decode(Response.self, from: data), ["edit", "insert"].contains(response.kind), !response.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty {
                        return JobResult(text: response.text, kind: response.kind == "edit" ? .replaceContext : .insert)
                    }
                    break
                }
            }
        }
        return JobResult(text: transcript.trimmingCharacters(in: .whitespacesAndNewlines), kind: .insert)
    }
}

public struct VoiceEdit: Equatable {
    public let advanceUTF16: Int
    public let deleteCount: Int
    public let original: String
    public let restoreUTF16: Int
    public static func containsInsertedResult(_ text: String, context: VoiceContext, snapshot: VoiceContext) -> Bool {
        guard let documentID = snapshot.documentID, documentID == context.documentID, context.selected.isEmpty, !text.isEmpty, snapshot.selected != text else { return false }
        return context.before == snapshot.before + text && context.after == snapshot.after
    }
    public static func plan(result: JobResult, context: VoiceContext) -> VoiceEdit? {
        guard !result.text.isEmpty else { return nil }
        switch result.kind {
        case .replaceSelection:
            guard !context.selected.isEmpty else { return nil }
            return VoiceEdit(advanceUTF16: 0, deleteCount: 0, original: context.selected, restoreUTF16: 0)
        case .replaceContext:
            guard context.selected.isEmpty else { return nil }
            return VoiceEdit(advanceUTF16: context.after.utf16.count, deleteCount: (context.before + context.after).count, original: context.before + context.after, restoreUTF16: context.after.utf16.count)
        case .insert:
            guard context.selected.isEmpty else { return nil }
            return VoiceEdit(advanceUTF16: 0, deleteCount: 0, original: "", restoreUTF16: 0)
        case .cardOnly: return nil
        }
    }
}
