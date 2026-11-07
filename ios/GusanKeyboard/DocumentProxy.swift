import UIKit

private let hostDocumentID = UUID()

extension UITextDocumentProxy {
    /// `documentIdentifier` is declared non-optional but is nil when the keyboard runs inside a host app
    /// (Simulator previews); bridging it then traps. Read it through KVC and fall back to a per-process id.
    var documentID: UUID {
        (self as AnyObject).value(forKey: "documentIdentifier") as? UUID ?? hostDocumentID
    }
}
