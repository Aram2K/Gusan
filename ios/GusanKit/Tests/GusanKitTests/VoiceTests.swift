import XCTest
@testable import GusanKit

final class VoiceTests: XCTestCase {
    let now = Date(timeIntervalSince1970: 1000)
    func response(_ code: Int) -> HTTPURLResponse { HTTPURLResponse(url: TranscribeClient.url, statusCode: code, httpVersion: nil, headerFields: nil)! }
    func json(_ body: Any) throws -> Data { try JSONSerialization.data(withJSONObject: body) }
    func body(_ request: URLRequest) throws -> [String: Any] { try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request.httpBody)) as? [String: Any]) }
    func testTranscribeRequest() throws {
        let wav = WAV.encode(samples: [1, -2, 32767])
        for (input, expected) in [("en", "en"), ("hy", "hy"), (" RU ", "ru"), ("auto", "auto"), ("", "auto"), ("unknown", "auto")] {
            let request = try TranscribeClient().request(wav: wav, language: input, accessToken: "token")
            XCTAssertEqual(request.url, TranscribeClient.url)
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.timeoutInterval, 90)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            let payload = try body(request)
            XCTAssertEqual(Set(payload.keys), Set(["audio", "sample_rate", "language", "provider", "languages", "model"]))
            XCTAssertEqual(Data(base64Encoded: try XCTUnwrap(payload["audio"] as? String)), wav)
            XCTAssertEqual(payload["sample_rate"] as? Int, 16000)
            XCTAssertEqual(payload["language"] as? String, expected)
            XCTAssertEqual(payload["provider"] as? String, "gemini")
            XCTAssertEqual(payload["model"] as? String, "fast")
            XCTAssertEqual(payload["languages"] as? [String], expected == "auto" ? [] : [expected])
        }
        for count in [0, 44] {
            XCTAssertThrowsError(try TranscribeClient().request(wav: Data(count: count), language: "en", accessToken: "x")) { XCTAssertEqual($0 as? VoiceError, .nothingHeard) }
        }
        XCTAssertThrowsError(try TranscribeClient().request(wav: Data(count: 18 * 1024 * 1024 + 1), language: "en", accessToken: "x")) { XCTAssertEqual($0 as? VoiceError, .tooLong) }
    }
    func testTranscribeResponses() throws {
        XCTAssertEqual(try TranscribeClient.parse(json(["text": " hello \n", "lang": "en", "model": "fast"]), response: response(200)), "hello")
        for (code, error) in [(401, VoiceError.unauthorized), (403, .proRequired), (413, .tooLong), (429, .dailyLimit), (503, .serverNotConfigured), (502, .transcriptionFailed), (500, .transcriptionFailed)] {
            XCTAssertThrowsError(try TranscribeClient.parse(Data(), response: response(code))) { XCTAssertEqual($0 as? VoiceError, error) }
        }
        XCTAssertThrowsError(try TranscribeClient.parse(json(["error": "failed", "detail": "No engine configured"]), response: response(502))) { XCTAssertEqual($0 as? VoiceError, .noEngine) }
        for data in [Data("bad".utf8), try json(["lang": "en"])] {
            XCTAssertThrowsError(try TranscribeClient.parse(data, response: response(200))) { XCTAssertEqual($0 as? VoiceError, .invalidResponse) }
        }
        XCTAssertThrowsError(try TranscribeClient.parse(json(["text": " \n"]), response: response(200))) { XCTAssertEqual($0 as? VoiceError, .nothingHeard) }
    }
    func testTranscribeNetwork() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let client = TranscribeClient(transport: URLSession(configuration: config))
        for (code, expected) in [(URLError.Code.timedOut, VoiceError.timeout), (.notConnectedToInternet, .offline), (.networkConnectionLost, .offline), (.dataNotAllowed, .offline), (.badServerResponse, .network)] {
            StubProtocol.handler = { _ in throw URLError(code) }
            do { _ = try await client.run(wav: WAV.encode(samples: [1]), language: "en", accessToken: "x"); XCTFail() }
            catch { XCTAssertEqual(error as? VoiceError, expected) }
        }
        StubProtocol.handler = { _ in (200, try self.json(["text": "hi"]), [:]) }
        let result = try await client.run(wav: WAV.encode(samples: [1]), language: "auto", accessToken: "x")
        XCTAssertEqual(result, "hi")
    }
    func testWAV() {
        let bytes = Array(WAV.encode(samples: [0x1234, -2]))
        XCTAssertEqual(bytes.count, 48)
        XCTAssertEqual(String(bytes: bytes[0..<4], encoding: .utf8), "RIFF")
        XCTAssertEqual(Array(bytes[4..<8]), [40, 0, 0, 0])
        XCTAssertEqual(String(bytes: bytes[8..<16], encoding: .utf8), "WAVEfmt ")
        XCTAssertEqual(Array(bytes[16..<36]), [16, 0, 0, 0, 1, 0, 1, 0, 128, 62, 0, 0, 0, 125, 0, 0, 2, 0, 16, 0])
        XCTAssertEqual(String(bytes: bytes[36..<40], encoding: .utf8), "data")
        XCTAssertEqual(Array(bytes[40...]), [4, 0, 0, 0, 0x34, 0x12, 0xfe, 0xff])
        XCTAssertEqual(WAV.encode(samples: []).count, 44)
    }
    func testSettingsAndActions() throws {
        for action in VoiceAction.all {
            let data = try JSONEncoder().encode(action)
            XCTAssertEqual(try JSONDecoder().decode(VoiceAction.self, from: data), action)
            XCTAssertEqual(try JSONDecoder().decode(String.self, from: data), action.rawValue)
            XCTAssertFalse(action.label.isEmpty)
        }
        XCTAssertThrowsError(try JSONDecoder().decode(VoiceAction.self, from: Data("\"bad\"".utf8)))
        let suite = "VoiceTests.\(UUID())"
        defer { UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite) }
        let settings = VoiceSettings(group: suite)
        XCTAssertEqual(settings.language, "auto"); XCTAssertEqual(settings.defaultAction, .transcribeOnly)
        XCTAssertEqual(settings.idleMinutes, 5); XCTAssertTrue(settings.autoReturn)
        settings.language = "hy"; settings.defaultAction = .smart(.translate); settings.idleMinutes = 30; settings.autoReturn = false
        let read = VoiceSettings(group: suite)
        XCTAssertEqual(read.language, "hy"); XCTAssertEqual(read.defaultAction, .smart(.translate)); XCTAssertEqual(read.idleMinutes, 30); XCTAssertFalse(read.autoReturn)
    }
    func testChannel() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let channel = VoiceChannel(container: directory, group: "test")
        XCTAssertNil(channel.readStatus()); XCTAssertNil(channel.readCommand())
        let status = VoiceStatus(phase: .recording, requestID: UUID(), heartbeat: now, level: 0.5)
        let command = VoiceCommand(kind: .start, requestID: UUID(), context: VoiceContext(selected: "text", documentID: "doc"), sentAt: now)
        try channel.write(status: status); try channel.write(command: command)
        XCTAssertEqual(channel.readStatus(), status); XCTAssertEqual(channel.readCommand(), command)
        XCTAssertEqual(channel.name(.command), "gusan.voice.command.test")
        XCTAssertTrue(status.isAlive(now: now.addingTimeInterval(2.9))); XCTAssertFalse(status.isAlive(now: now.addingTimeInterval(3)))
        XCTAssertFalse(VoiceStatus(phase: .off, heartbeat: now).isAlive(now: now))
        XCTAssertThrowsError(try VoiceChannel(container: nil).write(status: status)) { XCTAssertEqual($0 as? VoiceError, .notShared) }
    }
    func start(_ machine: inout VoiceMachine, id: UUID, context: VoiceContext? = nil) {
        _ = machine.handle(.sessionStarted(now))
        XCTAssertEqual(machine.handle(.command(VoiceCommand(kind: .start, requestID: id, context: context, sentAt: now), now)), [.beginCapture, .publish])
    }
    func stop(_ machine: inout VoiceMachine, id: UUID) {
        XCTAssertEqual(machine.handle(.command(VoiceCommand(kind: .stop, requestID: id, sentAt: now), now)), [.finishCapture(id), .publish])
    }
    func testMachineHappyAndStale() {
        var machine = VoiceMachine(); let id = UUID()
        start(&machine, id: id)
        XCTAssertEqual(machine.phase, .recording)
        XCTAssertEqual(machine.handle(.command(VoiceCommand(kind: .stop, requestID: UUID(), sentAt: now), now)), [])
        stop(&machine, id: id)
        XCTAssertEqual(machine.handle(.transcribed(UUID(), "stale")), [])
        XCTAssertEqual(machine.handle(.failed(UUID(), "stale")), [])
        XCTAssertEqual(machine.handle(.finished(UUID(), "stale", false)), [])
        XCTAssertEqual(machine.handle(.transcribed(id, "hello")), [.deliver("hello"), .publish])
        XCTAssertEqual(machine.phase, .done); XCTAssertEqual(machine.result, "hello")
    }
    func testPlansAndSmartMachine() {
        XCTAssertEqual(VoicePlan.plan(transcript: "hello", context: nil, action: .transcribeOnly), .insert("hello"))
        XCTAssertEqual(VoicePlan.plan(transcript: "hello", context: nil, action: .smart(.writeEmail)), .smart(.writeEmail, "hello"))
        let context = VoiceContext(selected: "selection")
        XCTAssertEqual(VoicePlan.plan(transcript: "shorten", context: context, action: .transcribeOnly), .instruction(instruction: "shorten", text: "selection"))
        for selection in [nil, context] {
            var machine = VoiceMachine(action: .smart(.summarize)); let id = UUID()
            start(&machine, id: id, context: selection); stop(&machine, id: id)
            XCTAssertEqual(machine.handle(.transcribed(id, "hello")), [.runAction(id, "hello", selection), .publish])
            XCTAssertEqual(machine.phase, .thinking)
            XCTAssertEqual(machine.handle(.finished(id, "result", selection != nil)), [.deliver("result"), .publish])
            XCTAssertEqual(machine.result, "result"); XCTAssertEqual(machine.replacesSelection, selection != nil)
        }
    }
    func testCancelFailureAndEnd() {
        for phase in [VoicePhase.recording, .transcribing, .thinking] {
            var machine = VoiceMachine(action: .smart(.smartAuto)); let id = UUID()
            start(&machine, id: id)
            if phase != .recording { stop(&machine, id: id) }
            if phase == .thinking { _ = machine.handle(.transcribed(id, "hello")) }
            var failed = machine
            XCTAssertEqual(failed.handle(.failed(id, "oops")), [.discardCapture, .cancelWork, .publish]); XCTAssertEqual(failed.message, "oops")
            XCTAssertEqual(machine.handle(.command(VoiceCommand(kind: .cancel, requestID: id, sentAt: now), now)), [phase == .recording ? .discardCapture : .cancelWork, .publish])
            XCTAssertEqual(machine.phase, .ready)
            XCTAssertEqual(machine.handle(.transcribed(id, "late")), [])
            XCTAssertEqual(machine.handle(.command(VoiceCommand(kind: .endSession, requestID: id, sentAt: now), now)), [.stopSession, .publish])
            XCTAssertEqual(machine.phase, .off)
        }
    }
    func testTimeoutAndOldCommand() {
        var machine = VoiceMachine(idleTimeout: 60); let id = UUID()
        _ = machine.handle(.sessionStarted(now))
        XCTAssertEqual(machine.handle(.command(VoiceCommand(kind: .start, requestID: id, sentAt: now.addingTimeInterval(-16)), now)), [])
        XCTAssertEqual(machine.handle(.tick(now.addingTimeInterval(59))), [])
        XCTAssertEqual(machine.handle(.tick(now.addingTimeInterval(60))), [.stopSession, .publish])
        start(&machine, id: id)
        XCTAssertEqual(machine.handle(.tick(now.addingTimeInterval(299))), [])
        XCTAssertEqual(machine.handle(.tick(now.addingTimeInterval(300))), [.finishCapture(id), .publish])
        XCTAssertEqual(machine.phase, .transcribing)
        _ = machine.handle(.sessionStopped); XCTAssertEqual(machine.phase, .off)
    }
    func testTapTable() {
        XCTAssertEqual(KeyboardVoice.tap(status: nil, now: now, hasFullAccess: false, shared: true, signedIn: true), .error(.notShared))
        XCTAssertEqual(KeyboardVoice.tap(status: nil, now: now, hasFullAccess: true, shared: false, signedIn: true), .error(.notShared))
        XCTAssertEqual(KeyboardVoice.tap(status: nil, now: now, hasFullAccess: true, shared: true, signedIn: false), .error(.notSignedIn))
        XCTAssertEqual(KeyboardVoice.tap(status: nil, now: now, hasFullAccess: true, shared: true, signedIn: true), .openApp)
        for (phase, expected) in [(VoicePhase.off, KeyboardVoice.TapResult.openApp), (.ready, .start), (.done, .start), (.failed, .start), (.recording, .stop), (.transcribing, .cancel), (.thinking, .cancel)] {
            XCTAssertEqual(KeyboardVoice.tap(status: VoiceStatus(phase: phase, heartbeat: now), now: now, hasFullAccess: true, shared: true, signedIn: true), expected)
            XCTAssertEqual(KeyboardVoice.tap(status: VoiceStatus(phase: phase, heartbeat: now.addingTimeInterval(-4)), now: now, hasFullAccess: true, shared: true, signedIn: true), .openApp)
        }
    }
    func testInstructionRequest() throws {
        let request = try SmartActionClient().request(instruction: "shorten", text: "long text", accessToken: "token")
        XCTAssertEqual(request.url, SmartActionClient.url); XCTAssertEqual(request.httpMethod, "POST"); XCTAssertEqual(request.timeoutInterval, 45)
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let payload = try body(request)
        XCTAssertEqual(payload["mode"] as? String, "smart_auto"); XCTAssertEqual(payload["max_tokens"] as? Int, 2000)
        let messages = try XCTUnwrap(payload["messages"] as? [[String: String]])
        XCTAssertEqual(messages.count, 2)
        XCTAssertEqual(messages[0], ["role": "system", "content": "You edit text for the user. Apply the user's spoken instruction to the text and output only the resulting text - no commentary, no quotes. Keep the text's language unless the instruction says otherwise."])
        XCTAssertEqual(messages[1], ["role": "user", "content": "Instruction:\nshorten\n\nText:\nlong text"])
        XCTAssertThrowsError(try SmartActionClient().request(instruction: "", text: "text", accessToken: "x"))
        XCTAssertThrowsError(try SmartActionClient().request(instruction: "edit", text: "", accessToken: "x"))
    }
}
