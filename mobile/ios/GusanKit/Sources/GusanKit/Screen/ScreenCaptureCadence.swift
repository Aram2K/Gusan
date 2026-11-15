import Foundation

public struct ScreenCaptureCadence {
    private var keyboardShownAt: Date?
    public init() {}
    public mutating func shouldBoost(keyboard: ScreenKeyboardState?) -> Bool {
        let shownAt = keyboard?.shownAt
        defer { keyboardShownAt = shownAt }
        return shownAt != nil && shownAt != keyboardShownAt
    }
}
