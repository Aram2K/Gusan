import XCTest
import CoreGraphics
@testable import GusanKit

final class MemoryBackend: SessionBackend {
    let name: String
    var values: [String: Data] = [:]
    var writable = true
    var writes = 0
    init(name: String = "Memory") { self.name = name }
    func read(_ key: String) -> SessionBackendRead { SessionBackendRead(data: values[key], readable: true) }
    func write(_ data: Data, key: String) -> Bool { guard writable else { return false }; values[key] = data; writes += 1; return true }
    func clear(_ key: String) { values[key] = nil }
}

private func testImage(width: Int, height: Int) -> CGImage {
    let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
    context.setFillColor(CGColor(red: 0.1, green: 0.3, blue: 0.9, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: width, height: height))
    for row in stride(from: 0, to: height, by: 8) {
        context.setFillColor(CGColor(red: Double(row % 255) / 255, green: 0.9, blue: 0.2, alpha: 1))
        context.fill(CGRect(x: 0, y: row, width: width / 2, height: 4))
    }
    return context.makeImage()!
}

final class ScreenFrameTests: XCTestCase {
    func testScaledSizeKeepsAspectAndNeverUpscales() {
        XCTAssertEqual(ScreenFrameGeometry.scaledSize(width: 1179, height: 2556).width, 590)
        XCTAssertEqual(ScreenFrameGeometry.scaledSize(width: 1179, height: 2556).height, 1280)
        XCTAssertEqual(ScreenFrameGeometry.scaledSize(width: 2556, height: 1179).width, 1280)
        XCTAssertEqual(ScreenFrameGeometry.scaledSize(width: 600, height: 900).width, 600)
        XCTAssertEqual(ScreenFrameGeometry.scaledSize(width: 0, height: 900).width, 0)
        XCTAssertEqual(ScreenFrameGeometry.scale(width: 2560, height: 1000), 0.5, accuracy: 0.001)
    }

    func testStoreKeepsOnlyLatestAndThrottles() {
        let store = ScreenFrameStore(minimumInterval: 0.4)
        let start = Date()
        XCTAssertTrue(store.shouldAccept(at: start))
        store.store(testImage(width: 20, height: 40), at: start)
        XCTAssertFalse(store.shouldAccept(at: start.addingTimeInterval(0.2)))
        XCTAssertTrue(store.shouldAccept(at: start.addingTimeInterval(0.45)))
        store.store(testImage(width: 30, height: 60), at: start.addingTimeInterval(0.5))
        XCTAssertEqual(store.accepted, 2)
        XCTAssertEqual(store.latest?.width, 30)
        let frame = store.frame(for: UUID())
        XCTAssertEqual(frame?.width, 30); XCTAssertEqual(frame?.height, 60)
        XCTAssertEqual(frame?.capturedAt, start.addingTimeInterval(0.5))
        XCTAssertEqual(frame?.jpeg.prefix(3), Data([0xFF, 0xD8, 0xFF]))
        store.clear()
        XCTAssertNil(store.latest); XCTAssertNil(store.frame(for: UUID()))
        XCTAssertTrue(store.shouldAccept(at: start.addingTimeInterval(0.55)))
    }

    func testJPEGBudgetLowersQuality() throws {
        let image = testImage(width: 640, height: 1280)
        let full = try XCTUnwrap(ScreenFrameEncoder.jpeg(image, quality: 0.75))
        let budgeted = try XCTUnwrap(ScreenFrameEncoder.jpeg(image, budgetBytes: full.count / 2))
        XCTAssertLessThan(budgeted.count, full.count)
        XCTAssertEqual(ScreenFrameEncoder.jpeg(image, budgetBytes: 10_000_000), full)
        let floor = try XCTUnwrap(ScreenFrameEncoder.jpeg(image, quality: ScreenFrameEncoder.qualities.last!))
        XCTAssertEqual(ScreenFrameEncoder.jpeg(image, budgetBytes: 1), floor)
        XCTAssertTrue(ScreenFrame(requestID: UUID(), capturedAt: Date(), width: 1, height: 1, jpeg: Data([1, 2, 3])).dataURL.hasPrefix("data:image/jpeg;base64,AQID"))
    }

    func testIdleTimer() {
        let start = Date()
        var timer = ScreenIdleTimer(timeout: 600, startedAt: start)
        XCTAssertEqual(timer.deadline, start.addingTimeInterval(600))
        XCTAssertFalse(timer.expired(at: start.addingTimeInterval(599)))
        XCTAssertTrue(timer.expired(at: start.addingTimeInterval(600)))
        timer.touch(at: start.addingTimeInterval(300))
        XCTAssertFalse(timer.expired(at: start.addingTimeInterval(800)))
        XCTAssertTrue(timer.expired(at: start.addingTimeInterval(900)))
        timer.touch(at: start)
        XCTAssertEqual(timer.lastActivity, start.addingTimeInterval(300), "touching with an earlier date never moves the deadline back")
        XCTAssertEqual(ScreenIdleTimer.defaultMinutes, 10)
        XCTAssertTrue(ScreenIdleTimer.minuteOptions.contains(ScreenIdleTimer.defaultMinutes))
    }
}

final class ScreenChannelTests: XCTestCase {
    private var clock = Date()
    private func channel(_ stores: [SessionBackend]) -> ScreenChannel { ScreenChannel(stores: stores, now: { self.clock }) }

    func testWritesEverywhereAndReadsNewest() {
        let file = MemoryBackend(name: "file"), board = MemoryBackend(name: "board")
        let channel = channel([file, board])
        XCTAssertTrue(channel.available)
        XCTAssertEqual(channel.diagnosticsDescription, "file + board")
        let status = ScreenStatus(startedAt: clock, heartbeat: clock, idleDeadline: clock.addingTimeInterval(600), width: 590, height: 1280)
        XCTAssertTrue(channel.write(status: status))
        XCTAssertNotNil(file.values["status"]); XCTAssertNotNil(board.values["status"])
        XCTAssertEqual(channel.readStatus(), status)
        XCTAssertTrue(channel.isLive())
        // The copy only one side can see still counts, and the newest copy wins.
        file.values["status"] = nil
        XCTAssertEqual(channel.readStatus(), status)
        clock = clock.addingTimeInterval(5)
        let later = ScreenStatus(startedAt: status.startedAt, heartbeat: clock, idleDeadline: status.idleDeadline, requests: 1)
        _ = ScreenChannel(stores: [file], now: { self.clock }).write(status: later)
        XCTAssertEqual(channel.readStatus(), later)
        clock = clock.addingTimeInterval(ScreenStatus.liveWindow)
        XCTAssertFalse(channel.isLive())
        channel.clearStatus()
        XCTAssertNil(channel.readStatus()); XCTAssertNil(board.values["status"])
    }

    func testBrokenStoreIsSkipped() {
        let file = MemoryBackend(name: "file"), board = MemoryBackend(name: "board")
        file.writable = false
        let channel = channel([file, board])
        XCTAssertTrue(channel.write(request: ScreenRequest()))
        XCTAssertNil(file.values["request"]); XCTAssertNotNil(board.values["request"])
        board.writable = false
        XCTAssertFalse(channel.write(request: ScreenRequest()))
        XCTAssertFalse(channel.available)
        XCTAssertEqual(channel.diagnosticsDescription, "Unavailable")
    }

    func testPacketsExpire() {
        let store = MemoryBackend()
        let channel = channel([store])
        let request = ScreenRequest(issuedAt: clock)
        channel.write(request: request)
        clock = clock.addingTimeInterval(29); XCTAssertEqual(channel.readRequest(), request)
        clock = clock.addingTimeInterval(2); XCTAssertNil(channel.readRequest())
        XCTAssertNil(store.values["request"], "an expired packet is removed")
        let frame = ScreenFrame(requestID: request.id, capturedAt: clock, width: 1, height: 1, jpeg: Data([1]))
        channel.write(frame: frame)
        clock = clock.addingTimeInterval(46); XCTAssertNil(channel.readFrame())
        clock = clock.addingTimeInterval(-100)
        channel.write(status: ScreenStatus(startedAt: clock, idleDeadline: clock))
        clock = clock.addingTimeInterval(-61); XCTAssertNil(channel.readStatus(), "a packet from the far future is distrusted")
    }

    func testRequestAndFrameHandshake() {
        let store = MemoryBackend()
        let channel = channel([store])
        let request = ScreenRequest()
        channel.write(request: request)
        XCTAssertEqual(channel.consumeRequest(), request)
        XCTAssertNil(channel.consumeRequest())
        let frame = ScreenFrame(requestID: request.id, capturedAt: clock, width: 2, height: 3, jpeg: Data([9, 9]))
        channel.write(frame: frame)
        XCTAssertNil(channel.consumeFrame(requestID: UUID()), "a frame for another request is left alone")
        XCTAssertNotNil(store.values["frame"])
        XCTAssertEqual(channel.consumeFrame(requestID: request.id), frame)
        XCTAssertNil(store.values["frame"], "the frame is gone as soon as the keyboard takes it")
        XCTAssertNil(channel.consumeFrame(requestID: request.id))
        XCTAssertEqual(channel.name(.frame), "gusan.screen.frame.com.aram2k.gusan")
        XCTAssertEqual(channel.name(.request), "gusan.screen.request.com.aram2k.gusan")
    }

    func testFrameProviderRoundTrip() async throws {
        let store = MemoryBackend()
        let channel = ScreenChannel(stores: [store])
        channel.write(status: ScreenStatus(startedAt: Date(), idleDeadline: Date().addingTimeInterval(60), width: 1, height: 1))
        let provider = ScreenChannelFrameProvider(channel: channel, pollInterval: 0.02)
        let extensionSide = Task.detached {
            while channel.readRequest() == nil { try await Task.sleep(nanoseconds: 5_000_000) }
            let request = try XCTUnwrap(channel.consumeRequest())
            channel.write(frame: ScreenFrame(requestID: request.id, capturedAt: Date(), width: 4, height: 8, jpeg: Data([7])))
        }
        let frame = try await provider.frame(timeout: 2)
        try await extensionSide.value
        XCTAssertEqual(frame.width, 4); XCTAssertEqual(frame.jpeg, Data([7]))
        XCTAssertNil(store.values["frame"]); XCTAssertNil(store.values["request"])
    }

    func testFrameProviderFailures() async {
        let store = MemoryBackend()
        let channel = ScreenChannel(stores: [store])
        let provider = ScreenChannelFrameProvider(channel: channel, pollInterval: 0.02)
        do { _ = try await provider.frame(timeout: 0.1); XCTFail() } catch { XCTAssertEqual(error as? ScreenError, .notLive) }
        channel.write(status: ScreenStatus(startedAt: Date(), idleDeadline: Date().addingTimeInterval(60)))
        do { _ = try await provider.frame(timeout: 0.1); XCTFail() } catch { XCTAssertEqual(error as? ScreenError, .noFrame) }
        XCTAssertNil(store.values["request"], "an unanswered request is withdrawn")
        store.writable = false
        do { _ = try await provider.frame(timeout: 0.1); XCTFail() } catch { XCTAssertEqual(error as? ScreenError, .channelUnavailable) }
    }
}

final class ScreenActionTests: XCTestCase {
    private let frame = ScreenFrame(requestID: UUID(), capturedAt: Date(), width: 590, height: 1280, jpeg: Data([0xFF, 0xD8, 0xFF, 0xE0]))

    func testRequestCarriesImageContentPart() throws {
        let request = try ScreenActionClient().request(instruction: "  Reply to this ", text: " Thanks, ", frame: frame, accessToken: "token")
        XCTAssertEqual(request.url, SmartActionClient.url)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let body = try XCTUnwrap(JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any])
        XCTAssertEqual(body["mode"] as? String, "smart_auto")
        XCTAssertEqual(body["max_tokens"] as? Int, 2000)
        let messages = try XCTUnwrap(body["messages"] as? [[String: Any]])
        XCTAssertEqual(messages.count, 2)
        XCTAssertEqual(messages[0]["role"] as? String, "system")
        XCTAssertEqual(messages[0]["content"] as? String, ScreenPrompts.system)
        XCTAssertEqual(messages[1]["role"] as? String, "user")
        let parts = try XCTUnwrap(messages[1]["content"] as? [[String: Any]])
        XCTAssertEqual(parts.count, 2)
        XCTAssertEqual(parts[0]["type"] as? String, "text")
        XCTAssertEqual(parts[0]["text"] as? String, "Instruction: Reply to this\n\nText near the cursor in the field, for context only (nothing is selected; \"this\" in the instruction means the screen):\nThanks,")
        XCTAssertEqual(parts[1]["type"] as? String, "image_url")
        let image = try XCTUnwrap(parts[1]["image_url"] as? [String: String])
        XCTAssertEqual(image["url"], "data:image/jpeg;base64," + frame.jpeg.base64EncodedString())
        XCTAssertTrue(try XCTUnwrap(image["url"]).range(of: "^data:image/[a-zA-Z0-9.+-]+;base64,", options: .regularExpression) != nil, "only inline images pass the server's onlyDataImages filter")
    }

    func testEmptyFieldAndEmptyInstruction() throws {
        let messages = try ScreenPrompts.messages(instruction: "Summarize", text: " \n", frame: frame)
        guard case .parts(let parts) = messages[1].content, case .text(let text) = parts[0] else { return XCTFail() }
        XCTAssertEqual(text, "Instruction: Summarize\n\n(The field is empty - write the text from the instruction and the screen.)")
        XCTAssertThrowsError(try ScreenActionClient().request(instruction: " \n", text: "x", frame: frame, accessToken: "t")) { XCTAssertEqual($0 as? GusanError, .emptyInput) }
    }

    private struct StubTransport: HTTPTransport {
        let status: Int; let body: String
        var seen: ((URLRequest) -> Void)? = nil
        func data(for request: URLRequest) async throws -> (Data, URLResponse) {
            seen?(request)
            return (Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
    }
    private struct StubFrames: ScreenFrameProvider {
        let frame: ScreenFrame?
        func frame(timeout: TimeInterval) async throws -> ScreenFrame { guard let frame else { throw ScreenError.noFrame }; return frame }
    }

    func testJobReportsProgressThenResult() async throws {
        let client = ScreenActionClient(transport: StubTransport(status: 200, body: #"{"text":"Sounds good, see you at 5!"}"#))
        let job = ScreenInstructionJob(instruction: "Reply to this", text: "", accessToken: "t", kind: .insert, frames: StubFrames(frame: frame), client: client)
        var events: [JobEvent] = []
        for try await event in job.run() { events.append(event) }
        XCTAssertEqual(events, [.progress("Looking at your screen…"), .progress("Thinking…"), .result(JobResult(text: "Sounds good, see you at 5!", kind: .insert))])
        let selection = ScreenInstructionJob(instruction: "Translate this", text: "Hola", accessToken: "t", kind: .replaceSelection, frames: StubFrames(frame: frame), client: client)
        let selected = try await selection.result(); XCTAssertEqual(selected.kind, .replaceSelection)
    }

    func testJobErrors() async {
        let ok = ScreenActionClient(transport: StubTransport(status: 200, body: #"{"text":"x"}"#))
        do { _ = try await ScreenInstructionJob(instruction: "Reply", text: "", accessToken: "t", kind: .insert, frames: StubFrames(frame: nil), client: ok).result(); XCTFail() }
        catch { XCTAssertEqual(error as? ScreenError, .noFrame) }
        do { _ = try await ScreenInstructionJob(instruction: " ", text: "", accessToken: "t", kind: .insert, frames: StubFrames(frame: frame), client: ok).result(); XCTFail() }
        catch { XCTAssertEqual(error as? GusanError, .emptyInput) }
        for (status, expected) in [(401, GusanError.unauthorized), (403, .proRequired), (413, .tooLong), (429, .dailyLimit), (503, .serverNotConfigured), (500, .failed)] {
            let client = ScreenActionClient(transport: StubTransport(status: status, body: "{}"))
            do { _ = try await ScreenInstructionJob(instruction: "Reply", text: "", accessToken: "t", kind: .insert, frames: StubFrames(frame: frame), client: client).result(); XCTFail() }
            catch { XCTAssertEqual(error as? GusanError, expected, "status \(status)") }
        }
        let blank = ScreenActionClient(transport: StubTransport(status: 200, body: #"{"text":"  "}"#))
        do { _ = try await ScreenInstructionJob(instruction: "Reply", text: "", accessToken: "t", kind: .insert, frames: StubFrames(frame: frame), client: blank).result(); XCTFail() }
        catch { XCTAssertEqual(error as? GusanError, .invalidResponse) }
    }
}

final class ScreenSettingsTests: XCTestCase {
    func testDefaultsAndSharing() {
        let suite = "ScreenSettingsTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        let board = MemoryBackend()
        let settings = ScreenSettings(defaults: defaults, shared: board)
        XCTAssertEqual(settings.idleMinutes, 10)
        XCTAssertFalse(settings.explained)
        settings.idleMinutes = 20; settings.explained = true
        XCTAssertEqual(settings.idleMinutes, 20); XCTAssertTrue(settings.explained)
        settings.idleMinutes = 7
        XCTAssertEqual(settings.idleMinutes, 10, "unknown values fall back to the default")
        // A second process with its own defaults (broken App Group) sees the pasteboard copy.
        let other = UserDefaults(suiteName: suite + ".other")!
        defer { other.removePersistentDomain(forName: suite + ".other") }
        let peer = ScreenSettings(defaults: other, shared: board)
        XCTAssertTrue(peer.explained); XCTAssertEqual(peer.idleMinutes, 10)
        peer.idleMinutes = 5
        XCTAssertEqual(settings.idleMinutes, 5)
    }
}

final class ScreenFrameTimingTests: XCTestCase {
    private func image(_ width: Int, _ height: Int) -> CGImage {
        let context = CGContext(data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue)!
        context.setFillColor(CGColor(red: 0.2, green: 0.4, blue: 0.8, alpha: 1)); context.fill(CGRect(x: 0, y: 0, width: width, height: height))
        return context.makeImage()!
    }

    func testFastThenIdleRate() {
        let store = ScreenFrameStore(fastInterval: 0.4, idleInterval: 1.0, capacity: 2)
        let start = Date()
        XCTAssertEqual(store.minimumInterval(at: start), 1.0, "idle until something asks for frames")
        store.boost(until: start.addingTimeInterval(10))
        XCTAssertEqual(store.minimumInterval(at: start), 0.4)
        store.store(image(2, 2), at: start)
        XCTAssertTrue(store.shouldAccept(at: start.addingTimeInterval(0.5)))
        store.store(image(2, 2), at: start.addingTimeInterval(0.5))
        XCTAssertEqual(store.minimumInterval(at: start.addingTimeInterval(11)), 1.0)
        store.store(image(2, 2), at: start.addingTimeInterval(11))
        XCTAssertFalse(store.shouldAccept(at: start.addingTimeInterval(11.5)), "only 0.5 s after the last frame once idle")
        XCTAssertTrue(store.shouldAccept(at: start.addingTimeInterval(12)))
        XCTAssertEqual(store.accepted, 3)
        XCTAssertNil(store.snapshot(before: start.addingTimeInterval(-1)), "capacity 2: the first frame is gone")
        XCTAssertEqual(store.snapshot(before: start.addingTimeInterval(1))?.capturedAt, start.addingTimeInterval(0.5))
    }

    func testRequestsUseFreshDistinctPicturesAndCropTheKeyboard() throws {
        let store = ScreenFrameStore(fastInterval: 0.4, idleInterval: 1.0, capacity: 4)
        let start = Date()
        store.store(image(10, 20), at: start)
        store.store(image(12, 24), at: start.addingTimeInterval(1))
        let shownAt = start.addingTimeInterval(1.3)
        store.store(image(14, 28), at: start.addingTimeInterval(2), "this one shows the keyboard")
        let keyboard = ScreenKeyboardState(shownAt: shownAt, keyboardHeight: 300, screenHeight: 800)
        let request = ScreenRequest(issuedAt: start.addingTimeInterval(1.5), phase: .start)
        let frame = try XCTUnwrap(store.frame(for: request, keyboard: keyboard))
        XCTAssertFalse(frame.frozen); XCTAssertTrue(frame.cropped)
        XCTAssertEqual(frame.requestID, request.id); XCTAssertEqual(frame.phase, .start)
        XCTAssertEqual(frame.width, 14); XCTAssertEqual(frame.capturedAt, start.addingTimeInterval(2))
        let finishRequest = ScreenRequest(issuedAt: start.addingTimeInterval(2.5), phase: .finish)
        XCTAssertNil(store.frame(for: finishRequest, keyboard: keyboard))
        store.store(image(16, 32), at: start.addingTimeInterval(3))
        let finish = try XCTUnwrap(store.frame(for: finishRequest, keyboard: keyboard))
        XCTAssertEqual(finish.phase, .finish); XCTAssertEqual(finish.requestID, finishRequest.id)
        XCTAssertGreaterThanOrEqual(finish.capturedAt, finishRequest.issuedAt)
        XCTAssertNotEqual(finish.jpeg, frame.jpeg)
        store.store(image(16, 32), at: start.addingTimeInterval(4))
        store.store(image(16, 32), at: start.addingTimeInterval(5))
        store.store(image(16, 32), at: start.addingTimeInterval(6))
        XCTAssertEqual(store.frame(for: UUID(), keyboard: keyboard)?.capturedAt, start.addingTimeInterval(6))
        XCTAssertEqual(store.frame(for: UUID())?.width, 16, "without a keyboard the latest picture is used")
        let unanswered = ScreenRequest(issuedAt: start.addingTimeInterval(7), phase: .finish)
        XCTAssertNil(store.frame(for: unanswered, keyboard: keyboard))
        XCTAssertEqual(store.frame(for: unanswered, keyboard: keyboard, allowFallback: true)?.capturedAt, start.addingTimeInterval(6))
        let cropped = try XCTUnwrap(store.frame(for: UUID(), keyboard: keyboard))
        XCTAssertFalse(cropped.frozen); XCTAssertTrue(cropped.cropped)
        XCTAssertEqual(cropped.width, 16); XCTAssertEqual(cropped.height, 20, "32 px minus the keyboard's 300/800")
        XCTAssertEqual(ScreenKeyboardState(shownAt: shownAt, keyboardHeight: 790, screenHeight: 800).bottomFraction, ScreenFrameGeometry.maxBottomCrop, "never crops more than 70 %")
        XCTAssertEqual(ScreenFrameGeometry.cropRect(width: 10, height: 100, bottomFraction: 0.25), CGRect(x: 0, y: 0, width: 10, height: 75))
        store.clear()
        XCTAssertNil(store.frame(for: UUID(), keyboard: keyboard), "right after a pause there is nothing to answer with; the extension waits for the next frame")
    }

    func testFreshness() {
        let now = Date()
        let shownAt = now.addingTimeInterval(-120)
        let frozen = ScreenFrame(requestID: UUID(), capturedAt: shownAt.addingTimeInterval(-1), width: 1, height: 1, jpeg: Data([1]), frozen: true)
        XCTAssertTrue(frozen.isFresh(now: now, keyboardShownAt: shownAt), "a frozen picture is judged against the keyboard's appearance, not now")
        XCTAssertFalse(frozen.isFresh(now: now, keyboardShownAt: nil), "without a known keyboard time it is just two minutes old")
        XCTAssertFalse(ScreenFrame(requestID: UUID(), capturedAt: shownAt.addingTimeInterval(-6), width: 1, height: 1, jpeg: Data([1]), frozen: true).isFresh(now: now, keyboardShownAt: shownAt))
        let live = ScreenFrame(requestID: UUID(), capturedAt: now.addingTimeInterval(-2), width: 1, height: 1, jpeg: Data([1]))
        XCTAssertTrue(live.isFresh(now: now, keyboardShownAt: shownAt))
        XCTAssertFalse(ScreenFrame(requestID: UUID(), capturedAt: now.addingTimeInterval(-6), width: 1, height: 1, jpeg: Data([1])).isFresh(now: now))
        XCTAssertFalse(ScreenFrame(requestID: UUID(), capturedAt: now.addingTimeInterval(120), width: 1, height: 1, jpeg: Data([1])).isFresh(now: now), "a picture from the future is distrusted")
    }

    func testProviderRejectsStalePicture() async {
        let store = MemoryBackend()
        let channel = ScreenChannel(stores: [store])
        channel.write(status: ScreenStatus(startedAt: Date(), idleDeadline: Date().addingTimeInterval(60), width: 1, height: 1))
        let provider = ScreenChannelFrameProvider(channel: channel, pollInterval: 0.02, keyboardShownAt: Date())
        let extensionSide = Task.detached {
            while channel.readRequest() == nil { try await Task.sleep(nanoseconds: 5_000_000) }
            let request = try XCTUnwrap(channel.consumeRequest())
            channel.write(frame: ScreenFrame(requestID: request.id, capturedAt: Date().addingTimeInterval(-30), width: 4, height: 8, jpeg: Data([7])))
        }
        do { _ = try await provider.frame(timeout: 2); XCTFail("a 30 s old picture is not what the user is looking at") }
        catch { XCTAssertEqual(error as? ScreenError, .noFrame) }
        try? await extensionSide.value
        XCTAssertNil(store.values["frame"], "the stale picture is removed")
        XCTAssertEqual(ScreenChannel.lifetimes["frame"], 10)
    }

    func testKeyboardPacket() {
        let store = MemoryBackend()
        var clock = Date()
        let channel = ScreenChannel(stores: [store], now: { clock })
        XCTAssertTrue(channel.keyboardNeedsRefresh())
        XCTAssertNil(channel.readKeyboard())
        let state = ScreenKeyboardState(shownAt: clock, keyboardHeight: 336, screenHeight: 852)
        XCTAssertTrue(channel.write(keyboard: state))
        XCTAssertEqual(channel.readKeyboard(), state)
        XCTAssertFalse(channel.keyboardNeedsRefresh())
        clock = clock.addingTimeInterval(ScreenChannel.keyboardRefresh)
        XCTAssertTrue(channel.keyboardNeedsRefresh(), "the keyboard rewrites it long before it expires")
        XCTAssertEqual(channel.readKeyboard(), state)
        clock = clock.addingTimeInterval(100)
        XCTAssertNil(channel.readKeyboard(), "a keyboard that died without saying goodbye stops freezing the picture")
        channel.write(keyboard: state); channel.clearKeyboard()
        XCTAssertNil(store.values["keyboard"])
        XCTAssertEqual(channel.name(.keyboard), "gusan.screen.keyboard.com.aram2k.gusan")
        XCTAssertEqual(ScreenStatus.heartbeatInterval * 3, ScreenStatus.liveWindow - 1)
    }
}

private extension ScreenFrameStore {
    func store(_ image: CGImage, at now: Date, _ note: String) { store(image, at: now) }
}

final class ScreenPromptSelectionTests: XCTestCase {
    private let frame = ScreenFrame(requestID: UUID(), capturedAt: Date(), width: 1, height: 1, jpeg: Data([1]))
    private func userText(selected: String, text: String) throws -> String {
        let messages = try ScreenPrompts.messages(instruction: "Translate this", text: text, selected: selected, frame: frame)
        guard case .parts(let parts) = messages[1].content, case .text(let body) = parts[0] else { XCTFail(); return "" }
        return body
    }
    func testTellsTheModelWhatThisMeans() throws {
        XCTAssertTrue(try userText(selected: "Bonjour", text: "Hi. Bonjour. Bye").contains("Selected text in the field"))
        XCTAssertTrue(try userText(selected: "Bonjour", text: "Hi. Bonjour. Bye").contains("\nBonjour"))
        XCTAssertFalse(try userText(selected: "Bonjour", text: "Hi. Bonjour. Bye").contains("Bye"), "only the selection is sent when there is one")
        XCTAssertTrue(try userText(selected: "", text: "Hi there").contains("nothing is selected; \"this\" in the instruction means the screen"))
        XCTAssertTrue(try userText(selected: "", text: "").contains("The field is empty"))
    }
}

final class ScreenConsentTests: XCTestCase {
    func testConsentExpiresAndFollowsTheVersion() {
        let suite = "ScreenConsentTests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: suite)!
        defer { defaults.removePersistentDomain(forName: suite) }
        var clock = Date()
        let board = MemoryBackend()
        let settings = ScreenSettings(defaults: defaults, shared: board, version: "1.2", now: { clock })
        XCTAssertFalse(settings.explained); XCTAssertNil(settings.explainedAt)
        settings.explained = true
        XCTAssertTrue(settings.explained); XCTAssertEqual(settings.explainedAt!.timeIntervalSince(clock), 0, accuracy: 0.001)
        clock = clock.addingTimeInterval(ScreenSettings.consentValidity - 1)
        XCTAssertTrue(settings.explained)
        clock = clock.addingTimeInterval(2)
        XCTAssertFalse(settings.explained, "re-shown after 30 days")
        XCTAssertNotNil(settings.explainedAt, "the page can say it is a repeat")
        settings.explained = true
        XCTAssertTrue(settings.explained)
        let updated = ScreenSettings(defaults: defaults, shared: board, version: "1.3", now: { clock })
        XCTAssertFalse(updated.explained, "re-shown after an app update")
        XCTAssertFalse(settings.armReminderShown)
        settings.armReminderShown = true
        XCTAssertTrue(ScreenSettings(defaults: defaults, shared: board, version: "1.2", now: { clock }).armReminderShown)
        XCTAssertNil(settings.lastIdleStop)
        settings.recordIdleStop(minutes: 10)
        XCTAssertEqual(settings.lastIdleStop?.minutes, 10); XCTAssertEqual(settings.lastIdleStop!.at.timeIntervalSince(clock), 0, accuracy: 0.001)
        // The extension's record is visible to the app through the shared copy.
        let other = UserDefaults(suiteName: suite + ".other")!
        defer { other.removePersistentDomain(forName: suite + ".other") }
        XCTAssertEqual(ScreenSettings(defaults: other, shared: board, version: "1.2", now: { clock }).lastIdleStop?.minutes, 10)
        settings.clearIdleStop()
        XCTAssertNil(settings.lastIdleStop)
        XCTAssertNil(ScreenSettings(defaults: other, shared: board, version: "1.2", now: { clock }).lastIdleStop)
    }
}
