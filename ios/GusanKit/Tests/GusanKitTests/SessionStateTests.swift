import XCTest
@testable import GusanKit

final class SessionStateTests: XCTestCase {
    func testReadyForActionsAndVoice() {
        let state = SessionState(signedIn: true, fullAccess: true, shareable: true)
        XCTAssertTrue(state.canRunActions)
        XCTAssertTrue(state.voiceReady)
    }
    func testMissingSignInAndFullAccessHaveOneReason() {
        XCTAssertEqual(SessionState(signedIn: false, fullAccess: true, shareable: true).actionError, .notSignedIn)
        let denied = SessionState(signedIn: false, fullAccess: false, shareable: false)
        XCTAssertEqual(denied.actionError, .fullAccessRequired)
        XCTAssertEqual(denied.voiceError, .fullAccessRequired)
    }
    func testStorageAndVoiceFailureDoNotPretendToBeSignedOut() {
        let storage = SessionState(signedIn: true, fullAccess: true, shareable: false)
        XCTAssertTrue(storage.signedIn)
        XCTAssertEqual(storage.actionError, .notShared)
        let voice = SessionState(signedIn: true, fullAccess: true, shareable: true, voiceError: .fallbackOff)
        XCTAssertTrue(voice.canRunActions)
        XCTAssertFalse(voice.voiceReady)
        XCTAssertEqual(voice.voiceError, .fallbackOff)
        XCTAssertEqual(KeyboardVoice.tap(status: nil, access: voice), .error(.fallbackOff))
    }
}
