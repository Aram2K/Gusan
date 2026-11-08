import XCTest
import UIKit
@testable import GusanKeyboardPreview

@MainActor
final class KeyboardSizingTests: XCTestCase {
    func testInputViewOwnsOneHostCompatibleHeight() throws {
        let keyboard = KeyboardViewController()
        keyboard.loadViewIfNeeded()
        let input = try XCTUnwrap(keyboard.inputView)
        XCTAssertTrue(input.allowsSelfSizing)
        XCTAssertTrue(input.translatesAutoresizingMaskIntoConstraints)
        let heights = input.constraints.filter { $0.firstAttribute == .height && $0.secondItem == nil }
        XCTAssertEqual(heights.count, 1)
        XCTAssertEqual(heights.first?.priority, UILayoutPriority(999))
        XCTAssertEqual(heights.first?.constant, 262)
    }

    func testReappearingDoesNotGrowWithPreviousHostBounds() throws {
        let keyboard = KeyboardViewController()
        keyboard.loadViewIfNeeded()
        let input = try XCTUnwrap(keyboard.inputView)
        for height: CGFloat in [470, 874, 3_000, 262] {
            input.frame = CGRect(x: 0, y: 0, width: 402, height: height)
            keyboard.beginAppearanceTransition(true, animated: false)
            keyboard.endAppearanceTransition()
            keyboard.viewWillLayoutSubviews()
            let keys = try XCTUnwrap(input.subviews.first { $0 is TypingKeyboardView })
            XCTAssertEqual(keys.frame.maxY, 262)
            let fitting = input.systemLayoutSizeFitting(CGSize(width: 402, height: height), withHorizontalFittingPriority: .required, verticalFittingPriority: .fittingSizeLevel)
            XCTAssertEqual(fitting.height, 262)
            XCTAssertEqual(input.constraints.filter { $0.firstAttribute == .height && $0.secondItem == nil }.count, 1)
            keyboard.beginAppearanceTransition(false, animated: false)
            keyboard.endAppearanceTransition()
        }
    }

    func testOrientationChangesReuseTheHeightConstraint() throws {
        let keyboard = KeyboardViewController()
        keyboard.loadViewIfNeeded()
        let input = try XCTUnwrap(keyboard.inputView)
        let constraint = try XCTUnwrap(input.constraints.first { $0.firstAttribute == .height && $0.secondItem == nil })
        for (sizeClass, expected): (UIUserInterfaceSizeClass, CGFloat) in [(.compact, 208), (.regular, 262), (.compact, 208), (.regular, 262)] {
            keyboard.traitOverrides.verticalSizeClass = sizeClass
            keyboard.viewWillLayoutSubviews()
            XCTAssertEqual(constraint.constant, expected)
            XCTAssertEqual(input.intrinsicContentSize.height, expected)
            XCTAssertEqual(input.sizeThatFits(CGSize(width: 402, height: 1_124)), CGSize(width: 402, height: expected))
            XCTAssertEqual(input.systemLayoutSizeFitting(UIView.layoutFittingCompressedSize).height, expected)
        }
    }

    func testFittingIgnoresPreviousHostHeight() throws {
        let keyboard = KeyboardViewController()
        keyboard.loadViewIfNeeded()
        let input = try XCTUnwrap(keyboard.inputView)
        for height: CGFloat in [444, 874, 1_124] {
            input.frame = CGRect(x: 0, y: 0, width: 402, height: height)
            keyboard.viewWillLayoutSubviews()
            XCTAssertEqual(input.sizeThatFits(input.bounds.size), CGSize(width: 402, height: 262))
        }
    }
}
