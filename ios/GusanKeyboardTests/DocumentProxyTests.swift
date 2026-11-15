import XCTest
import UIKit
@testable import GusanKeyboardPreview

@MainActor
final class DocumentProxyTests: XCTestCase {
    func testNilIdentifierRemainsUnknownAcrossReadsAndFields() {
        let proxy = StubDocumentProxy()
        XCTAssertNil(proxy.documentID)
        XCTAssertNil(proxy.documentID)
        XCTAssertNil(StubDocumentProxy().documentID)
    }

    func testObjectiveCIdentifierIsPreserved() {
        let proxy = StubDocumentProxy()
        let identifier = UUID()
        proxy.rawIdentifier = identifier as NSUUID
        XCTAssertEqual(proxy.documentID, identifier)
    }

    func testUnexpectedIdentifierTypeRemainsUnknown() {
        let proxy = StubDocumentProxy()
        proxy.rawIdentifier = "not a UUID"
        XCTAssertNil(proxy.documentID)
    }
}

private final class StubDocumentProxy: NSObject, UITextDocumentProxy {
    var rawIdentifier: Any?
    var documentContextBeforeInput: String? { nil }
    var documentContextAfterInput: String? { nil }
    var selectedText: String? { nil }
    var documentInputMode: UITextInputMode? { nil }
    var hasText: Bool { false }
    var documentIdentifier: UUID {
        XCTFail("Must read the nullable Objective-C value instead of force-bridging")
        return UUID()
    }

    override func value(forKey key: String) -> Any? {
        key == "documentIdentifier" ? rawIdentifier : super.value(forKey: key)
    }

    func adjustTextPosition(byCharacterOffset offset: Int) {}
    func insertText(_ text: String) {}
    func deleteBackward() {}
    func setMarkedText(_ markedText: String, selectedRange: NSRange) {}
    func unmarkText() {}
}
