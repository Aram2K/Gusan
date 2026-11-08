import UIKit

extension UITextDocumentProxy {
    /// Hosts can return nil despite the non-optional declaration; avoid force-bridging NSUUID.
    var documentID: UUID? {
        (self as AnyObject).value(forKey: "documentIdentifier") as? UUID
    }
}
