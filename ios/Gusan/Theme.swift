import SwiftUI

enum Theme {
    static func color(_ light: UInt32, _ dark: UInt32) -> Color {
        Color(UIColor { traits in
            let rgb = traits.userInterfaceStyle == .dark ? dark : light
            return UIColor(red: CGFloat((rgb >> 16) & 255) / 255, green: CGFloat((rgb >> 8) & 255) / 255, blue: CGFloat(rgb & 255) / 255, alpha: 1)
        })
    }
    static let brandRed = color(0xD91E18, 0xE5483F)
    static let brandBlue = color(0x0033A0, 0x4A6FD8)
    static let brandOrange = color(0xF2A815, 0xF2A815)
    static let accent = color(0x2563EB, 0x3B82F6)
    static let success = color(0x16A34A, 0x22C55E)
    static let danger = color(0xDC2626, 0xEF4444)
    static let surface = Color(uiColor: .systemGroupedBackground)
    static let card = Color(uiColor: .secondarySystemGroupedBackground)
    static let pro = LinearGradient(colors: [Color(red: 0x93 / 255, green: 0x33 / 255, blue: 0xEA / 255), Color(red: 0x7E / 255, green: 0x22 / 255, blue: 0xCE / 255)], startPoint: .topLeading, endPoint: .bottomTrailing)
    static let hero = LinearGradient(colors: [brandRed, brandBlue], startPoint: .topLeading, endPoint: .bottomTrailing)
    static let website = URL(string: "https://gusan.aibuben.xyz")!
    static let pricing = URL(string: "https://gusan.aibuben.xyz/#pricing")!
    static func openSettings() { UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!) }
}

struct BrandMark: View {
    var large = false
    var body: some View {
        Image("BrandMark").resizable().scaledToFit().frame(width: large ? 96 : 32, height: large ? 96 : 32).accessibilityLabel("Gusan")
    }
}

struct AppCard<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View { VStack(alignment: .leading, spacing: 12) { content }.frame(maxWidth: .infinity, alignment: .leading).padding(16).background(Theme.card, in: RoundedRectangle(cornerRadius: 16, style: .continuous)) }
}

struct PrimaryButton: View {
    let title: String
    var icon: String? = nil
    var busy = false
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if busy { ProgressView().tint(.white) }
                else if let icon { Image(systemName: icon) }
                Text(title).multilineTextAlignment(.center)
            }.font(.headline).frame(maxWidth: .infinity, minHeight: 52).padding(.horizontal, 16)
        }.buttonStyle(.plain).foregroundStyle(.white)
            .background(Theme.accent, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
            .disabled(busy).accessibilityHint(busy ? "Please wait" : title)
    }
}

struct PlanBadge: View {
    let pro: Bool
    var body: some View {
        Text(pro ? "Gusan Pro" : "Free").font(.caption).bold().padding(.horizontal, 12).padding(.vertical, 8)
            .foregroundStyle(pro ? .white : .primary)
            .background { if pro { Capsule().fill(Theme.pro) } else { Capsule().fill(Theme.surface) } }
    }
}

struct StatusRow: View {
    let title: String
    let detail: String
    let confirmed: Bool
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: confirmed ? "checkmark.circle.fill" : "circle.dashed").foregroundStyle(confirmed ? Theme.success : .secondary).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) { Text(title).font(.subheadline).bold(); Text(detail).font(.footnote).foregroundStyle(.secondary) }
        }.accessibilityElement(children: .combine)
    }
}

struct ScreenContent<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 12) { content }.padding(20).frame(maxWidth: 680).frame(maxWidth: .infinity) }.background(Theme.surface)
    }
}
