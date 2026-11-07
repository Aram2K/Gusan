import Foundation

/// What the keyboard and the app need from the agent backend. `AgentClient` talks to Gusan Cloud;
/// `StubAgentService` plays the same contract locally for tests, previews and screenshots.
public protocol AgentService: AnyObject {
    func start(_ intent: AgentIntent, context: String?) async throws -> AgentTask
    /// `waitSeconds` (0–20) lets the server long-poll H before answering.
    func status(_ id: UUID, waitSeconds: Int) async throws -> AgentTask
    func confirm(_ id: UUID) async throws -> AgentTask
    func cancel(_ id: UUID) async throws -> AgentTask
    /// Newest first, straight from the `agent_tasks` table (owner RLS).
    func list(limit: Int) async throws -> [AgentTask]
    func markSeen(_ id: UUID) async throws
}

public final class AgentClient: AgentService {
    public static let functionURL = URL(string: AuthClient.baseURL + "/functions/v1/agent-action")!
    public static let tableURL = URL(string: AuthClient.baseURL + "/rest/v1/agent_tasks")!
    public static let seenRPCURL = URL(string: AuthClient.baseURL + "/rest/v1/rpc/mark_agent_task_seen")!

    public typealias TokenProvider = () async throws -> String
    private let token: TokenProvider
    private let transport: HTTPTransport

    public init(tokenProvider: @escaping TokenProvider, transport: HTTPTransport = URLSession.shared) {
        self.token = tokenProvider
        self.transport = transport
    }

    /// Uses the shared Supabase session; refreshes it when it is about to expire. No provider keys on the phone.
    public convenience init(auth: AuthClient, transport: HTTPTransport = URLSession.shared) {
        self.init(tokenProvider: {
            guard let session = try await auth.refresh() else { throw AgentError.code("unauthorized", status: 401) }
            return session.accessToken
        }, transport: transport)
    }

    // MARK: Requests (public so tests can pin the wire format)

    public static func actionRequest(_ body: [String: Any], token: String) throws -> URLRequest {
        var request = URLRequest(url: functionURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 45
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(AuthClient.publishableKey, forHTTPHeaderField: "apikey")
        request.httpBody = try JSONSerialization.data(withJSONObject: body)
        return request
    }

    public static func listRequest(limit: Int, token: String) -> URLRequest {
        var components = URLComponents(url: tableURL, resolvingAgainstBaseURL: false)!
        components.queryItems = [
            URLQueryItem(name: "select", value: "id,mode,status,title,steps,result,outcome,error_code,created_at,updated_at,finished_at,deadline_at,seen_at"),
            URLQueryItem(name: "order", value: "created_at.desc"),
            URLQueryItem(name: "limit", value: String(max(1, min(limit, 100)))),
        ]
        var request = URLRequest(url: components.url!)
        request.timeoutInterval = 20
        request.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        request.setValue(AuthClient.publishableKey, forHTTPHeaderField: "apikey")
        return request
    }

    // MARK: AgentService

    public func start(_ intent: AgentIntent, context: String?) async throws -> AgentTask {
        var body: [String: Any] = ["action": "start", "instruction": intent.instruction, "mode": intent.mode.rawValue]
        if let context = context?.trimmingCharacters(in: .whitespacesAndNewlines), !context.isEmpty {
            body["context"] = String(context.prefix(20_000))
        }
        body["timezone"] = TimeZone.current.identifier
        return try await task(body)
    }

    public func status(_ id: UUID, waitSeconds: Int) async throws -> AgentTask {
        try await task(["action": "status", "task_id": id.uuidString.lowercased(), "wait_s": max(0, min(waitSeconds, 20))])
    }

    public func confirm(_ id: UUID) async throws -> AgentTask {
        try await task(["action": "confirm", "task_id": id.uuidString.lowercased()])
    }

    public func cancel(_ id: UUID) async throws -> AgentTask {
        try await task(["action": "cancel", "task_id": id.uuidString.lowercased()])
    }

    public func list(limit: Int = 50) async throws -> [AgentTask] {
        let request = Self.listRequest(limit: limit, token: try await token())
        let (data, response) = try await transport.data(for: request)
        try Self.check(data, response)
        return try AgentTask.dateDecoder.decode([AgentTask].self, from: data)
    }

    public func markSeen(_ id: UUID) async throws {
        var request = URLRequest(url: Self.seenRPCURL)
        request.httpMethod = "POST"
        request.timeoutInterval = 20
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue("Bearer \(try await token())", forHTTPHeaderField: "Authorization")
        request.setValue(AuthClient.publishableKey, forHTTPHeaderField: "apikey")
        request.httpBody = try JSONSerialization.data(withJSONObject: ["p_task_id": id.uuidString.lowercased()])
        let (data, response) = try await transport.data(for: request)
        try Self.check(data, response)
    }

    private func task(_ body: [String: Any]) async throws -> AgentTask {
        let request = try Self.actionRequest(body, token: try await token())
        let (data, response) = try await transport.data(for: request)
        try Self.check(data, response)
        return try Self.decodeTask(data)
    }

    public static func decodeTask(_ data: Data) throws -> AgentTask {
        if let task = try? AgentTask.dateDecoder.decode(AgentTask.self, from: data) { return task }
        // Tolerate an envelope like {"task": {...}}.
        struct Envelope: Decodable { let task: AgentTask }
        return try AgentTask.dateDecoder.decode(Envelope.self, from: data).task
    }

    static func check(_ data: Data, _ response: URLResponse) throws {
        guard let http = response as? HTTPURLResponse else { throw AgentError.code("bad_response", status: 0) }
        guard !(200...299).contains(http.statusCode) else { return }
        let object = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any]
        let code = (object?["error"] as? String) ?? (object?["code"] as? String) ?? "http_\(http.statusCode)"
        throw AgentError(code: code, status: http.statusCode)
    }
}

/// Plays the backend contract in memory. Research tasks finish after a few status calls; do-it
/// tasks stop for confirmation first. Deterministic, so the UI can be exercised without a server.
public final class StubAgentService: AgentService {
    public struct Scenario {
        public var pollsUntilProgress: Int = 1
        public var pollsUntilDone: Int = 3
        public init() {}
    }

    private var tasks: [UUID: AgentTask] = [:]
    private var polls: [UUID: Int] = [:]
    private var order: [UUID] = []
    private let lock = NSLock()
    public var scenario = Scenario()
    public var delay: UInt64 = 0

    public init(seeded: [AgentTask] = []) {
        for task in seeded { tasks[task.id] = task; order.append(task.id) }
    }

    public func start(_ intent: AgentIntent, context: String?) async throws -> AgentTask {
        try await sleep()
        let task = AgentTask(mode: intent.mode, status: .queued, title: String(intent.instruction.prefix(120)))
        return locked { tasks[task.id] = task; polls[task.id] = 0; order.insert(task.id, at: 0); return task }
    }

    public func status(_ id: UUID, waitSeconds: Int) async throws -> AgentTask {
        try await sleep()
        return try locked {
        guard var task = tasks[id] else { throw AgentError.notFound }
        guard !task.status.isTerminal, task.status != .awaitingConfirmation else { return task }
        let count = (polls[id] ?? 0) + 1
        polls[id] = count
        if count >= scenario.pollsUntilDone {
            if task.mode == .do, task.confirmation == nil, task.outcome == nil {
                task.status = .awaitingConfirmation
                task.confirmation = AgentConfirmation(summary: "Submit the booking form on the restaurant's site for 2 people, Friday 19:30, under your name. No payment is taken.", actionKind: "submit")
            } else {
                task.status = .completed
                task.finishedAt = Date()
                task.result = Self.sampleResult(for: task)
            }
        } else if count >= scenario.pollsUntilProgress {
            task.status = .running
            task.steps.append(AgentStep(at: Date(), text: Self.sampleSteps[min(count - 1, Self.sampleSteps.count - 1)]))
        }
        task.updatedAt = Date()
        tasks[id] = task
        return task
        }
    }

    public func confirm(_ id: UUID) async throws -> AgentTask {
        try await sleep()
        return try locked {
        guard var task = tasks[id] else { throw AgentError.notFound }
        guard task.status == .awaitingConfirmation else { throw AgentError.notAwaitingConfirmation }
        task.status = .running
        task.confirmation = nil
        task.outcome = "confirmed"
        task.steps.append(AgentStep(at: Date(), text: "Confirmed. Submitting…"))
        polls[id] = 0
        tasks[id] = task
        return task
        }
    }

    public func cancel(_ id: UUID) async throws -> AgentTask {
        try await sleep()
        return try locked {
        guard var task = tasks[id] else { throw AgentError.notFound }
        if !task.status.isTerminal { task.status = .cancelled; task.finishedAt = Date(); task.confirmation = nil }
        tasks[id] = task
        return task
        }
    }

    public func list(limit: Int) async throws -> [AgentTask] {
        try await sleep()
        return locked { order.prefix(limit).compactMap { tasks[$0] } }
    }

    public func markSeen(_ id: UUID) async throws {
        locked { tasks[id]?.seenAt = Date() }
    }

    private func locked<T>(_ body: () throws -> T) rethrows -> T { lock.lock(); defer { lock.unlock() }; return try body() }
    private func sleep() async throws { if delay > 0 { try await Task.sleep(nanoseconds: delay) } }

    static let sampleSteps = ["Opening a search engine", "Reading the first results", "Comparing the options", "Writing up the answer"]

    static func sampleResult(for task: AgentTask) -> AgentResult {
        if task.mode == .do {
            return AgentResult(markdown: "**Done.** The booking form was submitted for 2 people on Friday at 19:30. A confirmation email should arrive shortly.", links: [AgentLink(title: "Booking page", url: URL(string: "https://example.com/book")!)])
        }
        return AgentResult(
            markdown: "## Quick answer\nThe cheapest direct option is **€89 return** with a Tuesday departure.\n\n- Tuesday 07:10 → 09:25, €89\n- Thursday 18:40 → 20:55, €104\n- Saturday 11:05 → 13:20, €131\n\nPrices checked just now; they change often.",
            links: [AgentLink(title: "Skyscanner results", url: URL(string: "https://www.skyscanner.net")!), AgentLink(title: "Airline fare rules", url: URL(string: "https://example.com/fares")!)],
            images: []
        )
    }

    /// Tasks in every state, for previews and Simulator screenshots.
    public static func demoTasks(now: Date = Date()) -> [AgentTask] {
        let research = AgentTask(id: UUID(uuidString: "00000000-0000-0000-0000-000000000003")!, mode: .research, status: .completed, title: "Find the cheapest direct flights to Rome next week", steps: sampleSteps.map { AgentStep(at: now, text: $0) }, result: sampleResult(for: AgentTask(mode: .research, status: .completed, title: "")), createdAt: now.addingTimeInterval(-3600), finishedAt: now.addingTimeInterval(-3300))
        let confirm = AgentTask(id: UUID(uuidString: "00000000-0000-0000-0000-000000000002")!, mode: .do, status: .awaitingConfirmation, title: "Book a table for two at Osteria Francescana on Friday at 19:30", steps: [AgentStep(at: now, text: "Opened the reservation page"), AgentStep(at: now, text: "Filled in the form")], confirmation: AgentConfirmation(summary: "Submit the reservation form on osteriafrancescana.it for 2 guests, Friday 19:30, name Aram. No payment is taken.", actionKind: "submit"), createdAt: now.addingTimeInterval(-600))
        let running = AgentTask(id: UUID(uuidString: "00000000-0000-0000-0000-000000000001")!, mode: .research, status: .running, title: "Research the best noise-cancelling headphones under €300", steps: [AgentStep(at: now, text: "Reading reviews on rtings.com")], createdAt: now.addingTimeInterval(-120))
        let failed = AgentTask(id: UUID(uuidString: "00000000-0000-0000-0000-000000000004")!, mode: .research, status: .timedOut, title: "Compare electricity tariffs in Paris for a 60 m² flat", createdAt: now.addingTimeInterval(-86_400), finishedAt: now.addingTimeInterval(-86_000), seenAt: now)
        return [running, confirm, research, failed]
    }
}
