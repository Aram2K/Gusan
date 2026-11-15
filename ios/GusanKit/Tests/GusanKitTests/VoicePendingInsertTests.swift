import XCTest
@testable import GusanKit

final class VoicePendingInsertTests: XCTestCase {
    func testRoundTripExpiryAndClear() throws {
        let name = "Gusan.pending." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let now = Date()
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one", now: now))
        let value = VoicePendingInsert(text: "Ready to insert", at: now, account: "user-one")
        value.write(to: defaults)
        XCTAssertEqual(VoicePendingInsert.read(from: defaults, account: "user-one", now: now.addingTimeInterval(599)), value)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one", now: now.addingTimeInterval(600)))
        XCTAssertNil(defaults.object(forKey: VoicePendingInsert.key))
        value.write(to: defaults)
        VoicePendingInsert.clear(in: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one", now: now))
    }
    func testAccountSwitchSignOutAndLegacyResultsAreCleared() {
        let name = "Gusan.pending.account." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let value = VoicePendingInsert(text: "Private", account: "user-one")
        value.write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-two"))
        XCTAssertNil(defaults.object(forKey: VoicePendingInsert.key))
        value.write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: nil))
        VoicePendingInsert(text: "Legacy").write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one"))
    }
    func testAppJSONAndMalformedOrEmptyResults() {
        let name = "Gusan.pending." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let now = Date()
        defaults.set(["text": "App result", "at": now.timeIntervalSince1970, "account": "user-one"], forKey: VoicePendingInsert.key)
        XCTAssertEqual(VoicePendingInsert.read(from: defaults, account: "user-one", now: now)?.text, "App result")
        defaults.set("invalid JSON", forKey: VoicePendingInsert.key)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one", now: now))
        VoicePendingInsert(text: " ", at: now, account: "user-one").write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one", now: now))
        VoicePendingInsert(text: "Future", at: now.addingTimeInterval(1), account: "user-one").write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, account: "user-one", now: now))
    }
}
