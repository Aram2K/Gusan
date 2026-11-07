import UIKit
import GusanKit

/// What the "Screen" chip shows: nothing, a ghosted offer to start Gusan Screen, or the live control.
enum ScreenChipState { case hidden, offer, available }

/// The "Screen" chip in the toolbar. Live: Gusan blue when armed, neutral
/// otherwise. Not live: ghosted, and a tap explains how to start Gusan Screen.
final class ScreenChip: ToolbarButton {
    override init(frame: CGRect) {
        super.init(frame: frame)
        var config = UIButton.Configuration.filled()
        config.cornerStyle = .capsule
        config.imagePadding = 4
        config.contentInsets = NSDirectionalEdgeInsets(top: 6, leading: 10, bottom: 6, trailing: 10)
        config.title = "Screen"
        configuration = config
        accessibilityLabel = "Screen"
        isHidden = true
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(state: ScreenChipState, armed: Bool, theme: KeyboardTheme, traits: UITraitCollection) {
        guard var config = configuration else { return }
        let font = keyboardFont(12, style: .caption1, maximum: 20, weight: .semibold, traits: traits)
        config.image = UIImage(systemName: state == .offer ? "eye.slash" : armed ? "eye.fill" : "eye", withConfiguration: UIImage.SymbolConfiguration(font: font))
        config.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { attributes in var result = attributes; result.font = font; return result }
        config.baseBackgroundColor = armed ? KeyboardTheme.blue : theme.letter
        config.baseForegroundColor = armed ? .white : (theme.dark ? .white : KeyboardTheme.blue)
        configuration = config
        alpha = state == .offer ? 0.55 : 1
        switch state {
        case .offer:
            accessibilityValue = "Off. Gusan Screen isn't running"
            accessibilityHint = "Explains how to start Gusan Screen in the Gusan app"
        case .available, .hidden:
            accessibilityValue = armed ? "On. Your next instruction uses a picture of your screen" : "Off"
            accessibilityHint = armed ? "Stops using the screen" : "Your next instruction is answered with a picture of your screen"
        }
    }
}

/// Keyboard side of Gusan Screen: watches the broadcast heartbeat, shows the
/// chip, tells the extension when the keyboard is up (so it can freeze the
/// picture from before), and builds the job that fetches the frame on Run.
final class ScreenKeyboardController {
    let chip = ScreenChip(frame: .zero)
    var onChange: (() -> Void)?
    /// The broadcast ended (or the chip otherwise became unavailable) while the chip was armed.
    var onLost: (() -> Void)?
    /// The broadcast ended while the keyboard was up and the chip was not armed.
    var onStopped: (() -> Void)?
    /// The ghosted chip was tapped: explain how to start Gusan Screen.
    var onOffer: (() -> Void)?
    private(set) var live = false
    private(set) var explained = false
    /// When this keyboard came up; frames must predate it (frozen) or be current.
    private(set) var shownAt: Date?
    var armed = false { didSet { if armed != oldValue { onChange?() } } }
    private weak var host: UIInputViewController?
    private let channel: ScreenChannel
    private let settings: ScreenSettings
    private let group: String?
    private var observer: DarwinObserver?
    private var watchdog: Timer?
    private var visible = false
    private var geometry: (keyboard: Double, screen: Double)?
    private var publishedKeyboard = false
    init(host: UIInputViewController, store: SessionStore, pasteboardAllowed: @escaping () -> Bool) {
        self.host = host
        group = store.resolution.chosen
        channel = ScreenChannel(container: store.resolution.container, pasteboardAllowed: pasteboardAllowed)
        settings = ScreenSettings(group: store.resolution.chosen, pasteboardAccessAllowed: pasteboardAllowed)
        chip.addTarget(self, action: #selector(toggle), for: .touchUpInside)
    }
    /// The chip is never offered while the focused field is secure. (ReplayKit does
    /// not blank secure fields; the rest of the screen is still in the picture.)
    var secureField: Bool { host?.textDocumentProxy.isSecureTextEntry == true }
    /// Debug-only: `debugScreenPreview` in the shared defaults fakes a live broadcast so the chip can be screenshotted in the Simulator host.
    private var previewEnabled = false
    /// Debug-only: `debugScreenOffer` fakes "explained, Full Access, not live" so the ghosted chip can be screenshotted.
    private var offerPreview = false
    var state: ScreenChipState {
        if previewEnabled { return .available }
        if offerPreview { return live ? .available : .offer }
        guard explained, !secureField, host?.hasFullAccess == true else { return .hidden }
        return live ? .available : .offer
    }
    var available: Bool { state == .available }
    var frames: ScreenFrameProvider { ScreenChannelFrameProvider(channel: channel, keyboardShownAt: shownAt) }
    var diagnostics: String { "Screen channel: " + channel.diagnosticsDescription }
    /// The first time the chip is armed the keyboard says what Run will send.
    var needsArmReminder: Bool { !previewEnabled && !settings.armReminderShown }
    func markArmReminderShown() { settings.armReminderShown = true }
    func appear() {
        visible = true
        shownAt = Date()
        publishedKeyboard = false
        #if DEBUG
        previewEnabled = group.flatMap { UserDefaults(suiteName: $0) }?.bool(forKey: "debugScreenPreview") == true || UserDefaults.standard.bool(forKey: "debugScreenPreview")
        if previewEnabled { live = true; explained = true; armed = true; onChange?() }
        offerPreview = group.flatMap { UserDefaults(suiteName: $0) }?.bool(forKey: "debugScreenOffer") == true || UserDefaults.standard.bool(forKey: "debugScreenOffer")
        if offerPreview { explained = true; onChange?() }
        #endif
        observer = channel.observe(.status) { [weak self] in self?.refresh() }
        refresh()
    }
    func disappear() {
        visible = false; observer = nil; watchdog?.invalidate(); watchdog = nil
        if publishedKeyboard { channel.clearKeyboard(); channel.post(.keyboard); publishedKeyboard = false }
        geometry = nil
    }
    /// Where the keyboard is, so the extension can freeze a pre-keyboard frame or crop the keyboard out.
    func publishKeyboard(height: Double, screenHeight: Double) {
        geometry = (height, screenHeight)
        publishKeyboardIfNeeded(force: !publishedKeyboard)
    }
    private func publishKeyboardIfNeeded(force: Bool = false) {
        guard live, visible, let geometry, let shownAt, force || channel.keyboardNeedsRefresh() else { return }
        guard channel.write(keyboard: ScreenKeyboardState(shownAt: shownAt, keyboardHeight: geometry.keyboard, screenHeight: geometry.screen)) else { return }
        publishedKeyboard = true
        channel.post(.keyboard)
    }
    /// Reads the heartbeat. Runs on each status notification; a one-shot watchdog
    /// catches an extension that dies without a final post. No periodic polling.
    func refresh() {
        if previewEnabled || offerPreview { return }
        guard visible, host?.hasFullAccess == true else { setLive(false); return }
        explained = settings.explained
        setLive(channel.isLive())
        watchdog?.invalidate(); watchdog = nil
        if live {
            watchdog = Timer.scheduledTimer(withTimeInterval: ScreenStatus.liveWindow + 1, repeats: false) { [weak self] _ in self?.refresh() }
            publishKeyboardIfNeeded()
        }
    }
    private func setLive(_ value: Bool) {
        let wasAvailable = available, wasLive = live
        live = value
        let lost = armed && !available
        if !available { armed = false }
        if available != wasAvailable { onChange?() }
        if lost { onLost?() } else if wasLive && !live && visible { onStopped?() }
    }
    func update(theme: KeyboardTheme, busy: Bool, traits: UITraitCollection) {
        let state = state
        chip.isHidden = state == .hidden
        chip.isEnabled = !busy
        chip.update(state: state, armed: armed, theme: theme, traits: traits)
    }
    func summary(_ text: String) -> String { armed ? text + " · Screen" : text }
    func job(instruction: String, text: String, selected: String, accessToken: String, kind: JobResult.Kind) throws -> GusanJob {
        guard !secureField else { throw ScreenError.secureField }
        guard explained else { throw ScreenError.notExplained }
        guard live else { throw ScreenError.notLive }
        return ScreenInstructionJob(instruction: instruction, text: text, selected: selected, accessToken: accessToken, kind: kind == .replaceContext ? .insert : kind, frames: frames)
    }
    @objc private func toggle() {
        if state == .offer { onOffer?() } else { armed.toggle() }
    }
}
