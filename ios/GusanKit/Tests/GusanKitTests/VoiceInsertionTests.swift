import XCTest
@testable import GusanKit

final class VoiceInsertionTests: XCTestCase {
    func testSelectionCanOnlyBeReplacedWhenSnapshotStillMatches() {
        let snapshot = VoiceContext(selected: "original", before: "a", after: "b", documentID: "one")
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: snapshot), .planned)
        var current = snapshot
        current.selected = "other"
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: current), .clipboard)
        current.selected = ""
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: current), .cursor)
        current.documentID = "two"
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: current), .clipboard)
    }
    func testUnknownDocumentAllowsOnlyExactContextMatch() {
        for documentID: String? in [nil, ""] {
            let snapshot = VoiceContext(before: "a", after: "b", documentID: documentID)
            XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: snapshot), .planned)
            XCTAssertTrue(KeyboardVoiceState(context: snapshot).canInsert(context: snapshot))
            var changed = snapshot
            changed.before = "changed"
            XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: changed), .clipboard)
            XCTAssertFalse(KeyboardVoiceState(context: snapshot).canInsert(context: changed))
        }
    }

    func testNilIdentifierRequiresExactContextAndNeverFallsBackToCursor() {
        let unknown = VoiceContext(selected: "original", before: "a", after: "b")
        let known = VoiceContext(selected: "original", before: "a", after: "b", documentID: "one")
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: unknown, current: unknown), .planned)
        XCTAssertTrue(KeyboardVoiceState(context: unknown).canInsert(context: unknown))
        var emptyID = unknown
        emptyID.documentID = ""
        var changedSelection = unknown
        changedSelection.selected = "other"
        var changedAfter = unknown
        changedAfter.after = "changed"
        for (snapshot, current) in [(known, unknown), (unknown, known), (unknown, VoiceContext()), (unknown, changedSelection), (unknown, changedAfter), (unknown, emptyID)] {
            XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: snapshot, current: current), .clipboard)
            XCTAssertFalse(KeyboardVoiceState(context: snapshot).canInsert(context: current))
        }
    }
}
