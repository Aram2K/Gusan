import XCTest
@testable import GusanKit

final class VoicePendingInsertTests: XCTestCase {
    func testRoundTripExpiryAndClear() throws {
        let name = "Gusan.pending." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let now = Date()
        XCTAssertNil(VoicePendingInsert.read(from: defaults, now: now))
        let value = VoicePendingInsert(text: "Ready to insert", at: now)
        value.write(to: defaults)
        XCTAssertEqual(VoicePendingInsert.read(from: defaults, now: now.addingTimeInterval(599)), value)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, now: now.addingTimeInterval(600)))
        XCTAssertNil(defaults.object(forKey: VoicePendingInsert.key))
        value.write(to: defaults)
        VoicePendingInsert.clear(in: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, now: now))
    }
    func testAppJSONAndMalformedOrEmptyResults() {
        let name = "Gusan.pending." + UUID().uuidString
        let defaults = UserDefaults(suiteName: name)!
        defer { defaults.removePersistentDomain(forName: name) }
        let now = Date()
        defaults.set(["text": "App result", "at": now.timeIntervalSince1970], forKey: VoicePendingInsert.key)
        XCTAssertEqual(VoicePendingInsert.read(from: defaults, now: now)?.text, "App result")
        defaults.set("invalid JSON", forKey: VoicePendingInsert.key)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, now: now))
        VoicePendingInsert(text: " ", at: now).write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, now: now))
        VoicePendingInsert(text: "Future", at: now.addingTimeInterval(1)).write(to: defaults)
        XCTAssertNil(VoicePendingInsert.read(from: defaults, now: now))
    }
}
