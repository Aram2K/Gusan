import SwiftUI
import UIKit

struct KeyboardDismissal: UIViewRepresentable {
    func makeUIView(context: Context) -> KeyboardDismissalView { KeyboardDismissalView() }
    func updateUIView(_ uiView: KeyboardDismissalView, context: Context) {}
}

final class KeyboardDismissalView: UIView, UIGestureRecognizerDelegate {
    private lazy var tap: UITapGestureRecognizer = {
        let recognizer = UITapGestureRecognizer(target: self, action: #selector(dismissKeyboard))
        recognizer.cancelsTouchesInView = false
        recognizer.delaysTouchesBegan = false
        recognizer.delaysTouchesEnded = false
        recognizer.delegate = self
        return recognizer
    }()

    override func didMoveToWindow() {
        super.didMoveToWindow()
        tap.view?.removeGestureRecognizer(tap)
        window?.addGestureRecognizer(tap)
    }

    @objc private func dismissKeyboard() { window?.endEditing(true) }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldReceive touch: UITouch) -> Bool {
        var view = touch.view
        while let current = view {
            if current is UITextField || (current as? UITextView)?.isEditable == true { return false }
            view = current.superview
        }
        return true
    }

    func gestureRecognizer(_ gestureRecognizer: UIGestureRecognizer, shouldRecognizeSimultaneouslyWith otherGestureRecognizer: UIGestureRecognizer) -> Bool { true }
}
