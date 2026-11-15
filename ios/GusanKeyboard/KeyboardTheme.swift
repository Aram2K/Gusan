import UIKit

struct KeyboardTheme {
    let dark: Bool
    static let blue = UIColor { $0.userInterfaceStyle == .dark ? UIColor(red: 0x3b / 255, green: 0x82 / 255, blue: 0xf6 / 255, alpha: 1) : UIColor(red: 0x25 / 255, green: 0x63 / 255, blue: 0xeb / 255, alpha: 1) }
    static let purpleStart = UIColor(red: 0xa8 / 255, green: 0x55 / 255, blue: 0xf7 / 255, alpha: 1)
    static let purpleEnd = UIColor(red: 0x7e / 255, green: 0x22 / 255, blue: 0xce / 255, alpha: 1)

    init(appearance: UIKeyboardAppearance?, traits: UITraitCollection) {
        dark = appearance == .dark || (appearance != .light && traits.userInterfaceStyle == .dark)
    }
    var background: UIColor { color(light: 0xD1D3D9, dark: 0x2C2C2E) }
    var letter: UIColor { color(light: 0xFFFFFF, dark: 0x6B6B6B) }
    var special: UIColor { color(light: 0xACB3BC, dark: 0x4A4A4A) }
    var foreground: UIColor { dark ? .white : .black }
    var brandRed: UIColor { color(light: 0xD91E18, dark: 0xE5483F) }
    var brandBlue: UIColor { color(light: 0x0033A0, dark: 0x4A6FD8) }
    var brandOrange: UIColor { color(light: 0xF2A815, dark: 0xF2A815) }
    var success: UIColor { color(light: 0x16A34A, dark: 0x22C55E) }
    var danger: UIColor { color(light: 0xDC2626, dark: 0xEF4444) }
    var shadow: UIColor { color(light: 0x898B90, dark: 0x141414) }
    private func color(light: UInt32, dark: UInt32) -> UIColor {
        let rgb = self.dark ? dark : light
        return UIColor(red: CGFloat((rgb >> 16) & 255) / 255, green: CGFloat((rgb >> 8) & 255) / 255, blue: CGFloat(rgb & 255) / 255, alpha: 1)
    }
}
