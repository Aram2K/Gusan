import XCTest
@testable import GusanKit

final class KeyboardSettingsSnapshotTests: XCTestCase {
    private func withDefaults(_ body: (UserDefaults) -> Void) {
        let name = "KeyboardSettingsSnapshotTests.\(UUID().uuidString)"
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        body(defaults)
    }

    func testEmptyDefaults() {
        withDefaults { defaults in
            let snapshot = KeyboardSettingsSnapshot(defaults: defaults)
            XCTAssertEqual(snapshot, KeyboardSettingsSnapshot())
            XCTAssertEqual(snapshot.keyboardMode, .smart)
            XCTAssertTrue(snapshot.keyboardScreenEnabled)
            XCTAssertTrue(snapshot.keyClicks)
            XCTAssertTrue(snapshot.doubleSpacePeriod)
            XCTAssertTrue(snapshot.autoCapitalization)
            XCTAssertTrue(snapshot.pasteboardFallback)
        }
    }

    func testSnapshotDoesNotReadChangesUntilRecreated() {
        withDefaults { defaults in
            defaults.set("agent", forKey: "keyboardMode")
            for key in ["keyboardScreenEnabled", "keyClicks", "doubleSpacePeriod", "autoCapitalization", "pasteboardFallback"] {
                defaults.set(false, forKey: key)
            }
            let snapshot = KeyboardSettingsSnapshot(defaults: defaults)
            XCTAssertEqual(snapshot.keyboardMode, .agent)
            XCTAssertFalse(snapshot.keyboardScreenEnabled)
            XCTAssertFalse(snapshot.keyClicks)
            XCTAssertFalse(snapshot.doubleSpacePeriod)
            XCTAssertFalse(snapshot.autoCapitalization)
            XCTAssertFalse(snapshot.pasteboardFallback)
            defaults.set("smart", forKey: "keyboardMode")
            defaults.set(true, forKey: "keyClicks")
            XCTAssertEqual(snapshot.keyboardMode, .agent)
            XCTAssertFalse(snapshot.keyClicks)
            XCTAssertEqual(KeyboardSettingsSnapshot(defaults: defaults).keyboardMode, .smart)
            XCTAssertTrue(KeyboardSettingsSnapshot(defaults: defaults).keyClicks)
        }
    }

    func testLegacyModeAndInvalidValues() {
        withDefaults { defaults in
            defaults.set("agent", forKey: "stripMode")
            XCTAssertEqual(KeyboardSettingsSnapshot(defaults: defaults).keyboardMode, .agent)
            defaults.set("unknown", forKey: "keyboardMode")
            defaults.set(["invalid"], forKey: "keyClicks")
            XCTAssertEqual(KeyboardSettingsSnapshot(defaults: defaults).keyboardMode, .smart)
            XCTAssertTrue(KeyboardSettingsSnapshot(defaults: defaults).keyClicks)
        }
    }
}
