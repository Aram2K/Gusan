import XCTest
@testable import GusanKit

final class VoiceTests: XCTestCase {
    final class Board: SessionBackend {
        let name: String
        var values: [String: Data] = [:]
        var writable = true
        init(_ name: String = "Team pasteboard") { self.name = name }
        func read(_ key: String) -> SessionBackendRead { .init(data: values[key], readable: true) }
        func write(_ data: Data, key: String) -> Bool { guard writable else { return false }; values[key] = data; return true }
        func clear(_ key: String) { values.removeValue(forKey: key) }
    }
    func testVoiceChannelDiagnosticReasons() {
        let board = Board(); var route = VoiceChannel.Route.pasteboard
        let channel = VoiceChannel(container: nil, group: "voice", pasteboard: board, route: { route })
        XCTAssertEqual(channel.diagnosticsDescription, "Pasteboard fallback")
        board.writable = false
        XCTAssertEqual(channel.diagnosticsDescription, "Unavailable (pasteboard read/write denied)")
        route = .fullAccessRequired
        XCTAssertEqual(channel.diagnosticsDescription, "Unavailable (Full Access is off)")
        XCTAssertEqual(channel.unavailableError, .fullAccessRequired)
        route = .unavailable
        XCTAssertEqual(channel.diagnosticsDescription, "Unavailable (no shared voice storage)")
    }
    func testPasteboardVoiceStartTranscriptResultAndCleanupWithoutAppGroup() throws {
        let board = Board()
        let keyboard = VoiceChannel(container: nil, group: "voice", pasteboard: board, route: { .pasteboard })
        let app = VoiceChannel(container: nil, group: "voice", pasteboard: board, route: { .pasteboard })
        let id = UUID(), context = VoiceContext(selected: "Original text", documentID: "doc")
        XCTAssertTrue(keyboard.available)
        XCTAssertNil(keyboard.readStatus()); XCTAssertNil(app.consumeCommand()); XCTAssertNil(app.readTranscript())
        XCTAssertEqual(KeyboardVoice.tap(status: nil, hasFullAccess: true, shared: keyboard.available, signedIn: true), .openApp)
        try keyboard.write(keyboard: KeyboardVoiceState(requestID: id, context: context))
        try keyboard.write(command: VoiceCommand(kind: .start, requestID: id, context: context))
        let launch = VoiceLaunch(requestID: id); try keyboard.write(launch: launch)
        XCTAssertTrue(app.consumeLaunch(nonce: launch.nonce.uuidString))
        XCTAssertFalse(app.consumeLaunch(nonce: launch.nonce.uuidString))
        var machine = VoiceMachine(action: .smart(.summarize))
        _ = machine.handle(.sessionStarted(Date()))
        _ = machine.handle(.command(try XCTUnwrap(app.consumeCommand()), Date()))
        XCTAssertNil(board.values["command"])
        try app.write(status: VoiceStatus(phase: machine.phase, requestID: id))
        XCTAssertTrue(try XCTUnwrap(keyboard.readStatus()).isAlive())
        try keyboard.write(command: VoiceCommand(kind: .stop, requestID: id))
        _ = machine.handle(.command(try XCTUnwrap(app.consumeCommand()), Date()))
        let transcript = VoiceTranscript(requestID: id, text: "Spoken instruction")
        try app.write(transcript: transcript)
        XCTAssertEqual(keyboard.readTranscript(), transcript)
        _ = machine.handle(.transcribed(id, transcript.text))
        _ = machine.handle(.finished(id, "Rewritten result", true))
        let done = VoiceStatus(phase: machine.phase, requestID: id, result: machine.result, replacesSelection: true)
        try app.write(status: done)
        XCTAssertEqual(keyboard.readKeyboardState()?.context, context)
        XCTAssertEqual(keyboard.readStatus()?.result, "Rewritten result")
        try keyboard.consumeResult(requestID: id)
        for key in ["command", "keyboard", "launch", "transcript", "status"] { XCTAssertNil(board.values[key]) }
        try app.write(status: done)
        XCTAssertNil(keyboard.readStatus(), "An unacknowledged result must not be republished")
        _ = machine.handle(.acknowledged(try XCTUnwrap(app.consumeReceipt())))
        XCTAssertNil(machine.result); XCTAssertNil(machine.context)
        try app.write(status: VoiceStatus(phase: machine.phase))
        XCTAssertNil(keyboard.readStatus()?.result)
        _ = machine.handle(.sessionStopped)
        try app.write(status: VoiceStatus(phase: machine.phase))
        XCTAssertTrue(board.values.isEmpty)
        try app.write(transcript: transcript); try keyboard.write(keyboard: KeyboardVoiceState(requestID: id, context: context))
        try app.write(status: VoiceStatus(phase: .failed, requestID: id, message: "Network error"))
        XCTAssertNil(board.values["transcript"]); XCTAssertNil(app.readKeyboardState()?.context)
        app.clear(); XCTAssertTrue(board.values.isEmpty)
    }
    func testVoiceFallbackOffPurgesDataAndRejectsRequests() throws {
        let board = Board(); var enabled = true
        let channel = VoiceChannel(container: nil, group: "voice", pasteboard: board, route: { enabled ? .pasteboard : .disabled })
        try channel.write(command: VoiceCommand(kind: .start, requestID: UUID(), context: VoiceContext(selected: "private")))
        enabled = false
        XCTAssertFalse(channel.available); XCTAssertTrue(board.values.isEmpty)
        XCTAssertEqual(channel.unavailableError, .fallbackOff)
        XCTAssertThrowsError(try channel.write(command: VoiceCommand(kind: .start, requestID: UUID()))) { XCTAssertEqual($0 as? VoiceError, .fallbackOff) }
        XCTAssertEqual(KeyboardVoice.tap(status: nil, hasFullAccess: true, shared: false, signedIn: true, fallbackOff: true), .error(.fallbackOff))
        XCTAssertEqual(KeyboardVoice.tap(status: nil, hasFullAccess: false, shared: false, signedIn: true, fallbackOff: true), .error(.fullAccessRequired))
        enabled = true; board.writable = false
        XCTAssertFalse(channel.available)
        XCTAssertThrowsError(try channel.write(command: VoiceCommand(kind: .start, requestID: UUID()))) { XCTAssertEqual($0 as? VoiceError, .notShared) }
    }
    func testAbandonedPasteboardVoiceDataExpires() throws {
        let board = Board()
        let channel = VoiceChannel(container: nil, group: "voice", pasteboard: board, route: { .pasteboard })
        for age in [-60.0, 601.0] {
            board.values["status"] = try JSONSerialization.data(withJSONObject: ["issuedAt": Date().addingTimeInterval(-age).timeIntervalSinceReferenceDate, "value": ["phase": "done", "heartbeat": Date().timeIntervalSinceReferenceDate, "level": 0, "result": "Expired", "replacesSelection": false]])
            XCTAssertNil(channel.readStatus()); XCTAssertNil(board.values["status"])
        }
    }
    func testStartupFailureSurvivesStopUntilReturningKeyboardAcknowledgesIt() throws {
        for pasteboard in [false, true] {
            let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
            defer { try? FileManager.default.removeItem(at: directory) }
            let board = Board()
            let channel = VoiceChannel(container: directory, group: "voice", pasteboard: board, route: { pasteboard ? .pasteboard : .file })
            let id = UUID()
            try channel.write(keyboard: KeyboardVoiceState(requestID: id, context: VoiceContext(selected: "private text")))
            try channel.write(command: VoiceCommand(kind: .start, requestID: id))
            let failure = channel.failureStatus(message: VoiceError.micPermissionDenied.localizedDescription)
            try channel.write(status: VoiceStatus(phase: .off))
            XCTAssertNil(channel.readCommand()); XCTAssertNil(channel.readKeyboardState())
            try channel.write(status: failure)
            XCTAssertEqual(channel.readKeyboardState()?.requestID, id)
            XCTAssertNil(channel.readKeyboardState()?.context)
            XCTAssertEqual(channel.readStatus()?.requestID, id)
            XCTAssertEqual(channel.readStatus()?.message, VoiceError.micPermissionDenied.localizedDescription)
            XCTAssertEqual(channel.readStatus(), failure, "Reading must not clear an unseen failure")
            try channel.consumeResult(requestID: id)
            XCTAssertNil(channel.readStatus()); XCTAssertNil(channel.readKeyboardState())
            try channel.write(status: failure)
            XCTAssertNil(channel.readStatus(), "An acknowledged failure must not be republished")
            channel.clear(); XCTAssertTrue(board.values.isEmpty)
        }
    }
    func testDisablingPasteboardDoesNotClearActiveFileVoiceState() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let board = Board()
        let channel = VoiceChannel(container: directory, group: "voice", pasteboard: board, route: { .file })
        let id = UUID(), state = KeyboardVoiceState(requestID: id, context: VoiceContext(selected: "original"))
        let command = VoiceCommand(kind: .start, requestID: id), launch = VoiceLaunch(requestID: id)
        let status = VoiceStatus(phase: .recording, requestID: id), transcript = VoiceTranscript(requestID: id, text: "speech")
        try channel.write(keyboard: state); try channel.write(command: command); try channel.write(launch: launch)
        try channel.write(status: status); try channel.write(transcript: transcript)
        board.values["status"] = Data("old pasteboard state".utf8)
        channel.clearPasteboard()
        XCTAssertTrue(board.values.isEmpty)
        XCTAssertEqual(channel.readCommand(), command); XCTAssertEqual(channel.readKeyboardState(), state)
        XCTAssertEqual(channel.readStatus(), status); XCTAssertEqual(channel.readTranscript(), transcript)
        XCTAssertTrue(channel.consumeLaunch(nonce: launch.nonce.uuidString))
        XCTAssertTrue(channel.available)
    }
    func testVoiceUsesPasteboardWhenFileGroupsDifferEvenIfKeychainIsShared() throws {
        let file = Board("App-group file"), keychain = Board("Keychain"), reports = Board(), voice = Board("voice")
        let peer = SessionReport(role: .keyboard, timestamp: Date(), groupID: "other", keychainGroupID: "app", stores: ["Keychain", "App-group file"].map { .init(name: $0, readable: true, writable: true, hasSession: false) }, fullAccess: true)
        reports.values["report.keyboard"] = try JSONEncoder().encode(peer)
        let resolution = AppGroupResolution(candidatesTried: [], chosen: "app", container: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
        let store = SessionStore(resolution: resolution, backends: [file, keychain, reports], privateBackend: Board("private"), voicePasteboard: voice)
        let channel = store.makeVoiceChannel()
        try channel.write(command: VoiceCommand(kind: .start, requestID: UUID()))
        XCTAssertNotNil(voice.values["command"])
        channel.clear(); XCTAssertTrue(voice.values.isEmpty)
    }
    func testVoiceNormalSharingAndLocalFailureRespectToggle() throws {
        for (writable, enabled) in [(true, true), (true, false), (false, true), (false, false)] {
            let file = Board("App-group file"), reports = Board(), voice = Board("voice")
            file.writable = writable
            let peer = SessionReport(role: .keyboard, timestamp: Date(), groupID: "app", keychainGroupID: "app", stores: [.init(name: "App-group file", readable: true, writable: true, hasSession: false)], fullAccess: true)
            reports.values["report.keyboard"] = try JSONEncoder().encode(peer)
            voice.values["status"] = Data("old".utf8)
            let resolution = AppGroupResolution(candidatesTried: [], chosen: "app", container: FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString))
            let store = SessionStore(resolution: resolution, backends: [file, reports], privateBackend: Board("private"), pasteboardFallbackAllowed: { enabled }, voicePasteboard: voice)
            let channel = store.makeVoiceChannel()
            XCTAssertEqual(channel.available, writable || enabled)
            let expected = writable ? "App Group" : (enabled ? "Pasteboard fallback" : "Unavailable (pasteboard fallback is off)")
            XCTAssertEqual(store.diagnostics().localVoiceChannel, expected)
            let combined = !writable && enabled ? "Unavailable (keyboard cannot read/write the pasteboard)" : expected
            XCTAssertEqual(store.diagnostics().voiceChannel, combined)
            XCTAssertTrue(store.diagnostics().summary.contains("Voice: " + combined))
            if writable || !enabled { XCTAssertTrue(voice.values.isEmpty) }
            if !writable && !enabled { XCTAssertEqual(channel.unavailableError, .fallbackOff) }
            channel.clear()
        }
    }
    func testLiveVoiceInstruction() async throws {
        let environment = ProcessInfo.processInfo.environment
        guard let token = environment["GUSAN_LIVE_ACCESS_TOKEN"], !token.isEmpty else { throw XCTSkip("Set GUSAN_LIVE_ACCESS_TOKEN to run live voice integration.") }
        let path = try XCTUnwrap(environment["GUSAN_LIVE_WAV"], "Set GUSAN_LIVE_WAV to a WAV containing 'quarterly report'.")
        let wav = try Data(contentsOf: URL(fileURLWithPath: path))
        let transcript = try await TranscribeClient().run(wav: wav, language: "en", accessToken: token)
        print("Live transcript: \(transcript)")
        XCTAssertTrue(transcript.localizedCaseInsensitiveContains("quarterly report"))
        let result = try await SmartActionClient().run(instruction: "Make this more formal", text: transcript, accessToken: token)
        print("Live instruction result: \(result)")
        XCTAssertFalse(result.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
    }
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
    func testVoiceLaunchRequiresFreshMatchingOneTimeKeyboardRequest() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let channel = VoiceChannel(container: directory)
        let id = UUID(), launch = VoiceLaunch(requestID: id, issuedAt: now)
        XCTAssertFalse(channel.consumeLaunch(nonce: nil, now: now))
        try channel.write(launch: launch)
        XCTAssertFalse(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now))
        try channel.write(command: VoiceCommand(kind: .start, requestID: UUID(), sentAt: now))
        XCTAssertFalse(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now))
        try channel.write(command: VoiceCommand(kind: .start, requestID: id, sentAt: now))
        XCTAssertFalse(channel.consumeLaunch(nonce: UUID().uuidString, now: now))
        XCTAssertFalse(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now.addingTimeInterval(-1)))
        XCTAssertFalse(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now.addingTimeInterval(60)))
        XCTAssertTrue(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now.addingTimeInterval(59)))
        XCTAssertFalse(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now.addingTimeInterval(59)))
        try channel.write(launch: launch)
        try channel.write(command: VoiceCommand(kind: .stop, requestID: id, sentAt: now))
        XCTAssertFalse(channel.consumeLaunch(nonce: launch.nonce.uuidString, now: now))
    }
    func testKeyboardStateRoundTrip() throws {
        let directory = FileManager.default.temporaryDirectory.appendingPathComponent(UUID().uuidString)
        defer { try? FileManager.default.removeItem(at: directory) }
        let channel = VoiceChannel(container: directory)
        XCTAssertNil(channel.readKeyboardState())
        var state = KeyboardVoiceState(requestID: UUID(), context: VoiceContext(selected: "selection", before: "before", after: "after", documentID: "old"), startedAt: now)
        try channel.write(keyboard: state)
        XCTAssertEqual(VoiceChannel(container: directory).readKeyboardState(), state)
        state.insertedID = state.requestID
        try channel.write(keyboard: state)
        XCTAssertEqual(channel.readKeyboardState(), state)
        XCTAssertThrowsError(try VoiceChannel(container: nil).write(keyboard: state)) { XCTAssertEqual($0 as? VoiceError, .notShared) }
    }
    func testKeyboardDeliveryRules() {
        let state = KeyboardVoiceState(requestID: UUID(), context: VoiceContext(selected: "selection", documentID: "old"), startedAt: now)
        XCTAssertTrue(state.canInsert(context: VoiceContext(selected: "selection", documentID: "old")))
        XCTAssertFalse(state.canInsert(context: VoiceContext(selected: "changed", documentID: "old")))
        XCTAssertFalse(state.canInsert(context: VoiceContext(selected: "selection", documentID: "different field")))
        XCTAssertFalse(state.canInsert(context: VoiceContext(selected: "selection", before: "changed", documentID: "old")))
        XCTAssertFalse(state.canInsert(context: VoiceContext(selected: "selection", after: "changed", documentID: "old")))
        let empty = KeyboardVoiceState(context: VoiceContext(documentID: "doc"), startedAt: now)
        XCTAssertTrue(empty.canInsert(context: VoiceContext(documentID: "doc")))
        XCTAssertFalse(empty.canInsert(context: VoiceContext(documentID: "other")))
        XCTAssertFalse(KeyboardVoiceState(context: VoiceContext()).canInsert(context: VoiceContext()))
        XCTAssertFalse(KeyboardVoiceState().canInsert(context: VoiceContext()))
        let status = VoiceStatus(phase: .done, requestID: state.requestID, heartbeat: now, result: "result")
        XCTAssertFalse(state.isStale(status: status, now: now.addingTimeInterval(599)))
        XCTAssertTrue(state.isStale(status: status, now: now.addingTimeInterval(600)))
        let fresh = KeyboardVoiceState(startedAt: now.addingTimeInterval(601))
        XCTAssertTrue(fresh.isStale(status: status, now: now.addingTimeInterval(601)))
        let renewed = VoiceStatus(phase: .done, heartbeat: now.addingTimeInterval(601))
        XCTAssertTrue(state.isStale(status: renewed, now: now.addingTimeInterval(601)))
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
        XCTAssertEqual(machine.handle(.transcribed(id, "hello")), [.runAction(id, "hello", nil), .publish])
        XCTAssertEqual(machine.handle(.completed(id, JobResult(text: "hello", kind: .insert))), [.deliver("hello"), .publish])
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
        XCTAssertEqual(KeyboardVoice.tap(status: nil, now: now, hasFullAccess: false, shared: true, signedIn: true), .error(.fullAccessRequired))
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
