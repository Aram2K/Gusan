import Foundation

public struct KeyboardSettingsSnapshot: Equatable {
    public let keyboardMode: KeyboardMode
    public let keyboardScreenEnabled: Bool
    public let keyClicks: Bool
    public let doubleSpacePeriod: Bool
    public let autoCapitalization: Bool
    public let pasteboardFallback: Bool

    public init(defaults: UserDefaults? = nil) {
        keyboardMode = (defaults?.string(forKey: "keyboardMode") ?? defaults?.string(forKey: "stripMode")).flatMap(KeyboardMode.init(rawValue:)) ?? .smart
        keyboardScreenEnabled = defaults?.object(forKey: "keyboardScreenEnabled") as? Bool ?? true
        keyClicks = defaults?.object(forKey: "keyClicks") as? Bool ?? true
        doubleSpacePeriod = defaults?.object(forKey: "doubleSpacePeriod") as? Bool ?? true
        autoCapitalization = defaults?.object(forKey: "autoCapitalization") as? Bool ?? true
        pasteboardFallback = defaults?.object(forKey: "pasteboardFallback") as? Bool ?? true
    }
}
