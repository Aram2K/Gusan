import XCTest
@testable import GusanKit

final class KeyboardVoiceStartupTests: XCTestCase {
    func testValidateBeforePermissionAndPreserveIdentityAfterLongWait() {
        let now = Date(), command = VoiceCommand(kind: .start, requestID: UUID(), context: VoiceContext(selected: "text"), sentAt: Date(timeIntervalSince1970: 100), action: .transcribeOnly)
        let accepted = KeyboardVoiceStartup.validatedStart(command, within: 60, now: command.sentAt.addingTimeInterval(59))
        XCTAssertEqual(accepted, command)
        let resumed = KeyboardVoiceStartup.afterPermission(accepted!, now: now)
        XCTAssertEqual(resumed.id, command.id)
        XCTAssertEqual(resumed.requestID, command.requestID)
        XCTAssertEqual(resumed.context, command.context)
        XCTAssertEqual(resumed.action, .transcribeOnly)
        XCTAssertEqual(resumed.sentAt, now)
        var machine = VoiceMachine()
        _ = machine.handle(.sessionStarted(now))
        XCTAssertTrue(machine.handle(.command(resumed, now)).contains(.beginCapture))
    }
    func testRejectMissingExpiredFutureAndNonStart() {
        let now = Date()
        XCTAssertNil(KeyboardVoiceStartup.validatedStart(nil, within: 60, now: now))
        for (kind, age) in [(VoiceCommand.Kind.start, 61.0), (.start, -1), (.cancel, 0), (.stop, 0)] {
            XCTAssertNil(KeyboardVoiceStartup.validatedStart(VoiceCommand(kind: kind, requestID: UUID(), sentAt: now.addingTimeInterval(-age)), within: 60, now: now))
        }
    }
}
