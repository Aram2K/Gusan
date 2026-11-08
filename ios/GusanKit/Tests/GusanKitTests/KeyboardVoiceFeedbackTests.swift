import XCTest
@testable import GusanKit

final class KeyboardVoiceFeedbackTests: XCTestCase {
    func testTimedOutRequestCannotRestoreProcessingOrDeliverResults() {
        let id = UUID()
        XCTAssertNil(KeyboardVoiceFeedback.statusForDisplay(nil, timedOutRequestID: id))
        for phase in [VoicePhase.recording, .transcribing, .thinking, .done, .failed] {
            let late = VoiceStatus(phase: phase, requestID: id, message: "Working…", result: "late result")
            XCTAssertNil(KeyboardVoiceFeedback.statusForDisplay(late, timedOutRequestID: id))
            XCTAssertEqual(KeyboardVoiceFeedback.statusForDisplay(late, timedOutRequestID: nil), late)
        }
        let retry = VoiceStatus(phase: .recording, requestID: UUID())
        XCTAssertEqual(KeyboardVoiceFeedback.statusForDisplay(retry, timedOutRequestID: id), retry)
        let ready = VoiceStatus(phase: .ready)
        XCTAssertEqual(KeyboardVoiceFeedback.statusForDisplay(ready, timedOutRequestID: id), ready)
    }

    func testFailureFreshnessAndRequestIdentity() {
        let now = Date(), id = UUID()
        let pending = KeyboardVoiceState(requestID: id, startedAt: now.addingTimeInterval(-300))
        var status = VoiceStatus(phase: .failed, requestID: id, heartbeat: .distantPast, message: "Denied")
        status.writtenAt = now
        XCTAssertTrue(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
        status.writtenAt = now.addingTimeInterval(-200)
        XCTAssertTrue(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(status, pending: KeyboardVoiceState(), now: now))
        status.requestID = UUID()
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
        status.requestID = id; status.writtenAt = now.addingTimeInterval(-400)
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
        status.writtenAt = now.addingTimeInterval(1)
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
    }

    func testMissingAndNonFailureAndLegacyTimestamp() {
        let now = Date(), pending = KeyboardVoiceState()
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(nil, pending: pending, now: now))
        var status = VoiceStatus(phase: .recording)
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
        status.phase = .failed; status.writtenAt = nil; status.heartbeat = now
        XCTAssertTrue(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
        status.heartbeat = .distantPast
        XCTAssertFalse(KeyboardVoiceFeedback.displaysFailure(status, pending: pending, now: now))
    }
}
