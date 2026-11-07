import UIKit

#if DEBUG
private let hostDocumentID = UUID()
#endif

extension UITextDocumentProxy {
    /// `documentIdentifier` is declared non-optional but is nil when the keyboard runs inside a host app
    /// (the Simulator preview); bridging it then traps. Debug builds read it through KVC with a fallback;
    /// the shipping keyboard uses the property directly.
    var documentID: UUID {
        #if DEBUG
        (self as AnyObject).value(forKey: "documentIdentifier") as? UUID ?? hostDocumentID
        #else
        documentIdentifier
        #endif
    }
}
