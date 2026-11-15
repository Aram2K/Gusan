import XCTest
@testable import GusanKit

final class KeyboardVoiceRoutingTests: XCTestCase {
    func testLegacyStateWithoutActionDecodes() throws {
        let old = Data(#"{"startedAt":100,"context":{"selected":"","before":"","after":""}}"#.utf8)
        let state = try JSONDecoder().decode(KeyboardVoiceState.self, from: old)
        XCTAssertNil(state.action)
        XCTAssertNil(state.requestID)
        XCTAssertEqual(state.startedAt, Date(timeIntervalSinceReferenceDate: 100))
    }
    func testActionAndRequestRoundTrip() throws {
        for action in [VoiceAction.transcribeOnly, .automatic, .smart(.smartAuto)] {
            let state = KeyboardVoiceState(requestID: UUID(), context: VoiceContext(selected: "selection"), action: action)
            let restored = try JSONDecoder().decode(KeyboardVoiceState.self, from: JSONEncoder().encode(state))
            XCTAssertEqual(restored, state)
            XCTAssertEqual(restored.action, action)
        }
    }
    func testInvalidActionIsRejected() {
        XCTAssertThrowsError(try JSONDecoder().decode(KeyboardVoiceState.self, from: Data(#"{"startedAt":100,"action":"invalid"}"#.utf8)))
    }
}
