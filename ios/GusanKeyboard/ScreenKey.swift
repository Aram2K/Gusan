import UIKit
import GusanKit

/// The "Screen" chip in the toolbar: visible only while Gusan Screen is live.
final class ScreenChip: ToolbarButton {
    override init(frame: CGRect) {
        super.init(frame: frame)
        var config = UIButton.Configuration.filled()
        config.cornerStyle = .capsule
        config.imagePadding = 4
        config.contentInsets = NSDirectionalEdgeInsets(top: 4, leading: 8, bottom: 4, trailing: 8)
        config.title = "Screen"
        config.image = UIImage(systemName: "eye", withConfiguration: UIImage.SymbolConfiguration(pointSize: 12, weight: .semibold))
        config.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { attributes in
            var result = attributes; result.font = .systemFont(ofSize: 12, weight: .semibold); return result
        }
        configuration = config
        accessibilityLabel = "Use screen"
        isHidden = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(armed: Bool, theme: KeyboardTheme) {
        guard var config = configuration else { return }
        config.image = UIImage(systemName: armed ? "eye.fill" : "eye", withConfiguration: UIImage.SymbolConfiguration(pointSize: 12, weight: .semibold))
        config.baseBackgroundColor = armed ? KeyboardTheme.blue : theme.letter
        config.baseForegroundColor = armed ? .white : (theme.dark ? .white : KeyboardTheme.blue)
        configuration = config
        accessibilityValue = armed ? "On. Your next instruction uses the screen." : "Off"
        accessibilityHint = armed ? "Stops using the screen" : "Your next instruction is answered with a picture of your screen"
    }
}

/// Keyboard side of Gusan Screen: watches the broadcast heartbeat, shows the
/// chip, and builds the job that fetches the current frame when the user asks.
final class ScreenKeyboardController {
    let chip = ScreenChip(frame: .zero)
    var onChange: (() -> Void)?
    private(set) var live = false
    private(set) var explained = false
    var armed = false { didSet { if armed != oldValue { onChange?() } } }
    private weak var host: UIInputViewController?
    private let channel: ScreenChannel
    private let settings: ScreenSettings
    private let group: String?
    private var observer: DarwinObserver?
    private var timer: Timer?
    private var visible = false
    init(host: UIInputViewController, store: SessionStore, pasteboardAllowed: @escaping () -> Bool) {
        self.host = host
        group = store.resolution.chosen
        channel = ScreenChannel(container: store.resolution.container, pasteboardAllowed: pasteboardAllowed)
        settings = ScreenSettings(group: store.resolution.chosen, pasteboardAccessAllowed: pasteboardAllowed)
        chip.addTarget(self, action: #selector(toggle), for: .touchUpInside)
    }
    /// Secure fields never use the screen (iOS blanks them in the broadcast anyway).
    var secureField: Bool { host?.textDocumentProxy.isSecureTextEntry == true }
    /// Debug-only: `debugScreenPreview` in the shared defaults fakes a live broadcast so the chip can be screenshotted in the Simulator host.
    private var previewEnabled = false
    var available: Bool { previewEnabled || (live && explained && !secureField && host?.hasFullAccess == true) }
    var frames: ScreenFrameProvider { ScreenChannelFrameProvider(channel: channel) }
    var diagnostics: String { "Screen channel: " + channel.diagnosticsDescription }
    func appear() {
        visible = true
        #if DEBUG
        previewEnabled = group.flatMap { UserDefaults(suiteName: $0) }?.bool(forKey: "debugScreenPreview") == true || UserDefaults.standard.bool(forKey: "debugScreenPreview")
        if previewEnabled { live = true; explained = true; armed = true; onChange?() }
        #endif
        observer = channel.observe(.status) { [weak self] in self?.refresh() }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in self?.refresh() }
        refresh()
    }
    func disappear() { visible = false; observer = nil; timer?.invalidate(); timer = nil }
    func refresh() {
        if previewEnabled { return }
        guard visible, host?.hasFullAccess == true else { setLive(false); return }
        explained = settings.explained
        setLive(channel.isLive())
    }
    private func setLive(_ value: Bool) {
        let wasAvailable = available
        live = value
        if !available { armed = false }
        if available != wasAvailable { onChange?() }
    }
    func update(theme: KeyboardTheme, busy: Bool) {
        chip.isHidden = !available
        chip.isEnabled = !busy
        chip.update(armed: armed, theme: theme)
    }
    func summary(_ text: String) -> String { armed ? "Screen + " + text : text }
    func job(instruction: String, text: String, accessToken: String, kind: JobResult.Kind) throws -> GusanJob {
        guard !secureField else { throw ScreenError.secureField }
        guard explained else { throw ScreenError.notExplained }
        guard live else { throw ScreenError.notLive }
        return ScreenInstructionJob(instruction: instruction, text: text, accessToken: accessToken, kind: kind == .replaceContext ? .insert : kind, frames: frames)
    }
    @objc private func toggle() { armed.toggle() }
}
