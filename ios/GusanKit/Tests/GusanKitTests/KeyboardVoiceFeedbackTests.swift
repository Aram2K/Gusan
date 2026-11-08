import XCTest
@testable import GusanKit

final class KeyboardVoiceFeedbackTests: XCTestCase {
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
