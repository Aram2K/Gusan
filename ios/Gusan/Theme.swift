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
    /// Links, chevrons, icons and tint: the dark value is bright enough for 4.5:1 on the dark grain and glass rows.
    static let accent = color(0x0058B8, 0x5AA9FF)
    /// Filled primary buttons (white label): the deeper dark blue keeps the white text readable.
    static let accentFill = color(0x0058B8, 0x0A84FF)
    /// Secondary text (footers, captions, hints): warm greys that reach 4.5:1 on the sand and the dark grain.
    static let secondaryText = color(0x625C52, 0xD0C9BD)
    static let agent = color(0x5E5CE6, 0x8482FF)
    static let success = color(0x1D7A3B, 0x30D158)
    static let inactiveTab = color(0x8E8E93, 0x8E8E93)
    static let danger = color(0xDC2626, 0xEF4444)
    /// Background tint behind every screen. Switch to `.teal` or `.plum` here.
    static let tint: Tint = .sand
    struct Tint {
        let lightTop: UInt32, lightBottom: UInt32, darkTop: UInt32, darkBottom: UInt32, glow: UInt32
        static let sand = Tint(lightTop: 0xF7F1E6, lightBottom: 0xE8DBC4, darkTop: 0x1E1A15, darkBottom: 0x2B241A, glow: 0xE6C98F)
        static let teal = Tint(lightTop: 0xE6F1F0, lightBottom: 0xCBE0DD, darkTop: 0x101C1D, darkBottom: 0x182B2C, glow: 0x7FB8B2)
        static let plum = Tint(lightTop: 0xF2EAF0, lightBottom: 0xE0D2DC, darkTop: 0x1C1620, darkBottom: 0x2B1F2D, glow: 0xB58BAE)
        var top: Color { Theme.color(lightTop, darkTop) }
        var bottom: Color { Theme.color(lightBottom, darkBottom) }
        var glowColor: Color { Theme.color(glow, glow) }
    }
    /// Tiled monochrome noise, generated once from a fixed seed (no asset, no network).
    static let grain: UIImage = {
        let side = 128
        var state: UInt32 = 0x6D2B_79F5
        var bytes = [UInt8](repeating: 0, count: side * side)
        for index in bytes.indices {
            state = state &* 1_664_525 &+ 1_013_904_223
            bytes[index] = UInt8(truncatingIfNeeded: state >> 24)
        }
        guard let provider = CGDataProvider(data: Data(bytes) as CFData),
              let image = CGImage(width: side, height: side, bitsPerComponent: 8, bitsPerPixel: 8, bytesPerRow: side,
                                  space: CGColorSpaceCreateDeviceGray(), bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.none.rawValue),
                                  provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)
        else { return UIImage() }
        return UIImage(cgImage: image, scale: 2, orientation: .up)
    }()
    static let pro = LinearGradient(colors: [Color(red: 0x93 / 255, green: 0x33 / 255, blue: 0xEA / 255), Color(red: 0x7E / 255, green: 0x22 / 255, blue: 0xCE / 255)], startPoint: .topLeading, endPoint: .bottomTrailing)
    static let hero = LinearGradient(colors: [brandRed, brandBlue], startPoint: .topLeading, endPoint: .bottomTrailing)
    static let website = URL(string: "https://gusan.aibuben.xyz")!
    static let pricing = URL(string: "https://gusan.aibuben.xyz/#pricing")!
    static func configureTabBar() {
        UITabBar.appearance().unselectedItemTintColor = UIColor(inactiveTab)
        if #available(iOS 26, *) { return } // native Liquid Glass tab bar
        let appearance = UITabBarAppearance()
        appearance.configureWithDefaultBackground()
        for item in [appearance.stackedLayoutAppearance, appearance.inlineLayoutAppearance, appearance.compactInlineLayoutAppearance] {
            item.normal.iconColor = UIColor(inactiveTab)
            item.normal.titleTextAttributes = [.foregroundColor: UIColor(inactiveTab)]
        }
        UITabBar.appearance().standardAppearance = appearance
        UITabBar.appearance().scrollEdgeAppearance = appearance
    }
    static func openSettings() { UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!) }
}

struct BrandMark: View {
    var large = false
    var body: some View {
        Image("BrandMark").resizable().scaledToFit().frame(width: large ? 96 : 32, height: large ? 96 : 32).accessibilityLabel("Gusan")
    }
}

/// Calm tint, a soft glow and a grainy texture; sits behind every screen in light and dark.
struct GrainBackground: View {
    @Environment(\.colorScheme) private var scheme
    var body: some View {
        ZStack {
            LinearGradient(colors: [Theme.tint.top, Theme.tint.bottom], startPoint: .top, endPoint: .bottom)
            RadialGradient(colors: [Theme.tint.glowColor.opacity(scheme == .dark ? 0.18 : 0.42), .clear],
                           center: UnitPoint(x: 0.15, y: 0.05), startRadius: 0, endRadius: 420)
            Image(uiImage: Theme.grain).resizable(resizingMode: .tile)
                .opacity(scheme == .dark ? 0.2 : 0.12)
                .blendMode(.overlay)
        }
        .compositingGroup()
        .ignoresSafeArea()
        .allowsHitTesting(false)
        .accessibilityHidden(true)
    }
}

/// Translucent row background for `List`/`Form` rows over the grain; `tint` adds a faint colour wash.
struct GlassRowBackground: View {
    var tint: Color? = nil
    var body: some View {
        #if compiler(>=6.2)
        if #available(iOS 26, *), let tint {
            Color.clear.glassEffect(.regular.tint(tint.opacity(0.22)), in: Rectangle())
        } else {
            material
        }
        #else
        material
        #endif
    }
    private var material: some View {
        ZStack {
            Rectangle().fill(.thinMaterial)
            if let tint { tint.opacity(0.1) }
        }
    }
}

/// `List` whose rows sit on translucent glass over the grain background.
struct GlassList<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        List { content.listRowBackground(GlassRowBackground()) }.listStyle(.insetGrouped).gusanScreen()
    }
}

extension View {
    /// Hides the system list background and shows the grain behind it.
    func gusanScreen() -> some View { scrollContentBackground(.hidden).background(GrainBackground()) }

    /// Liquid Glass on iOS 26+; blurred `.ultraThinMaterial` with a light edge highlight on iOS 17–25.
    @ViewBuilder func glassSurface<S: InsettableShape>(_ shape: S, tint: Color? = nil, interactive: Bool = false) -> some View {
        #if compiler(>=6.2)
        if #available(iOS 26, *) {
            glassEffect(Glass.gusan(tint: tint, interactive: interactive), in: shape)
        } else {
            materialGlass(shape, tint: tint)
        }
        #else
        materialGlass(shape, tint: tint)
        #endif
    }

    private func materialGlass<S: InsettableShape>(_ shape: S, tint: Color?) -> some View {
        background {
            ZStack {
                shape.fill(.ultraThinMaterial)
                if let tint { shape.fill(tint.opacity(0.85)) }
                shape.strokeBorder(LinearGradient(colors: [.white.opacity(0.55), .white.opacity(0.08)], startPoint: .topLeading, endPoint: .bottomTrailing), lineWidth: 1)
            }
        }
    }
}

#if compiler(>=6.2)
@available(iOS 26, *)
private extension Glass {
    static func gusan(tint: Color?, interactive: Bool) -> Glass {
        var glass = Glass.regular
        if let tint { glass = glass.tint(tint) }
        if interactive { glass = glass.interactive() }
        return glass
    }
}
#endif

/// `NavigationLink` row with its own chevron in `Theme.secondaryText`; the system chevron is too faint on the dark grain.
struct GlassNavigationLink<Destination: View, Label: View>: View {
    @ViewBuilder var destination: Destination
    @ViewBuilder var label: Label
    var body: some View {
        HStack(spacing: 8) {
            label
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(.footnote.weight(.semibold)).foregroundStyle(Theme.secondaryText).accessibilityHidden(true)
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .contentShape(Rectangle())
        .accessibilityElement(children: .combine).accessibilityAddTraits(.isButton)
        .background { NavigationLink { destination } label: { EmptyView() }.opacity(0).accessibilityHidden(true) }
    }
}

struct AppCard<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View { VStack(alignment: .leading, spacing: 12) { content }.frame(maxWidth: .infinity, alignment: .leading).padding(16).glassSurface(RoundedRectangle(cornerRadius: 16, style: .continuous)) }
}

struct PrimaryButton: View {
    let title: String
    var icon: String? = nil
    var busy = false
    var tint: Color = Theme.accentFill
    let action: () -> Void
    var body: some View {
        Button(action: action) {
            HStack(spacing: 8) {
                if busy { ProgressView().tint(.white) }
                else if let icon { Image(systemName: icon) }
                Text(title).multilineTextAlignment(.center)
            }.font(.headline).frame(maxWidth: .infinity, minHeight: 52).padding(.horizontal, 16)
                .contentShape(Rectangle())
        }.buttonStyle(.plain).foregroundStyle(.white)
            .glassSurface(RoundedRectangle(cornerRadius: 14, style: .continuous), tint: tint, interactive: true)
            .disabled(busy).accessibilityHint(busy ? "Please wait" : title)
    }
}

struct PlanBadge: View {
    let pro: Bool
    var body: some View {
        Text(pro ? "Gusan Pro" : "Free").font(.caption).bold().padding(.horizontal, 12).padding(.vertical, 8)
            .foregroundStyle(pro ? .white : .primary)
            .background { if pro { Capsule().fill(Theme.pro) } else { Capsule().fill(.thinMaterial) } }
    }
}

struct StatusRow: View {
    let title: String
    let detail: String
    let confirmed: Bool
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: confirmed ? "checkmark.circle.fill" : "circle.dashed").foregroundStyle(confirmed ? Theme.success : Theme.secondaryText).accessibilityHidden(true)
            VStack(alignment: .leading, spacing: 4) { Text(title).font(.subheadline).bold(); Text(detail).font(.footnote).foregroundStyle(Theme.secondaryText) }
        }.accessibilityElement(children: .combine)
    }
}

struct ScreenContent<Content: View>: View {
    @ViewBuilder var content: Content
    var body: some View {
        ScrollView { VStack(alignment: .leading, spacing: 12) { content }.padding(20).frame(maxWidth: 680).frame(maxWidth: .infinity) }.background(GrainBackground())
    }
}
