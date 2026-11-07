import XCTest
@testable import GusanKit

final class AgentTests: XCTestCase {
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
        for text in ["Make it more formal", "Translate to French", "fix the grammar", "Rewrite this as a haiku", "Summarize this", "Reply to this politely", "Send it", "ok", "Find it in the text and shorten"] {
            XCTAssertNil(AgentIntent.detect(text), text)
        }
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
        XCTAssertEqual(AgentError(code: "pro_required", status: 403), .code("pro_required", status: 403))
        XCTAssertEqual(AgentError(code: "too_many_active", status: 429), .tooManyActive)
        XCTAssertEqual(AgentError(code: "agent_busy", status: 503), .busy)
        XCTAssertEqual(AgentError.message(forCode: "confirmation_expired"), "The confirmation expired.")
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
        let task = try await stub.status(try XCTUnwrap(id), waitSeconds: 0)
        XCTAssertEqual(task.status, .cancelled)
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
