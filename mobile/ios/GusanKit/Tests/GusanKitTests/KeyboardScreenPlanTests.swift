import XCTest
@testable import GusanKit

final class KeyboardScreenPlanTests: XCTestCase {
    private func frame(_ phase: ScreenRequest.Phase) -> ScreenFrame {
        ScreenFrame(requestID: UUID(), capturedAt: Date(), width: 1, height: 1, jpeg: Data([phase == .start ? 1 : 2]), phase: phase)
    }
    private func labels(_ frames: [ScreenFrame]) throws -> [String] {
        let messages = try ScreenPrompts.messages(instruction: "Describe the change", text: "", frames: frames)
        guard case .parts(let parts) = messages[1].content else { XCTFail(); return [] }
        return parts.dropFirst().compactMap { if case .text(let text) = $0 { return text }; return nil }
    }
    func testTwoFramesKeepTheirTemporalLabels() throws {
        let start = frame(.start), finish = frame(.finish)
        let plan = KeyboardScreenPlan(enabled: true, live: true, secure: false, fullAccess: true, start: start, finish: finish)
        XCTAssertEqual(plan.frames, [start, finish]); XCTAssertNil(plan.message)
        XCTAssertEqual(try labels(plan.frames), ["Screen at start of voice instruction", "Screen at finish"])
        var unchanged = start
        unchanged.phase = .finish
        XCTAssertEqual(try labels([start, unchanged]).count, 2, "Two capture moments remain labeled even when pixels did not change")
    }
    func testEitherMissingFrameHasAnUnambiguousLabelAndFeedback() throws {
        for phase in [ScreenRequest.Phase.start, .finish] {
            let picture = frame(phase)
            let plan = KeyboardScreenPlan(enabled: true, live: true, secure: false, fullAccess: true, start: phase == .start ? picture : nil, finish: phase == .finish ? picture : nil)
            XCTAssertEqual(plan.message, "Screen: only 1 frame")
            XCTAssertEqual(plan.frames.count, 1)
            XCTAssertEqual(try labels(plan.frames), [(phase == .start ? "Screen at start of voice instruction" : "Screen at finish") + " (only frame available)"])
        }
        let missing = KeyboardScreenPlan(enabled: true, live: true, secure: false, fullAccess: true, start: nil, finish: nil)
        XCTAssertTrue(missing.frames.isEmpty)
        XCTAssertEqual(missing.message, "Screen unavailable — sent without it")
        XCTAssertThrowsError(try labels(missing.frames)) { XCTAssertEqual($0 as? ScreenError, .noFrame) }
    }
    func testBroadcastLossChangesIconAndDropsFramesWithFeedback() {
        let now = Date()
        let fresh = ScreenStatus(startedAt: now, heartbeat: now, idleDeadline: now.addingTimeInterval(600))
        let stale = ScreenStatus(startedAt: now, heartbeat: now.addingTimeInterval(-11), idleDeadline: now.addingTimeInterval(600))
        XCTAssertEqual(KeyboardScreenIconState.resolve(enabled: true, status: fresh, now: now), .live)
        for status in [stale, nil] {
            let state = KeyboardScreenIconState.resolve(enabled: true, status: status, now: now)
            XCTAssertEqual(state, .needsBroadcast)
            XCTAssertEqual(state.accessibilityValue, "Needs screen broadcast")
            let plan = KeyboardScreenPlan(enabled: true, live: state == .live, secure: false, fullAccess: true, start: frame(.start), finish: frame(.finish))
            XCTAssertTrue(plan.frames.isEmpty)
            XCTAssertEqual(plan.message, "Screen unavailable — sent without it")
        }
    }
    func testOffSecureAndNoFullAccessNeverRequestOrSendFrames() {
        XCTAssertTrue(KeyboardScreenPlan.shouldRequest(enabled: true, live: true, secure: false, fullAccess: true))
        for (enabled, live, secure, access) in [(false, true, false, true), (true, true, true, true), (true, true, false, false), (true, false, false, true)] {
            XCTAssertFalse(KeyboardScreenPlan.shouldRequest(enabled: enabled, live: live, secure: secure, fullAccess: access))
            let plan = KeyboardScreenPlan(enabled: enabled, live: live, secure: secure, fullAccess: access, start: frame(.start), finish: frame(.finish))
            XCTAssertTrue(plan.frames.isEmpty)
        }
        XCTAssertEqual(KeyboardScreenIconState.resolve(enabled: false, live: true), .off)
        XCTAssertEqual(KeyboardScreenIconState.off.accessibilityValue, "Off")
    }
    func testScreenEnabledWithoutLiveBroadcastKeepsNormalVoicePath() {
        let screenTake = KeyboardScreenPlan.shouldRequest(enabled: true, live: false, secure: false, fullAccess: true)
        XCTAssertFalse(screenTake)
        let action: VoiceAction? = screenTake ? .transcribeOnly : nil
        XCTAssertNil(action)
    }
    func testRequestPhaseRoundTripAndResponseCorrelation() throws {
        let store = MemoryBackend(), channel = ScreenChannel(stores: [])
        XCTAssertNil(channel.readFrame())
        let transport = ScreenChannel(stores: [store])
        for phase in [ScreenRequest.Phase.start, .finish] {
            let request = ScreenRequest(phase: phase)
            XCTAssertTrue(transport.write(request: request))
            XCTAssertEqual(transport.readRequest(), request)
            var response = frame(phase)
            response = ScreenFrame(requestID: request.id, capturedAt: response.capturedAt, width: response.width, height: response.height, jpeg: response.jpeg, phase: phase)
            XCTAssertTrue(transport.write(frame: response))
            XCTAssertNil(transport.consumeFrame(requestID: UUID()))
            XCTAssertEqual(transport.consumeFrame(requestID: request.id), response)
        }
        let legacy = try JSONDecoder().decode(ScreenRequest.self, from: Data(#"{"id":"00000000-0000-0000-0000-000000000001","issuedAt":100}"#.utf8))
        XCTAssertNil(legacy.phase)
    }
    func testProviderPermissionGuardDoesNotWriteARequest() async {
        let store = MemoryBackend()
        let provider = ScreenChannelFrameProvider(channel: ScreenChannel(stores: [store]), accessAllowed: { false })
        do { _ = try await provider.frame(timeout: 0.01); XCTFail() }
        catch { XCTAssertEqual(error as? ScreenError, .channelUnavailable) }
        XCTAssertEqual(store.writes, 0)
        XCTAssertNil(store.values["request"])
    }
}
