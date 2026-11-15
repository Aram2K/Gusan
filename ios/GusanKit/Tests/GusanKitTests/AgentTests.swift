import XCTest
@testable import GusanKit

final class AgentTests: XCTestCase {
    func testAddressingDevinRoutesToDevinProvider() {
        for text in ["Devin, fix the typo in my README", "hey Devin add a dark mode toggle to settings", "please ask Devin to rewrite the login page in SwiftUI"] {
            let intent = AgentIntent.detect(text)
            XCTAssertEqual(intent?.provider, .devin, text)
            XCTAssertEqual(intent?.mode, .research, text)
            XCTAssertFalse(intent?.instruction.lowercased().contains("devin") ?? true, text)
        }
        XCTAssertNil(AgentIntent.detect("Devin, hi"))
        XCTAssertEqual(AgentIntent.detect("find the cheapest flights to Rome")?.provider, .h)
        XCTAssertEqual(AgentIntent(mode: .do, instruction: "book it").provider, .h)
    }

    func testElapsedFreezesAtFinishedThenUpdatedTimestamp() {
        let start = Date(timeIntervalSince1970: 100)
        for status in [AgentStatus.completed, .failed, .cancelled, .timedOut] {
            var task = AgentTask(mode: .research, status: status, title: "Task", createdAt: start,
                                 updatedAt: start.addingTimeInterval(30), finishedAt: start.addingTimeInterval(20))
            XCTAssertFalse(task.elapsedIsRunning)
            XCTAssertEqual(task.elapsed(at: start.addingTimeInterval(500)), 20)
            task.finishedAt = nil
            XCTAssertEqual(task.elapsed(at: start.addingTimeInterval(900)), 30)
            task.updatedAt = nil
            XCTAssertEqual(task.elapsed(), 0)
        }
        var task = AgentTask(mode: .research, status: .queued, title: "Task", createdAt: start)
        for status in ["queued", "running", "awaiting_confirmation"] {
            task.setServerStatus(status)
            XCTAssertTrue(task.elapsedIsRunning)
            XCTAssertEqual(task.elapsed(at: start.addingTimeInterval(50)), 50)
        }
        for status in ["awaiting_login", "awaiting_code", "stopped", "expired", "interrupted"] {
            task.setServerStatus(status)
            XCTAssertFalse(task.elapsedIsRunning)
        }
        task.setServerStatus("stopped"); XCTAssertTrue(task.status.isTerminal)
        task.setServerStatus("expired"); XCTAssertTrue(task.status.isTerminal)
    }

    func testRemovingIndividualHistoriesAndStubTask() async throws {
        let suite = "AgentHistoryRemoveTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let history = SmartHistory(defaults: defaults), sessions = AgentSessionStore(defaults: defaults)
        history.append(instruction: "First", result: "One")
        history.append(instruction: "Second", result: "Two")
        let first = try XCTUnwrap(history.entries.first)
        history.remove(first.id); history.remove(UUID())
        XCTAssertEqual(history.entries.map(\.result), ["Two"])
        let task = AgentTask(mode: .research, status: .running, title: "Active")
        sessions.record(taskID: task.id, message: "First")
        let other = UUID(); sessions.record(taskID: other, message: "Second")
        sessions.remove(task.id); sessions.remove(UUID())
        XCTAssertEqual(sessions.records.map(\.id), [other])
        let service = StubAgentService(seeded: [task])
        try await service.delete(task.id)
        try await service.delete(task.id)
        let remaining = try await service.list(limit: 100)
        XCTAssertTrue(remaining.isEmpty)
    }

    func testDeleteClientRequestAndErrors() async throws {
        final class Transport: HTTPTransport {
            var seen: URLRequest?
            var status = 200
            func data(for request: URLRequest) async throws -> (Data, URLResponse) {
                seen = request
                return (Data("{}".utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
            }
        }
        let transport = Transport(), client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
        let id = UUID()
        try await client.delete(id)
        let request = try XCTUnwrap(transport.seen)
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: String])
        XCTAssertEqual(body, ["action": "delete", "task_id": id.uuidString.lowercased()])
        transport.status = 404
        try await client.delete(id)
        transport.status = 500
        do { try await client.delete(id); XCTFail("Expected delete failure") }
        catch { XCTAssertTrue(error is AgentError) }
    }

    func testClearingHistoriesRemovesPersistedPrivateData() throws {
        let suite = "AgentHistoryClearTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let history = SmartHistory(defaults: defaults), sessions = AgentSessionStore(defaults: defaults)
        history.clear(); sessions.clear()
        XCTAssertTrue(history.entries.isEmpty); XCTAssertTrue(sessions.records.isEmpty)
        defaults.set("agent", forKey: "stripMode")
        history.append(instruction: "Private request", result: "Private answer")
        sessions.record(taskID: UUID(), message: "Private agent request")
        XCTAssertEqual(history.entries.count, 1); XCTAssertEqual(sessions.records.count, 1)
        history.clear(); sessions.clear()
        XCTAssertTrue(SmartHistory(defaults: defaults).entries.isEmpty)
        XCTAssertTrue(AgentSessionStore(defaults: defaults).records.isEmpty)
        XCTAssertNil(defaults.data(forKey: "gusan.smart.history"))
        XCTAssertNil(defaults.data(forKey: "gusan.agent.sessions"))
        XCTAssertEqual(defaults.string(forKey: "stripMode"), "agent")
        defaults.set(Data("corrupt".utf8), forKey: "gusan.smart.history")
        defaults.set(Data("corrupt".utf8), forKey: "gusan.agent.sessions")
        history.clear(); sessions.clear()
        XCTAssertNil(defaults.data(forKey: "gusan.smart.history"))
        XCTAssertNil(defaults.data(forKey: "gusan.agent.sessions"))
    }
    func testSessionThreadResolutionAndGrouping() throws {
        let suite = "AgentThreadTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = AgentSessionStore(defaults: defaults)
        let root = AgentTask(mode: .research, status: .completed, title: "First", createdAt: Date(timeIntervalSince1970: 1))
        let next = AgentTask(mode: .research, status: .running, title: "Next", createdAt: Date(timeIntervalSince1970: 2))
        let legacy = AgentTask(mode: .do, status: .failed, title: "Legacy", createdAt: Date(timeIntervalSince1970: 3))
        XCTAssertTrue(store.threads(for: []).isEmpty)
        store.record(taskID: root.id, message: "Full original instruction")
        store.record(taskID: next.id, message: "Follow up", continuing: root.id)
        let restored = AgentSessionStore(defaults: defaults)
        XCTAssertEqual(restored.sessionID(for: next.id), root.id)
        XCTAssertEqual(restored.sessionID(for: root.id), root.id)
        XCTAssertEqual(restored.sessionID(for: legacy.id), legacy.id)
        let threads = restored.threads(for: [next, legacy, root])
        XCTAssertEqual(threads.map(\.id), [legacy.id, root.id])
        XCTAssertEqual(threads.last?.tasks.map(\.id), [root.id, next.id])
        XCTAssertEqual(threads.last?.needsAttention, true)
        defaults.set(Data("corrupt".utf8), forKey: "gusan.agent.sessions")
        XCTAssertEqual(restored.sessionID(for: next.id), next.id)
        XCTAssertEqual(restored.threads(for: [root, next]).count, 2)
    }
    func testRichPhasesRoundTripWithoutChangingKeyboardStatus() throws {
        let cases: [(String, AgentStatus, AgentPhase)] = [
            ("awaiting_login", .awaitingLogin, .needsLogin), ("awaiting_code", .running, .needsCode),
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
        let login = AgentTask(mode: .do, status: .awaitingLogin, title: "Sign in")
        XCTAssertEqual(login.phase, .needsLogin)
        XCTAssertFalse(login.status.isTerminal)
        XCTAssertTrue(login.needsAttention)
        XCTAssertEqual(login.summaryLine, "Open Vault in the Gusan app to approve a saved login")
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

    private final class ActionTransport: HTTPTransport {
        var responses: [(Int, String)] = []
        var bodies: [[String: Any]] = []
        func data(for request: URLRequest) async throws -> (Data, URLResponse) {
            bodies.append(try JSONSerialization.jsonObject(with: request.httpBody!) as! [String: Any])
            guard !responses.isEmpty else { throw URLError(.notConnectedToInternet) }
            let (status, json) = responses.removeFirst()
            return (Data(json.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
    }

    func testLiveContinuationAndStopResponseShapes() async throws {
        let transport = ActionTransport()
        let client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
        let id = UUID(), child = UUID()
        transport.responses = [
            (200, """
            {"task_id":"\(id)","mode":"research","status":"running","title":"Next option","steps":[],"confirmation":null,"result":null,"outcome":null,"error_code":null,"created_at":"2026-11-08T01:00:00Z","updated_at":"2026-11-08T01:02:00Z","finished_at":null,"deadline_at":"2026-11-08T01:10:00Z"}
            """),
            (201, """
            {"task_id":"\(child)","continued_from":"\(id)","mode":"do","status":"queued","title":"Book the next option","steps":[],"confirmation":null,"result":null}
            """),
            (200, """
            {"task_id":"\(child)","mode":"do","status":"cancelled","title":"Book the next option","steps":[],"confirmation":null,"login_request":null,"result":null,"error_code":null,"finished_at":"2026-11-08T01:03:00Z"}
            """),
            (200, """
            {"task_id":"\(id)","mode":"research","status":"completed","title":"Next option","result":{"markdown":"Saved answer","links":[],"images":[]}}
            """)
        ]
        let resumed = try await client.continueTask(id, message: "Compare the next option")
        XCTAssertEqual(resumed.id, id)
        XCTAssertEqual(resumed.phase, .working)
        XCTAssertNil(resumed.result)
        XCTAssertNil(resumed.finishedAt)
        let next = try await client.continueTask(id, message: "Book the next option")
        XCTAssertEqual(next.id, child)
        XCTAssertEqual(next.mode, .do)
        XCTAssertEqual(next.status, .queued)
        let stopped = try await client.stop(child)
        XCTAssertEqual(stopped.id, child)
        XCTAssertEqual(stopped.status, .cancelled)
        XCTAssertNotNil(stopped.finishedAt)
        let terminal = try await client.stop(id)
        XCTAssertEqual(terminal.status, .completed)
        XCTAssertEqual(terminal.result?.plainText, "Saved answer")
        XCTAssertEqual(transport.bodies.map { $0["action"] as! String }, ["continue", "continue", "stop", "stop"])
        XCTAssertEqual(Set(transport.bodies[0].keys), Set(["action", "task_id", "message"]))
        XCTAssertEqual(transport.bodies[0]["task_id"] as? String, id.uuidString.lowercased())
        XCTAssertEqual(transport.bodies[0]["message"] as? String, "Compare the next option")
        XCTAssertEqual(Set(transport.bodies[2].keys), Set(["action", "task_id"]))
    }

    func testContinueAndStopDoNotFallbackForValidationOrServiceFailures() async throws {
        for action in ["continue", "stop"] {
            for (status, json) in [
                (400, #"{"error":"bad_request","field":"message"}"#),
                (400, #"{"error":"bad_request","field":"task_id"}"#),
                (400, #"{"error":"bad_request"}"#),
                (400, #"{"error":"plan_missing","field":"action"}"#),
                (409, #"{"error":"task_active"}"#),
                (429, #"{"error":"too_many_active"}"#),
                (503, #"{"error":"bad_request","field":"action"}"#),
                (200, "{}")
            ] {
                let transport = ActionTransport()
                transport.responses = [(status, json)]
                let client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
                do {
                    if action == "continue" { _ = try await client.continueTask(UUID(), message: " ") }
                    else { _ = try await client.stop(UUID()) }
                    XCTFail("Invalid response must throw")
                } catch { XCTAssertEqual(transport.bodies.count, 1, "\(action): \(status) \(json)") }
            }
            let transport = ActionTransport()
            let client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
            do {
                if action == "continue" { _ = try await client.continueTask(UUID(), message: "Next") }
                else { _ = try await client.stop(UUID()) }
                XCTFail("Network failure must throw")
            } catch { XCTAssertEqual(transport.bodies.count, 1) }
        }
    }

    func testFailedSameTaskFollowupRetriesThroughContinue() async throws {
        let transport = ActionTransport()
        let client = AgentClient(tokenProvider: { "jwt" }, transport: transport)
        let id = UUID()
        transport.responses = [
            (200, """
            {"task_id":"\(id)","mode":"research","status":"running","title":"Follow up","result":null}
            """),
            (200, """
            {"task_id":"\(id)","mode":"research","status":"failed","title":"Follow up","result":null,"error_code":"internal"}
            """),
            (200, """
            {"task_id":"\(id)","mode":"research","status":"running","title":"Follow up","result":null,"error_code":null}
            """)
        ]
        let first = try await client.continueTask(id, message: "Compare the next option")
        XCTAssertEqual(first.id, id)
        let failed = try await client.status(id, waitSeconds: 0)
        XCTAssertEqual(failed.phase, .failed)
        let retry = try await client.continueTask(failed.id, message: "Compare the next option")
        XCTAssertEqual(retry.id, id)
        XCTAssertEqual(retry.phase, .working)
        XCTAssertEqual(transport.bodies.map { $0["action"] as! String }, ["continue", "status", "continue"])
        XCTAssertEqual(transport.bodies.first?["message"] as? String, transport.bodies.last?["message"] as? String)
    }

    func testInPlaceContinuationPreservesChatAndRestoresLegacyRecords() throws {
        let suite = "AgentSameTaskTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let store = AgentSessionStore(defaults: defaults)
        let original = AgentTask(mode: .research, status: .completed, title: "Original", result: AgentResult(markdown: "First answer"))
        defaults.set(Data("""
        [{"id":"\(original.id)","sessionID":"\(original.id)","message":"Original instruction","date":0}]
        """.utf8), forKey: "gusan.agent.sessions")
        XCTAssertNil(store.records.first?.previousTurns)
        var resumed = original
        resumed.status = .running; resumed.result = nil
        XCTAssertEqual(store.recordContinuation(previous: original, task: resumed, message: "Next"), original.id)
        let restored = AgentSessionStore(defaults: defaults)
        XCTAssertEqual(restored.records.count, 1)
        XCTAssertEqual(restored.records.first?.message, "Next")
        XCTAssertEqual(restored.records.first?.previousTurns?.first?.message, "Original instruction")
        XCTAssertEqual(restored.records.first?.previousTurns?.first?.task.result?.plainText, "First answer")
        XCTAssertEqual(restored.threads(for: [resumed]).count, 1)
        let child = AgentTask(mode: .do, status: .queued, title: "Book")
        XCTAssertEqual(restored.recordContinuation(previous: resumed, task: child, message: "Book"), original.id)
        XCTAssertEqual(restored.records.count, 2)
        XCTAssertEqual(restored.sessionID(for: child.id), original.id)
        for _ in 0..<55 { restored.recordContinuation(previous: original, task: resumed, message: "Again") }
        XCTAssertEqual(restored.records.first?.previousTurns?.count, 50)
        restored.clear()
        XCTAssertTrue(restored.records.isEmpty)
        defaults.set(Data("invalid".utf8), forKey: "gusan.agent.sessions")
        XCTAssertEqual(restored.recordContinuation(previous: original, task: resumed, message: "Recovered"), original.id)
        XCTAssertEqual(restored.records.first?.previousTurns?.first?.message, "Original")
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

    func testLoginAndCodeAttentionTransitionsPersist() throws {
        let suite = "AgentAttentionTests." + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suite))
        defer { defaults.removePersistentDomain(forName: suite) }
        let tracker = AgentTaskTracker(defaults: defaults)
        var task = AgentTask(mode: .do, status: .running, title: "Sign in", seenAt: Date())
        XCTAssertFalse(task.needsAttention)
        XCTAssertTrue(tracker.consumeEvents(from: [task]).isEmpty)
        task.setServerStatus("awaiting_login")
        XCTAssertTrue(task.needsAttention)
        XCTAssertEqual(tracker.consumeEvents(from: [task]), [.needsLogin(task)])
        XCTAssertTrue(AgentTaskTracker(defaults: defaults).consumeEvents(from: [task]).isEmpty)
        task.setServerStatus("awaiting_code")
        XCTAssertTrue(task.needsAttention)
        XCTAssertEqual(tracker.consumeEvents(from: [task]), [.needsCode(task)])
        XCTAssertTrue(tracker.consumeEvents(from: [task]).isEmpty)
        task.status = .running
        XCTAssertFalse(task.needsAttention)
        XCTAssertTrue(tracker.consumeEvents(from: [task]).isEmpty)
        task.setServerStatus("awaiting_login")
        XCTAssertEqual(tracker.consumeEvents(from: [task]), [.needsLogin(task)])
        tracker.markNotified(task)
        XCTAssertTrue(tracker.consumeEvents(from: [task]).isEmpty)
    }

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
            func delete(_ id: UUID) async throws {}
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
