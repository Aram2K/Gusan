import XCTest
import GusanKit

final class ReplacementUndoTests: XCTestCase {
    func testContextReplacementThenTypingKeepsUndoAndRedoCursorConsistent() throws {
        var record = ReplacementUndo(original: "Hello world", replacement: "Hi", snapshot: VoiceContext(documentID: "one"), restoreUTF16: 5)
        XCTAssertTrue(record.append(" 👋"))
        let undo = try XCTUnwrap(record.step(context: VoiceContext(before: "Hi 👋", documentID: "one")))
        XCTAssertEqual(undo.advanceUTF16, -3); XCTAssertEqual(undo.text, "Hello world")
        XCTAssertEqual(undo.restoreUTF16, 3)
        let redo = try XCTUnwrap(record.inverted().step(context: VoiceContext(before: "Hello world 👋", documentID: "one")))
        XCTAssertEqual(redo.advanceUTF16, -3); XCTAssertEqual(redo.deleteCount, 11)
        XCTAssertEqual(redo.text, "Hi"); XCTAssertEqual(redo.restoreUTF16, 3)
    }
    func testTypingAndDeletingAfterTheResultKeepUndo() throws {
        var record = ReplacementUndo(original: "Old", replacement: "New", snapshot: VoiceContext(before: "Prefix ", after: "Tail", documentID: "one"))
        XCTAssertTrue(record.append(" 👋!")); XCTAssertTrue(record.deleteBackward())
        let current = VoiceContext(before: "Prefix New 👋", after: "Tail", documentID: "one")
        let step = try XCTUnwrap(record.step(context: current))
        XCTAssertEqual(step.advanceUTF16, -3); XCTAssertEqual(step.deleteCount, 3)
        XCTAssertEqual(step.text, "Old"); XCTAssertEqual(step.restoreUTF16, 3)
        XCTAssertEqual(record.inverted().step(context: VoiceContext(before: "Prefix Old 👋", after: "Tail", documentID: "one"))?.text, "New")
    }
    func testChangesTouchingTheResultOrDocumentRejectUndo() {
        var record = ReplacementUndo(original: "Old", replacement: "New", snapshot: VoiceContext(before: "Prefix ", documentID: "one"))
        XCTAssertFalse(record.deleteBackward())
        for context in [VoiceContext(before: "Prefix Ne", documentID: "one"), VoiceContext(before: "Prefix New", documentID: "two"), VoiceContext(selected: "New", before: "Prefix ", documentID: "one")] {
            XCTAssertNil(record.step(context: context))
        }
    }
    func testEmptyOriginalSupportsRedoWithoutDeletingUserText() throws {
        let record = ReplacementUndo(original: "", replacement: "New", snapshot: VoiceContext(documentID: "one"))
        XCTAssertEqual(record.step(context: VoiceContext(before: "New", documentID: "one"))?.text, "")
        let redo = try XCTUnwrap(record.inverted().step(context: VoiceContext(documentID: "one")))
        XCTAssertEqual(redo.deleteCount, 0); XCTAssertEqual(redo.text, "New")
        XCTAssertNil(record.inverted().step(context: VoiceContext(before: "Other", documentID: "one")))
    }
    func testContextCursorRestorationAndTruncatedWindow() throws {
        let record = ReplacementUndo(original: "Hello 👋!", replacement: "New text", snapshot: VoiceContext(documentID: "one"), restoreUTF16: 3)
        XCTAssertEqual(record.step(context: VoiceContext(before: "text", documentID: "one"))?.restoreUTF16, -3)
        let reverse = record.inverted()
        XCTAssertEqual(reverse.step(context: VoiceContext(before: "Hello ", after: "👋!", documentID: "one"))?.advanceUTF16, 3)
        var editingInside = reverse
        XCTAssertFalse(editingInside.append("x"))
    }
}
