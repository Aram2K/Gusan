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
    func testUnknownDocumentNeverReplacesOrInserts() {
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: VoiceContext(), current: VoiceContext()), .clipboard)
        XCTAssertEqual(VoiceInsertionDestination.resolve(snapshot: VoiceContext(documentID: ""), current: VoiceContext(documentID: "")), .clipboard)
    }
}
