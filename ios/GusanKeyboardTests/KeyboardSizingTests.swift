import XCTest
import UIKit
@testable import GusanKeyboardPreview

@MainActor
final class KeyboardSizingTests: XCTestCase {
    func testInputViewOwnsOneRequiredHeight() throws {
        let keyboard = KeyboardViewController()
        keyboard.loadViewIfNeeded()
        let input = try XCTUnwrap(keyboard.inputView)
        XCTAssertTrue(input.allowsSelfSizing)
        let heights = input.constraints.filter { $0.firstAttribute == .height && $0.secondItem == nil }
        XCTAssertEqual(heights.count, 1)
        XCTAssertEqual(heights.first?.priority, .required)
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
            XCTAssertEqual(input.systemLayoutSizeFitting(UIView.layoutFittingCompressedSize).height, expected)
        }
    }
}
