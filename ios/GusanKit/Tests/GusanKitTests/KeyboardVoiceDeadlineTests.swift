import XCTest
@testable import GusanKit

final class KeyboardVoiceDeadlineTests: XCTestCase {
    func testUnansweredAndWrongRequest() {
        let start = Date(), pending = KeyboardVoiceState(requestID: UUID(), startedAt: Date(timeIntervalSince1970: 100))
        var deadline = KeyboardVoiceDeadline()
        XCTAssertNil(deadline.expiry(pending: KeyboardVoiceState(), status: nil, now: start))
        XCTAssertNil(deadline.expiry(pending: pending, status: nil, now: pending.startedAt.addingTimeInterval(9)))
        XCTAssertEqual(deadline.expiry(pending: pending, status: VoiceStatus(phase: .recording, requestID: UUID()), now: pending.startedAt.addingTimeInterval(10)), .acknowledgement)
    }

    func testPermissionAcknowledgementAndProcessingDeadline() {
        let start = Date(), id = UUID()
        let pending = KeyboardVoiceState(requestID: id, startedAt: start)
        var deadline = KeyboardVoiceDeadline()
        XCTAssertNil(deadline.expiry(pending: pending, status: VoiceStatus(phase: .ready, requestID: id), now: start))
        XCTAssertNil(deadline.expiry(pending: pending, status: nil, now: start.addingTimeInterval(300)))
        var processing = VoiceStatus(phase: .transcribing, requestID: id)
        processing.writtenAt = start.addingTimeInterval(300)
        XCTAssertNil(deadline.expiry(pending: pending, status: processing, now: start.addingTimeInterval(300)))
        XCTAssertNil(deadline.expiry(pending: pending, status: VoiceStatus(phase: .thinking, requestID: id), now: start.addingTimeInterval(419)))
        XCTAssertEqual(deadline.expiry(pending: pending, status: nil, now: start.addingTimeInterval(420)), .processing)
        for phase in [VoicePhase.done, .failed] {
            XCTAssertNil(deadline.expiry(pending: pending, status: VoiceStatus(phase: phase, requestID: id), now: start.addingTimeInterval(500)))
        }
    }
}
