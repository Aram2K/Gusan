import XCTest
@testable import GusanKit

final class ScreenTakeTests: XCTestCase {
    private func frame(_ bytes: [UInt8]) -> ScreenFrame {
        ScreenFrame(requestID: UUID(), capturedAt: Date(), width: 10, height: 20, jpeg: Data(bytes))
    }
    func testFramesDeduplicateByContentAndKeepChronologicalOrder() throws {
        let first = frame([1, 2]), duplicate = frame([1, 2]), last = frame([3, 4])
        XCTAssertEqual(ScreenPrompts.uniqueFrames([first, duplicate, last]), [first, last])
        XCTAssertEqual(ScreenPrompts.uniqueFrames([first, last, frame([5])]), [first, last])
        let messages = try ScreenPrompts.messages(instruction: "Reply", text: "Around cursor", selected: "Selected draft", frames: [first, duplicate, last])
        guard let message = messages.last, case .parts(let parts) = message.content else { return XCTFail("Expected image content") }
        XCTAssertEqual(parts.count, 3)
        XCTAssertEqual(Array(parts.suffix(2)), [.imageURL(first.dataURL), .imageURL(last.dataURL)])
        guard let part = parts.first, case .text(let text) = part else { return XCTFail("Expected instruction") }
        XCTAssertTrue(text.contains("Selected draft"))
    }
    func testEmptyTakeAndEmptyInstructionFail() {
        XCTAssertEqual(ScreenPrompts.uniqueFrames([]), [])
        XCTAssertThrowsError(try ScreenPrompts.messages(instruction: "Reply", text: "", frames: [])) {
            XCTAssertEqual($0 as? ScreenError, .noFrame)
        }
        XCTAssertThrowsError(try ScreenPrompts.messages(instruction: "  ", text: "", frames: [frame([1])])) {
            XCTAssertEqual($0 as? GusanError, .emptyInput)
        }
    }
}
