import UIKit
import GusanKit

final class ScreenKeyboardController {
    var onChange: (() -> Void)?
    var onLost: (() -> Void)?
    private(set) var live = false
    private(set) var feedback: String?
    private weak var host: UIInputViewController?
    private let channel: ScreenChannel
    private let group: String?
    private var observer: DarwinObserver?
    private var watchdog: Timer?
    private var feedbackTimer: Timer?
    private var visible = false
    private var shownAt: Date?
    private var geometry: (keyboard: Double, screen: Double)?
    private var publishedKeyboard = false
    private var previewEnabled = false
    init(host: UIInputViewController, store: SessionStore, pasteboardAllowed: @escaping () -> Bool) {
        self.host = host
        group = store.resolution.chosen
        channel = ScreenChannel(container: store.resolution.container, pasteboardAllowed: pasteboardAllowed)
    }
    var secureField: Bool { host?.textDocumentProxy.isSecureTextEntry == true }
    func frames(phase: ScreenRequest.Phase) -> ScreenFrameProvider {
        ScreenChannelFrameProvider(channel: channel, phase: phase, accessAllowed: { [weak self] in
            self?.host?.hasFullAccess == true && self?.secureField == false
        })
    }
    func appear() {
        visible = true; shownAt = Date(); publishedKeyboard = false
        #if DEBUG
        previewEnabled = group.flatMap { UserDefaults(suiteName: $0) }?.bool(forKey: "debugScreenPreview") == true || UserDefaults.standard.bool(forKey: "debugScreenPreview")
        #endif
        observer = channel.observe(.status) { [weak self] in self?.refresh() }
        refresh()
    }
    func disappear() {
        visible = false; observer = nil; watchdog?.invalidate(); watchdog = nil
        feedbackTimer?.invalidate(); feedbackTimer = nil; feedback = nil
        if publishedKeyboard { channel.clearKeyboard(); channel.post(.keyboard); publishedKeyboard = false }
        geometry = nil
    }
    func publishKeyboard(height: Double, screenHeight: Double) {
        geometry = (height, screenHeight)
        publishKeyboardIfNeeded(force: !publishedKeyboard)
    }
    private func publishKeyboardIfNeeded(force: Bool = false) {
        guard live, host?.hasFullAccess == true, visible, let geometry, let shownAt, force || channel.keyboardNeedsRefresh() else { return }
        guard channel.write(keyboard: ScreenKeyboardState(shownAt: shownAt, keyboardHeight: geometry.keyboard, screenHeight: geometry.screen)) else { return }
        publishedKeyboard = true; channel.post(.keyboard)
    }
    func refresh() {
        guard visible else { return }
        let wasLive = live
        live = host?.hasFullAccess == true && (previewEnabled || channel.isLive())
        watchdog?.invalidate(); watchdog = nil
        if live {
            watchdog = Timer.scheduledTimer(withTimeInterval: ScreenStatus.liveWindow + 1, repeats: false) { [weak self] _ in self?.refresh() }
            publishKeyboardIfNeeded()
        }
        if wasLive != live { onChange?() }
        if wasLive && !live { onLost?() }
    }
    func reportCapture(_ message: String?) {
        guard let message else { return }
        feedbackTimer?.invalidate(); feedback = message; onChange?()
        feedbackTimer = Timer.scheduledTimer(withTimeInterval: 2, repeats: false) { [weak self] _ in
            self?.feedback = nil; self?.feedbackTimer = nil; self?.onChange?()
        }
    }
}
