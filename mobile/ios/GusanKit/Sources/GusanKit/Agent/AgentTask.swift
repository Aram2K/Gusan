import Foundation

/// Wire contract of the Gusan Cloud `agent-action` edge function and the `agent_tasks` table.
public enum AgentMode: String, Codable, CaseIterable {
    case research, `do`
    public var label: String { self == .research ? "Research" : "Do it" }
}

public enum AgentStatus: String, Codable {
    case awaitingLogin = "awaiting_login"
    case queued, running, awaitingConfirmation = "awaiting_confirmation", completed, failed, cancelled, timedOut = "timed_out"
    public var isTerminal: Bool { [.completed, .failed, .cancelled, .timedOut].contains(self) }
    public var label: String {
        switch self {
        case .queued: return "Queued"
        case .running: return "Working"
        case .awaitingConfirmation: return "Needs your OK"
        case .awaitingLogin: return "Needs a login"
        case .completed: return "Done"
        case .failed: return "Failed"
        case .cancelled: return "Cancelled"
        case .timedOut: return "Timed out"
        }
    }
}

public enum AgentPhase: String, Codable {
    case queued, working, needsConfirmation, needsLogin, needsCode, completed, failed, cancelled, stopped, timedOut
    public var label: String {
        switch self {
        case .needsLogin: return "Needs a login"
        case .needsCode: return "Needs a code"
        case .stopped: return "Stopped"
        case .working: return "Working"
        case .needsConfirmation: return "Needs your OK"
        case .completed: return "Done"
        case .queued: return "Queued"
        case .failed: return "Failed"
        case .cancelled: return "Cancelled"
        case .timedOut: return "Timed out"
        }
    }
}

public struct AgentStep: Codable, Equatable {
    public let at: Date?
    public let text: String
    public init(at: Date? = nil, text: String) { self.at = at; self.text = text }
}

public struct AgentConfirmation: Codable, Equatable {
    public let summary: String
    public let actionKind: String?
    public let startURL: URL?
    public let steps: [String]
    enum CodingKeys: String, CodingKey { case summary, actionKind = "action_kind", startURL = "start_url", steps }
    public init(summary: String, actionKind: String? = nil, startURL: URL? = nil, steps: [String] = []) {
        self.summary = summary; self.actionKind = actionKind; self.startURL = startURL; self.steps = steps
    }
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        summary = (try? c.decode(String.self, forKey: .summary)) ?? ""
        actionKind = try? c.decode(String.self, forKey: .actionKind)
        startURL = try? c.decode(URL.self, forKey: .startURL)
        steps = (try? c.decode([String].self, forKey: .steps)) ?? []
    }
    public var kindLabel: String {
        switch actionKind {
        case "submit": return "Submit a form"
        case "purchase": return "Make a purchase"
        case "send": return "Send a message"
        case "delete": return "Delete something"
        case "account_change": return "Change an account"
        default: return "Take an action"
        }
    }
}

public struct AgentLink: Codable, Equatable, Hashable {
    public let title: String?
    public let url: URL
    public init(title: String?, url: URL) { self.title = title; self.url = url }
}

public struct AgentResult: Codable, Equatable {
    public var markdown: String
    public var links: [AgentLink]
    public var images: [URL]
    public init(markdown: String, links: [AgentLink] = [], images: [URL] = []) { self.markdown = markdown; self.links = links; self.images = images }
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        markdown = (try? container.decode(String.self, forKey: .markdown)) ?? ""
        links = ((try? container.decode([AgentLink?].self, forKey: .links)) ?? []).compactMap { $0 }
        images = ((try? container.decode([URL?].self, forKey: .images)) ?? []).compactMap { $0 }
    }
    /// Plain text suitable for pasting at the cursor.
    public var plainText: String { MarkdownBlocks.plainText(markdown) }
}

public struct AgentTask: Codable, Equatable, Identifiable {
    public let id: UUID
    public var mode: AgentMode
    public var provider: AgentProvider = .h
    public var status: AgentStatus {
        didSet { rawStatus = status.rawValue }
    }
    public private(set) var rawStatus: String
    public var phase: AgentPhase {
        switch rawStatus {
        case "awaiting_login": return .needsLogin
        case "awaiting_code": return .needsCode
        case "interrupted": return .stopped
        default:
            switch status {
            case .queued: return .queued
            case .running: return .working
            case .awaitingConfirmation: return .needsConfirmation
            case .awaitingLogin: return .needsLogin
            case .completed: return .completed
            case .failed: return .failed
            case .cancelled: return .cancelled
            case .timedOut: return .timedOut
            }
        }
    }
    public mutating func setServerStatus(_ value: String) {
        status = Self.coarseStatus(value)
        rawStatus = value
    }
    private static func coarseStatus(_ value: String) -> AgentStatus {
        switch value {
        case "idle": return .completed
        case "interrupted", "stopped": return .cancelled
        case "expired": return .timedOut
        default: return AgentStatus(rawValue: value) ?? .running
        }
    }
    public var title: String
    public var steps: [AgentStep]
    public var confirmation: AgentConfirmation?
    public var result: AgentResult?
    public var outcome: String?
    public var errorCode: String?
    public var createdAt: Date
    public var updatedAt: Date?
    public var finishedAt: Date?
    public var deadlineAt: Date?
    public var seenAt: Date?

    public init(id: UUID = UUID(), mode: AgentMode, status: AgentStatus, title: String, steps: [AgentStep] = [], confirmation: AgentConfirmation? = nil, result: AgentResult? = nil, outcome: String? = nil, errorCode: String? = nil, createdAt: Date = Date(), updatedAt: Date? = nil, finishedAt: Date? = nil, deadlineAt: Date? = nil, seenAt: Date? = nil) {
        self.id = id; self.mode = mode; self.status = status; self.rawStatus = status.rawValue; self.title = title; self.steps = steps; self.confirmation = confirmation
        self.result = result; self.outcome = outcome; self.errorCode = errorCode; self.createdAt = createdAt; self.updatedAt = updatedAt
        self.finishedAt = finishedAt; self.deadlineAt = deadlineAt; self.seenAt = seenAt
    }

    enum CodingKeys: String, CodingKey {
        case taskID = "task_id", id, mode, provider, status, title, steps, confirmation, result, outcome
        case errorCode = "error_code", createdAt = "created_at", updatedAt = "updated_at", finishedAt = "finished_at", deadlineAt = "deadline_at", seenAt = "seen_at"
    }
    /// Accepts both the function's task object (`task_id`) and a raw `agent_tasks` row (`id`).
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        guard let id = (try? c.decode(UUID.self, forKey: .taskID)) ?? (try? c.decode(UUID.self, forKey: .id)) else {
            throw DecodingError.keyNotFound(CodingKeys.taskID, .init(codingPath: decoder.codingPath, debugDescription: "task id missing"))
        }
        self.id = id
        mode = (try? c.decode(AgentMode.self, forKey: .mode)) ?? .research
        provider = (try? c.decode(AgentProvider.self, forKey: .provider)) ?? .h
        rawStatus = (try? c.decode(String.self, forKey: .status)) ?? "running"
        status = Self.coarseStatus(rawStatus)
        title = (try? c.decode(String.self, forKey: .title)) ?? ""
        steps = ((try? c.decode([AgentStep?].self, forKey: .steps)) ?? []).compactMap { $0 }
        confirmation = try? c.decodeIfPresent(AgentConfirmation.self, forKey: .confirmation)
        result = try? c.decodeIfPresent(AgentResult.self, forKey: .result)
        outcome = try? c.decodeIfPresent(String.self, forKey: .outcome)
        errorCode = try? c.decodeIfPresent(String.self, forKey: .errorCode)
        createdAt = (try? c.decode(Date.self, forKey: .createdAt)) ?? Date()
        updatedAt = try? c.decodeIfPresent(Date.self, forKey: .updatedAt)
        finishedAt = try? c.decodeIfPresent(Date.self, forKey: .finishedAt)
        deadlineAt = try? c.decodeIfPresent(Date.self, forKey: .deadlineAt)
        seenAt = try? c.decodeIfPresent(Date.self, forKey: .seenAt)
    }
    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .taskID); try c.encode(mode, forKey: .mode); try c.encode(provider, forKey: .provider); try c.encode(rawStatus, forKey: .status); try c.encode(title, forKey: .title)
        try c.encode(steps, forKey: .steps); try c.encodeIfPresent(confirmation, forKey: .confirmation); try c.encodeIfPresent(result, forKey: .result)
        try c.encodeIfPresent(outcome, forKey: .outcome); try c.encodeIfPresent(errorCode, forKey: .errorCode); try c.encode(createdAt, forKey: .createdAt)
        try c.encodeIfPresent(updatedAt, forKey: .updatedAt); try c.encodeIfPresent(finishedAt, forKey: .finishedAt); try c.encodeIfPresent(deadlineAt, forKey: .deadlineAt); try c.encodeIfPresent(seenAt, forKey: .seenAt)
    }

    public var latestStep: String? { steps.last?.text }
    public var elapsedIsRunning: Bool { [.queued, .working, .needsConfirmation].contains(phase) }
    public var elapsedEnd: Date { max(createdAt, finishedAt ?? updatedAt ?? createdAt) }
    public func elapsed(at now: Date = Date()) -> TimeInterval {
        max(0, (elapsedIsRunning ? now : elapsedEnd).timeIntervalSince(createdAt))
    }
    /// Cancelled because the 15-minute confirmation window (capped at the task deadline) ran out.
    public var confirmationExpired: Bool { status == .cancelled && errorCode == "confirmation_expired" }
    /// Server limits (PR #18): at most this many non-terminal tasks per user, and starts per rolling 24 h.
    public static let maxActive = 2
    public static let maxPerDay = 40
    public static let confirmationWindow: TimeInterval = 15 * 60
    public var needsAttention: Bool { phase == .needsLogin || phase == .needsCode || status == .awaitingConfirmation || (status.isTerminal && seenAt == nil) }
    /// One line for cards and notifications.
    public var summaryLine: String {
        if phase == .needsCode { return phase.label }
        if phase == .stopped { return "Stopped. Gusan can continue with another message." }
        switch status {
        case .queued: return "Waiting for an agent…"
        case .running: return latestStep ?? (mode == .research ? "Researching…" : "Working on it…")
        case .awaitingConfirmation: return confirmation.map { "Needs your OK: " + $0.summary } ?? "Needs your OK before continuing"
        case .awaitingLogin: return "Open Vault in the Gusan app to approve a saved login"
        case .completed: return result.map { MarkdownBlocks.firstLine($0.markdown) } ?? "Finished"
        case .failed: return AgentError.message(forCode: errorCode) ?? "The task failed"
        case .cancelled: return confirmationExpired ? "Cancelled: the confirmation expired after 15 minutes" : "Cancelled"
        case .timedOut: return "Ran out of time before finishing"
        }
    }

    public static let dateDecoder: JSONDecoder = {
        let decoder = JSONDecoder()
        let full = ISO8601DateFormatter(); full.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let plain = ISO8601DateFormatter()
        decoder.dateDecodingStrategy = .custom { decoder in
            let raw = try decoder.singleValueContainer().decode(String.self)
            // Postgres emits "2026-11-07 21:00:00.12345+00"; normalise it before ISO parsing.
            var text = raw.replacingOccurrences(of: " ", with: "T")
            if let plus = text.range(of: "+00", options: .backwards), plus.upperBound == text.endIndex { text.replaceSubrange(plus, with: "Z") }
            if let dot = text.firstIndex(of: "."), let zone = text[dot...].firstIndex(where: { $0 == "Z" || $0 == "+" || $0 == "-" }) {
                let fraction = text[text.index(after: dot)..<zone]
                text.replaceSubrange(text.index(after: dot)..<zone, with: fraction.prefix(3).padding(toLength: 3, withPad: "0", startingAt: 0))
            }
            if let date = full.date(from: text) ?? plain.date(from: text) { return date }
            throw DecodingError.dataCorrupted(.init(codingPath: decoder.codingPath, debugDescription: "Bad date: \(raw)"))
        }
        return decoder
    }()
}

public enum AgentError: Error, LocalizedError, Equatable {
    case code(String, status: Int)
    case notConfigured, busy, tooManyActive, quotaExceeded, notAwaitingConfirmation, notFound, privacyMode, proRequired, unauthorized

    public init(code: String, status: Int) {
        switch code {
        case "agent_not_configured": self = .notConfigured
        case "agent_busy", "agent_capacity": self = .busy
        case "too_many_active": self = .tooManyActive
        case "quota_exceeded": self = .quotaExceeded
        case "not_awaiting_confirmation": self = .notAwaitingConfirmation
        case "not_found": self = .notFound
        case "pro_required": self = .proRequired
        case "unauthorized": self = .unauthorized
        default: self = .code(code, status: status)
        }
    }
    public var errorDescription: String? {
        switch self {
        case .notConfigured: return "Agent tasks aren't set up on Gusan Cloud yet."
        case .busy: return "Gusan's agent is busy right now. Try again in a minute."
        case .tooManyActive: return "You already have \(AgentTask.maxActive) tasks running. Wait for one to finish, or cancel it in Activity."
        case .quotaExceeded: return "You've reached today's limit of \(AgentTask.maxPerDay) agent tasks. It resets 24 hours after the first one."
        case .notAwaitingConfirmation: return "This task isn't waiting for confirmation any more."
        case .notFound: return "That task no longer exists."
        case .privacyMode: return "Agent tasks are off in Privacy Mode."
        case .proRequired: return "Gusan Pro is needed for agent tasks."
        case .unauthorized: return "Sign in to use agent tasks."
        case .code(let code, let status):
            if status == 400 { return "The request wasn't accepted (\(code))." }
            if status == 413 { return "That request is too large. Shorten the text or remove images." }
            return Self.message(forCode: code) ?? "The task failed (\(code))."
        }
    }
    /// Worth trying again right away (the server said so, or the network blinked).
    public var isRetryable: Bool {
        switch self {
        case .busy: return true
        case .code(let code, let status): return code == "upstream_error" || code == "storage_error" || code == "bad_response" || status >= 500
        default: return false
        }
    }
    /// Friendly text for a task row's `error_code` (H's enum and ours). nil for unknown codes.
    public static func message(forCode code: String?) -> String? {
        switch code {
        case nil, "": return nil
        case "agent_capacity": return "The agents were busy; try again."
        case "upstream_error": return "The agent service had a problem."
        case "confirmation_expired": return "The confirmation expired after 15 minutes."
        case "timeout": return "The task ran out of time."
        case "plan_missing": return "Gusan couldn't make a plan for that. Try rephrasing."
        case "login_expired": return "Gusan's login expired. Sign in again to continue."
        case "otp_timeout": return "Gusan didn't receive a code in time. Try again."
        case "off_plan_navigation": return "Gusan stopped because the page went outside the approved plan."
        case "vault_error": return "Gusan couldn't access the saved login. Try signing in again."
        case "environment_error": return "The agent's browser hit a problem."
        case "no_answer": return "The agent finished without an answer."
        case "answer_validation": return "The agent's answer couldn't be read."
        case "insufficient_credit": return "The agent service is out of credit."
        case "internal", "storage_error": return "Something went wrong on Gusan Cloud."
        case "max_steps_reached": return "The task took too many steps."
        case "blocked", "content_policy": return "The agent stopped: that action isn't allowed."
        default: return "The task failed (\(code!))."
        }
    }
}
