import XCTest
@testable import GusanKit

final class AgentTests: XCTestCase {
    func testRichPhasesRoundTripWithoutChangingKeyboardStatus() throws {
        let cases: [(String, AgentStatus, AgentPhase)] = [
            ("awaiting_login", .running, .needsLogin), ("awaiting_code", .running, .needsCode),
            ("idle", .completed, .completed), ("interrupted", .cancelled, .stopped),
            ("future_status", .running, .working), ("", .running, .working)
        ]
        for (raw, coarse, phase) in cases {
            let data = try JSONSerialization.data(withJSONObject: ["task_id": UUID().uuidString, "status": raw])
            let task = try AgentClient.decodeTask(data)
            XCTAssertEqual(task.status, coarse)
            XCTAssertEqual(task.phase, phase)
            XCTAssertEqual(task.rawStatus, raw)
            let restored = try AgentTask.dateDecoder.decode(AgentTask.self, from: JSONEncoder().encode(task))
            XCTAssertEqual(restored.rawStatus, raw)
            XCTAssertEqual(restored.phase, phase)
        }
        let missing = try AgentClient.decodeTask(Data("{\"task_id\":\"\(UUID().uuidString)\"}".utf8))
        XCTAssertEqual(missing.status, .running)
        var changed = missing
        changed.setServerStatus("awaiting_login")
        changed.status = .completed
        XCTAssertEqual(changed.rawStatus, "completed")
    }

    func testConfirmationDetailsAreTolerant() throws {
        let decoder = JSONDecoder()
        let full = try decoder.decode(AgentConfirmation.self, from: Data(#"{"summary":"Book","start_url":"https://example.com/book","steps":["Open","Submit"]}"#.utf8))
        XCTAssertEqual(full.startURL?.host, "example.com")
        XCTAssertEqual(full.steps, ["Open", "Submit"])
        let empty = try decoder.decode(AgentConfirmation.self, from: Data("{}".utf8))
        XCTAssertEqual(empty.summary, "")
        XCTAssertEqual(empty.steps, [])
        let malformed = try decoder.decode(AgentConfirmation.self, from: Data(#"{"summary":42,"start_url":42,"steps":false}"#.utf8))
        XCTAssertNil(malformed.startURL)
        XCTAssertEqual(malformed.steps, [])
    }

    func testContinuationAndStopFallbacks() async throws {
        final class Transport: HTTPTransport {
            var bodies: [[String: Any]] = []
            var rejection = 400
            let id = UUID()
            func data(for request: URLRequest) async throws -> (Data, URLResponse) {
                let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
                bodies.append(body)
                let action = body["action"] as! String
                let unsupported = action == "continue" || action == "stop"
                let task = AgentTask(id: action == "start" ? UUID() : id, mode: .do, status: .completed, title: "Previous title", result: AgentResult(markdown: "**Earlier** " + String(repeating: "x", count: 25_000)))
                let data = unsupported ? Data(#"{"error":"bad_request","field":"action"}"#.utf8) : try JSONEncoder().encode(task)
                return (data, HTTPURLResponse(url: request.url!, statusCode: unsupported ? rejection : 200, httpVersion: nil, headerFields: nil)!)
            }
        }
        let transport = Transport()
        let client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
        let next = try await client.continueTask(transport.id, message: "Try another date")
        XCTAssertNotEqual(next.id, transport.id)
        XCTAssertEqual(transport.bodies.map { $0["action"] as! String }, ["continue", "status", "start"])
        let start = transport.bodies[2]
        XCTAssertEqual(start["mode"] as? String, "do")
        XCTAssertEqual(start["instruction"] as? String, "Try another date")
        let context = try XCTUnwrap(start["context"] as? String)
        XCTAssertTrue(context.hasPrefix("Earlier in this session:\nPrevious title\nEarlier "))
        XCTAssertLessThanOrEqual(context.count, 20_000)
        _ = try await client.stop(transport.id)
        XCTAssertEqual(transport.bodies.suffix(2).map { $0["action"] as! String }, ["stop", "cancel"])
        transport.rejection = 503
        let count = transport.bodies.count
        do { _ = try await client.continueTask(transport.id, message: "Retry"); XCTFail("must not retry server errors") }
        catch { XCTAssertEqual(transport.bodies.count, count + 1) }
    }

    func testSessionThreadsPersistAndCap() throws {
        let suite = "AgentSessionTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = AgentSessionStore(defaults: defaults)
        XCTAssertTrue(store.records.isEmpty)
        let root = UUID(), child = UUID()
        store.record(taskID: root, message: "Original instruction")
        store.record(taskID: child, message: "Follow up", continuing: root)
        let restored = AgentSessionStore(defaults: defaults)
        XCTAssertEqual(restored.tasks(in: root).map(\.message), ["Original instruction", "Follow up"])
        XCTAssertEqual(restored.record(taskID: child, message: "Duplicate"), root)
        for _ in 0..<205 { restored.record(taskID: UUID(), message: "Next", continuing: child) }
        XCTAssertEqual(restored.records.count, 200)
        defaults.set(Data("invalid".utf8), forKey: "gusan.agent.sessions")
        XCTAssertTrue(restored.records.isEmpty)
    }

    func testSmartHistoryAndPrivacyCompatibility() throws {
        let suite = "SmartHistoryTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let history = SmartHistory(defaults: defaults)
        XCTAssertTrue(history.entries.isEmpty)
        history.append(instruction: "Empty", result: " ")
        XCTAssertTrue(history.entries.isEmpty)
        for index in 0..<55 { history.append(instruction: "Heard \(index)", result: "Answer", sourceApp: "Mail") }
        let restored = SmartHistory(defaults: defaults)
        XCTAssertEqual(restored.entries.count, 50)
        XCTAssertEqual(restored.entries.first?.instruction, "Heard 5")
        XCTAssertEqual(restored.entries.last?.sourceApp, "Mail")
        defaults.set(true, forKey: "privacyMode")
        let settings = SettingsStore(group: suite)
        XCTAssertFalse(settings.privacyMode)
        settings.privacyMode = true
        XCTAssertFalse(settings.privacyMode)
        XCTAssertNotEqual(AgentError(code: "privacy_mode", status: 403), .privacyMode)
    }

    func testStubContinuationAndRobustnessScenarios() async throws {
        let stub = StubAgentService()
        let root = try await stub.start(AgentIntent(mode: .do, instruction: "Book"), context: nil)
        let next = try await stub.continueTask(root.id, message: "Next")
        XCTAssertNotEqual(next.id, root.id)
        XCTAssertEqual(stub.parentTaskIDs[next.id], root.id)
        let stopped = try await stub.stop(next.id)
        XCTAssertEqual(stopped.phase, .stopped)
        stub.scenario.needsLogin = true
        let login = try await stub.status(root.id, waitSeconds: 0)
        XCTAssertEqual(login.phase, .needsLogin)
        XCTAssertFalse(login.status.isTerminal)
        stub.scenario.needsLogin = false
        stub.scenario.planMissing = true
        stub.scenario.pollsUntilDone = 1
        let failed = try await stub.status(root.id, waitSeconds: 0)
        XCTAssertEqual(failed.errorCode, "plan_missing")
        XCTAssertTrue(failed.summaryLine.contains("couldn't make a plan"))
    }

    // MARK: Intent

    func testDetectsResearchAndDoTasks() {
        XCTAssertEqual(AgentIntent.detect("Find the cheapest flights to Rome next week")?.mode, .research)
        XCTAssertEqual(AgentIntent.detect("research the best noise cancelling headphones")?.mode, .research)
        XCTAssertEqual(AgentIntent.detect("What is the population of Yerevan?")?.mode, .research)
        XCTAssertEqual(AgentIntent.detect("Book a table for two at Luigi's on Friday")?.mode, .do)
        XCTAssertEqual(AgentIntent.detect("please order a new charger for my MacBook")?.mode, .do)
        XCTAssertEqual(AgentIntent.detect("Hey Gusan, sign me up for the newsletter")?.mode, .do)
        XCTAssertEqual(AgentIntent.detect("Hey Gusan, sign me up for the newsletter")?.instruction, "sign me up for the newsletter")
    }

    func testIgnoresTextEdits() {
        for text in ["Make it more formal", "Translate to French", "fix the grammar", "Rewrite this as a haiku", "Summarize this", "Reply to this politely", "Send it", "ok", "Find it in the text and shorten",
                     "check grammar and spelling", "order these words alphabetically", "check my punctuation", "Find a shorter way to say this sentence", "Find a nicer way to say no", "list the pros and cons", "which is better here, A or B?", "order the paragraphs by date", "Sort these alphabetically"] {
            XCTAssertNil(AgentIntent.detect(text), text)
        }
        XCTAssertEqual(AgentIntent.detect("order the groceries from Rewe")?.mode, .do)
        XCTAssertEqual(AgentIntent.detect("Order a longer charging cable")?.mode, .do)
        XCTAssertEqual(AgentIntent.detect("check my flight status for LH123")?.mode, .research)
        XCTAssertEqual(AgentIntent.detect("Send Mary the notes from today")?.mode, .do, "sending to a person is a do-it task; the card asks before anything is sent")
        XCTAssertTrue(AgentIntent.referencesDraft("Find flights matching the dates in this text"))
        XCTAssertFalse(AgentIntent.referencesDraft("Find the cheapest flights to Rome"))
        XCTAssertTrue(AgentIntent.referencesDraft("Find flights matching this itinerary"))
        XCTAssertFalse(AgentIntent.referencesDraft("Find coffee shops near here"))
        XCTAssertFalse(AgentIntent.referencesDraft("Book the hotel mentioned above"))
    }

    // MARK: Decoding

    func testDecodesFunctionTaskObjectAndTableRow() throws {
        let function = """
        {"task_id":"6F9619FF-8B86-D011-B42D-00C04FC964FF","mode":"do","status":"awaiting_confirmation","title":"Book","steps":[{"at":"2026-11-07T21:00:00.123456Z","text":"Opened page"}],
         "confirmation":{"summary":"Submit the form","action_kind":"submit"},"result":null,"outcome":null,"error_code":null,
         "created_at":"2026-11-07T20:59:00Z","updated_at":"2026-11-07T21:00:00Z","finished_at":null,"deadline_at":"2026-11-07T21:12:00Z"}
        """
        let task = try AgentClient.decodeTask(Data(function.utf8))
        XCTAssertEqual(task.status, .awaitingConfirmation)
        XCTAssertEqual(task.confirmation?.actionKind, "submit")
        XCTAssertEqual(task.confirmation?.kindLabel, "Submit a form")
        XCTAssertEqual(task.steps.count, 1)
        XCTAssertNotNil(task.steps[0].at)
        XCTAssertTrue(task.needsAttention)

        let row = """
        [{"id":"6f9619ff-8b86-d011-b42d-00c04fc964ff","mode":"research","status":"completed","title":"Find","steps":[],
          "result":{"markdown":"# Answer\\nIt is **42**.","links":[{"title":"Source","url":"https://example.com"}],"images":["https://example.com/a.png"]},
          "outcome":"answered","error_code":null,"created_at":"2026-11-07 20:59:00.5+00","updated_at":null,"finished_at":"2026-11-07 21:00:00+00","deadline_at":null,"seen_at":null}]
        """
        let tasks = try AgentTask.dateDecoder.decode([AgentTask].self, from: Data(row.utf8))
        XCTAssertEqual(tasks.count, 1)
        XCTAssertEqual(tasks[0].result?.links.first?.url.host, "example.com")
        XCTAssertEqual(tasks[0].result?.images.count, 1)
        XCTAssertEqual(tasks[0].result?.plainText, "Answer\n\nIt is 42.")
        XCTAssertEqual(tasks[0].summaryLine, "Answer")
        XCTAssertNotNil(tasks[0].finishedAt)
    }

    func testErrorCodesMap() {
        XCTAssertEqual(AgentError(code: "pro_required", status: 403), .proRequired)
        XCTAssertTrue(AgentError(code: "agent_busy", status: 503).isRetryable)
        XCTAssertTrue(AgentError(code: "upstream_error", status: 502).isRetryable)
        XCTAssertFalse(AgentError.tooManyActive.isRetryable)
        XCTAssertTrue(AgentError.tooManyActive.errorDescription!.contains("2 tasks"))
        XCTAssertTrue(AgentError.quotaExceeded.errorDescription!.contains("40"))
        XCTAssertEqual(AgentError.message(forCode: "timeout"), "The task ran out of time.")
        XCTAssertEqual(AgentError(code: "too_many_active", status: 429), .tooManyActive)
        XCTAssertEqual(AgentError(code: "agent_busy", status: 503), .busy)
        XCTAssertEqual(AgentError.message(forCode: "confirmation_expired"), "The confirmation expired after 15 minutes.")
        let expired = AgentTask(mode: .do, status: .cancelled, title: "Book", errorCode: "confirmation_expired")
        XCTAssertTrue(expired.confirmationExpired)
        XCTAssertTrue(expired.summaryLine.contains("expired"))
        XCTAssertNil(AgentError.message(forCode: nil))
    }

    // MARK: Wire format

    func testRequestsCarryAuthAndContract() throws {
        let intent = AgentIntent(mode: .do, instruction: "Book a table")
        let request = try AgentClient.actionRequest(["action": "start", "instruction": intent.instruction, "mode": intent.mode.rawValue], token: "jwt")
        XCTAssertEqual(request.url, AgentClient.functionURL)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer jwt")
        XCTAssertEqual(request.value(forHTTPHeaderField: "apikey"), AuthClient.publishableKey)
        let body = try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any]
        XCTAssertEqual(body["action"] as? String, "start")
        XCTAssertEqual(body["mode"] as? String, "do")

        let list = AgentClient.listRequest(limit: 500, token: "jwt")
        let items = URLComponents(url: list.url!, resolvingAgainstBaseURL: false)!.queryItems!
        XCTAssertEqual(items.first { $0.name == "limit" }?.value, "100")
        XCTAssertEqual(items.first { $0.name == "order" }?.value, "created_at.desc")
        XCTAssertTrue(items.first { $0.name == "select" }!.value!.contains("seen_at"))
        XCTAssertTrue(items.first { $0.name == "select" }!.value!.contains("confirmation"))
    }

    func testClientSurfacesServerErrorCodes() async throws {
        final class Transport: HTTPTransport {
            var status = 429
            var body = #"{"error":"too_many_active"}"#
            var seen: [URLRequest] = []
            func data(for request: URLRequest) async throws -> (Data, URLResponse) {
                seen.append(request)
                return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
            }
        }
        let transport = Transport()
        let client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
        do {
            _ = try await client.start(AgentIntent(mode: .research, instruction: "Find x"), context: "  ")
            XCTFail("expected an error")
        } catch let error as AgentError {
            XCTAssertEqual(error, .tooManyActive)
        }
        let body = try JSONSerialization.jsonObject(with: transport.seen[0].httpBody!) as! [String: Any]
        XCTAssertNil(body["context"], "blank context must not be sent")
        XCTAssertNotNil(body["timezone"])

        transport.status = 200
        transport.body = #"{"task_id":"6f9619ff-8b86-d011-b42d-00c04fc964ff","mode":"research","status":"running","title":"Find x","steps":[],"created_at":"2026-11-07T20:59:00Z"}"#
        let task = try await client.status(UUID(uuidString: "6f9619ff-8b86-d011-b42d-00c04fc964ff")!, waitSeconds: 99)
        XCTAssertEqual(task.status, .running)
        let statusBody = try JSONSerialization.jsonObject(with: transport.seen[1].httpBody!) as! [String: Any]
        XCTAssertEqual(statusBody["wait_s"] as? Int, 20)
        XCTAssertEqual(statusBody["task_id"] as? String, "6f9619ff-8b86-d011-b42d-00c04fc964ff")
    }

    // MARK: Stub walks the contract

    func testStubResearchFlow() async throws {
        let stub = StubAgentService()
        let started = try await stub.start(AgentIntent(mode: .research, instruction: "Find flights"), context: nil)
        XCTAssertEqual(started.status, .queued)
        var task = try await stub.status(started.id, waitSeconds: 0)
        XCTAssertEqual(task.status, .running)
        XCTAssertNotNil(task.latestStep)
        task = try await stub.status(started.id, waitSeconds: 0)
        task = try await stub.status(started.id, waitSeconds: 0)
        XCTAssertEqual(task.status, .completed)
        XCTAssertFalse(task.result!.plainText.isEmpty)
        let list = try await stub.list(limit: 10)
        XCTAssertEqual(list.first?.id, started.id)
    }

    func testStubDoFlowNeedsConfirmation() async throws {
        let stub = StubAgentService()
        let started = try await stub.start(AgentIntent(mode: .do, instruction: "Book a table"), context: nil)
        var task = started
        for _ in 0..<3 { task = try await stub.status(started.id, waitSeconds: 0) }
        XCTAssertEqual(task.status, .awaitingConfirmation)
        XCTAssertNotNil(task.confirmation)
        task = try await stub.status(started.id, waitSeconds: 0)
        XCTAssertEqual(task.status, .awaitingConfirmation, "stays parked until the user decides")
        task = try await stub.confirm(started.id)
        XCTAssertEqual(task.status, .running)
        for _ in 0..<3 { task = try await stub.status(started.id, waitSeconds: 0) }
        XCTAssertEqual(task.status, .completed)
        do { _ = try await stub.confirm(started.id); XCTFail() } catch let error as AgentError { XCTAssertEqual(error, .notAwaitingConfirmation) }
        let cancelled = try await stub.cancel(started.id)
        XCTAssertEqual(cancelled.status, .completed, "cancelling a finished task is a no-op")
    }

    // MARK: Tracker

    func testTrackerNotifiesEachTransitionOnce() {
        let defaults = UserDefaults(suiteName: "AgentTests.\(UUID().uuidString)")!
        let tracker = AgentTaskTracker(defaults: defaults)
        var task = AgentTask(mode: .do, status: .running, title: "Book")
        XCTAssertTrue(tracker.consumeEvents(from: [task]).isEmpty)
        task.status = .awaitingConfirmation
        XCTAssertEqual(tracker.consumeEvents(from: [task]), [.needsConfirmation(task)])
        XCTAssertTrue(tracker.consumeEvents(from: [task]).isEmpty, "same state again is silent")
        task.status = .completed
        XCTAssertEqual(tracker.consumeEvents(from: [task]), [.finished(task)])
        XCTAssertTrue(AgentTaskTracker(defaults: defaults).consumeEvents(from: [task]).isEmpty, "persists across instances")
        XCTAssertTrue(tracker.consumeEvents(from: []).isEmpty)
        XCTAssertEqual(tracker.consumeEvents(from: [task]), [.finished(task)], "forgotten tasks notify again when they reappear")
        tracker.activeTaskID = task.id
        XCTAssertEqual(tracker.activeTaskID, task.id)
    }

    // MARK: Markdown

    func testMarkdownBlocks() {
        let blocks = MarkdownBlocks.parse("# Title\n\nSome *text* here\ncontinued.\n\n- one\n- two\n\n1. first\n2. second\n\n```\ncode\n```\n> quoted\n---")
        XCTAssertEqual(blocks, [
            .heading(level: 1, text: "Title"), .paragraph("Some *text* here continued."), .bullets(["one", "two"]),
            .numbered(["first", "second"]), .code("code"), .quote("quoted"), .rule,
        ])
        XCTAssertEqual(MarkdownBlocks.plainText("**Bold** and [link](https://x.y) and `code`"), "Bold and link (https://x.y) and code")
        XCTAssertEqual(MarkdownBlocks.firstLine("- a\n- b"), "• a")
    }
}

final class AgentJobTests: XCTestCase {
    func testLoginCodeAndUnknownPhasesKeepPolling() async throws {
        final class Service: AgentService {
            var polls = 0
            var task = AgentTask(mode: .research, status: .running, title: "Find", confirmation: AgentConfirmation(summary: "Login", startURL: URL(string: "https://example.com/login")))
            func start(_ intent: AgentIntent, context: String?) async throws -> AgentTask { task }
            func status(_ id: UUID, waitSeconds: Int) async throws -> AgentTask {
                let phases = ["awaiting_login", "awaiting_code", "future_status", "idle"]
                task.setServerStatus(phases[min(polls, phases.count - 1)])
                polls += 1
                if task.status == .completed { task.result = AgentResult(markdown: "Answer") }
                return task
            }
            func confirm(_ id: UUID) async throws -> AgentTask { task }
            func cancel(_ id: UUID) async throws -> AgentTask { task }
            func continueTask(_ id: UUID, message: String) async throws -> AgentTask { task }
            func stop(_ id: UUID) async throws -> AgentTask { task }
            func list(limit: Int) async throws -> [AgentTask] { [task] }
            func markSeen(_ id: UUID) async throws {}
        }
        let service = Service()
        var lines: [String] = []
        var card: JobCard?
        for try await event in AgentJob(service: service, start: .resume(service.task.id), interval: 1_000).run() {
            switch event {
            case .progress(let line): lines.append(line)
            case .result(let result): card = result.card
            case .needsConfirmation: XCTFail("login is not plan confirmation")
            }
        }
        XCTAssertEqual(lines, ["Needs a login on example.com…", "Needs a code on example.com…"])
        XCTAssertEqual(service.polls, 4)
        XCTAssertEqual(card?.insertableText, "Answer")
    }

    func testResearchJobEmitsProgressThenCard() async throws {
        let stub = StubAgentService()
        var seen: [AgentTask] = []
        let lock = NSLock()
        let job = AgentJob(service: stub, start: .new(AgentIntent(mode: .research, instruction: "Find flights"), context: nil), interval: 1_000) { task in lock.lock(); seen.append(task); lock.unlock() }
        var progress: [String] = []
        var card: JobCard?
        for try await event in job.run() {
            switch event {
            case .progress(let text): progress.append(text)
            case .result(let result): card = result.card
            case .needsConfirmation: XCTFail("research never asks")
            }
        }
        XCTAssertEqual(progress.first, "Starting research…")
        XCTAssertTrue(progress.count >= 2)
        XCTAssertEqual(card?.title, "Find flights")
        XCTAssertTrue(card?.insertableText?.contains("€89") == true)
        XCTAssertEqual(seen.last?.status, .completed)
    }

    func testDoJobWaitsForConfirmationAndRunnerConfirms() async throws {
        let stub = StubAgentService()
        let job = AgentJob(service: stub, start: .new(AgentIntent(mode: .do, instruction: "Book a table"), context: nil), interval: 1_000)
        var prompt: String?
        let result = try await JobRunner().result(of: job, confirmation: { request in prompt = request.prompt; return true })
        XCTAssertNotNil(prompt)
        XCTAssertEqual(result.kind, .cardOnly)
        XCTAssertTrue(result.text.contains("submitted"))
    }

    func testDecliningConfirmationCancelsOnServer() async throws {
        let stub = StubAgentService()
        var id: UUID?
        let job = AgentJob(service: stub, start: .new(AgentIntent(mode: .do, instruction: "Book a table"), context: nil), interval: 1_000) { id = $0.id }
        do {
            _ = try await JobRunner().result(of: job, confirmation: { _ in false })
            XCTFail("declined jobs end as cancelled")
        } catch is CancellationError {}
        // The cancel is sent from a detached task; give it a moment.
        var task = try await stub.status(try XCTUnwrap(id), waitSeconds: 0)
        for _ in 0..<50 where task.status != .cancelled { try await Task.sleep(nanoseconds: 20_000_000); task = try await stub.status(try XCTUnwrap(id), waitSeconds: 0) }
        XCTAssertEqual(task.status, .cancelled)
    }

    func testReconfirmationIsAskedAgain() async throws {
        let stub = StubAgentService()
        stub.scenario.reconfirmOnce = true
        let job = AgentJob(service: stub, start: .new(AgentIntent(mode: .do, instruction: "Book a table"), context: nil), interval: 1_000)
        var prompts = 0
        let result = try await JobRunner().result(of: job, confirmation: { _ in prompts += 1; return true })
        XCTAssertEqual(prompts, 2, "the same confirmation re-surfaced by the server must be asked again")
        XCTAssertEqual(result.kind, .cardOnly)
    }

    func testServerSideCancelUnblocksPendingConfirmation() async throws {
        let stub = StubAgentService()
        var id: UUID?
        let job = AgentJob(service: stub, start: .new(AgentIntent(mode: .do, instruction: "Book a table"), context: nil), interval: 1_000) { id = $0.id }
        do {
            _ = try await JobRunner().result(of: job, confirmation: { _ in
                _ = try await stub.cancel(try XCTUnwrap(id))   // another device cancels while the card is open
                try await Task.sleep(nanoseconds: 50_000_000)
                return true
            })
            XCTFail("the job must end as cancelled")
        } catch is CancellationError {}
        let task = try await stub.status(try XCTUnwrap(id), waitSeconds: 0)
        XCTAssertEqual(task.status, .cancelled)
    }

    func testResumeFinishedTaskYieldsCardImmediately() async throws {
        let done = StubAgentService.demoTasks().first { $0.status == .completed }!
        let stub = StubAgentService(seeded: [done])
        let result = try await AgentJob(service: stub, start: .resume(done.id), interval: 1_000).result()
        XCTAssertEqual(result.card?.title, done.title)
    }
}
