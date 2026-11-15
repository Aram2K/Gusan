import XCTest
@testable import GusanKit

final class ScreenCaptureCadenceTests: XCTestCase {
    func testKeyboardHeartbeatsDoNotKeepTheFastCaptureRateAlive() {
        let start = Date()
        let keyboard = ScreenKeyboardState(shownAt: start, keyboardHeight: 260, screenHeight: 800)
        var cadence = ScreenCaptureCadence()
        let frames = ScreenFrameStore(fastInterval: 0.1, idleInterval: 1)
        XCTAssertFalse(cadence.shouldBoost(keyboard: nil))
        XCTAssertTrue(cadence.shouldBoost(keyboard: keyboard))
        frames.boost(until: start.addingTimeInterval(10))
        for second in [1.0, 5, 9, 11, 30] {
            if cadence.shouldBoost(keyboard: keyboard) {
                XCTFail("A heartbeat must not extend the boost")
                frames.boost(until: start.addingTimeInterval(second + 10))
            }
        }
        XCTAssertEqual(frames.minimumInterval(at: start.addingTimeInterval(9)), 0.1)
        XCTAssertEqual(frames.minimumInterval(at: start.addingTimeInterval(11)), 1)
        XCTAssertFalse(cadence.shouldBoost(keyboard: ScreenKeyboardState(shownAt: start, keyboardHeight: 300, screenHeight: 800)))
        XCTAssertFalse(cadence.shouldBoost(keyboard: nil))
        XCTAssertTrue(cadence.shouldBoost(keyboard: ScreenKeyboardState(shownAt: start.addingTimeInterval(40), keyboardHeight: 260, screenHeight: 800)))
        frames.boost(until: start.addingTimeInterval(50))
        XCTAssertEqual(frames.minimumInterval(at: start.addingTimeInterval(41)), 0.1)
        XCTAssertEqual(frames.minimumInterval(at: start.addingTimeInterval(51)), 1)
    }
}
