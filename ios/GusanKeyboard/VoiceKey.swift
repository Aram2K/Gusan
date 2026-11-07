import UIKit
import GusanKit

private final class VoiceWaveform: UIView {
    var levels: [Float] = [] { didSet { setNeedsDisplay() } }
    override var intrinsicContentSize: CGSize { CGSize(width: 65, height: 24) }
    override func draw(_ rect: CGRect) {
        UIColor.systemRed.setStroke()
        let path = UIBezierPath(); path.lineWidth = 2
        for (index, level) in levels.enumerated() {
            let x = CGFloat(index) * rect.width / 30
            let height = max(2, CGFloat(level) * rect.height)
            path.move(to: CGPoint(x: x, y: (rect.height - height) / 2)); path.addLine(to: CGPoint(x: x, y: (rect.height + height) / 2))
        }
        path.stroke()
    }
}

final class VoiceKeyController: NSObject {
    let micButton = UIButton(type: .system)
    let bar = UIStackView()
    private weak var host: UIInputViewController?
    private let store: SessionStore
    private let channel: VoiceChannel
    private let waveform = VoiceWaveform()
    private let label = UILabel()
    private let undoButton = UIButton(type: .system)
    private var observer: DarwinObserver?
    private var timer: Timer?
    private var visible = false
    private var pending = KeyboardVoiceState()
    private var requestID: UUID? { pending.requestID }
    private var context: VoiceContext? { pending.context }
    private var insertedID: UUID? { pending.insertedID }
    private var inserted: String?
    private var original = ""
    private var undoContext: VoiceContext?
    private var localMessage: String?
    init(host: UIInputViewController, store: SessionStore) {
        self.host = host; self.store = store
        channel = VoiceChannel(container: store.resolution.container, group: store.resolution.chosen ?? "group.com.aram2k.gusan")
        super.init()
        restorePending()
        micButton.setImage(UIImage(systemName: "mic.fill"), for: .normal)
        micButton.accessibilityLabel = "Start dictation"
        micButton.backgroundColor = .tertiarySystemBackground; micButton.layer.cornerRadius = 8
        micButton.addTarget(self, action: #selector(tap), for: .touchUpInside)
        label.font = .systemFont(ofSize: 12); label.numberOfLines = 2
        undoButton.setTitle("Undo", for: .normal); undoButton.addTarget(self, action: #selector(undo), for: .touchUpInside); undoButton.isHidden = true
        waveform.setContentHuggingPriority(.required, for: .horizontal)
        undoButton.setContentHuggingPriority(.required, for: .horizontal)
        bar.spacing = 8; bar.alignment = .center
        [waveform, label, undoButton].forEach { bar.addArrangedSubview($0) }
        bar.isHidden = true
    }
    func appear() {
        restorePending()
        visible = true
        observer = channel.observe(.status) { [weak self] in self?.refresh() }
        refresh()
    }
    func disappear() { visible = false; observer = nil; timer?.invalidate(); timer = nil }
    private func currentContext() -> VoiceContext? {
        guard let proxy = host?.textDocumentProxy else { return nil }
        return VoiceContext(selected: proxy.selectedText ?? "", before: proxy.documentContextBeforeInput ?? "", after: proxy.documentContextAfterInput ?? "", documentID: proxy.documentIdentifier.uuidString)
    }
    @objc private func tap() {
        guard let host else { return }
        let signedIn = store.read() != nil
        let status = channel.readStatus()
        switch KeyboardVoice.tap(status: status, hasFullAccess: host.hasFullAccess, shared: channel.available && store.diagnostics().isShared, signedIn: signedIn) {
        case .error(let error): show(error.localizedDescription)
        case .openApp:
            guard sendStart() else { return }
            var url = URLComponents(string: "gusan://voice")!
            if let bundleID = hostBundleID() { url.queryItems = [URLQueryItem(name: "return", value: bundleID)] }
            open(url.url!)
        case .start: _ = sendStart()
        case .stop: send(.stop, id: status?.requestID)
        case .cancel: send(.cancel, id: status?.requestID)
        }
    }
    private func sendStart() -> Bool {
        pending = KeyboardVoiceState(requestID: UUID(), context: currentContext())
        do { try channel.write(keyboard: pending) }
        catch { show(error.localizedDescription); return false }
        inserted = nil; undoButton.isHidden = true
        return send(.start, id: requestID, context: context)
    }
    private func restorePending() { if let saved = channel.readKeyboardState() { pending = saved } }
    @discardableResult private func send(_ kind: VoiceCommand.Kind, id: UUID?, context: VoiceContext? = nil) -> Bool {
        guard let id else { return false }
        do {
            try channel.write(command: VoiceCommand(kind: kind, requestID: id, context: context)); channel.post(.command)
            localMessage = nil; refresh(); return true
        } catch { show(error.localizedDescription); return false }
    }
    private func show(_ message: String) { localMessage = message; label.text = message; bar.isHidden = false; waveform.isHidden = true }
    private func refresh() {
        guard visible else { return }
        let status = channel.readStatus()
        let alive = status?.isAlive() == true
        if alive && timer == nil { timer = Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { [weak self] _ in self?.refresh() } }
        if !alive { timer?.invalidate(); timer = nil }
        let phase = alive ? status?.phase ?? .off : .off
        let busy = phase == .transcribing || phase == .thinking
        micButton.setImage(UIImage(systemName: phase == .recording ? "stop.circle.fill" : busy ? "xmark.circle" : "mic.fill"), for: .normal)
        micButton.tintColor = phase == .recording ? .systemRed : .systemBlue
        micButton.accessibilityLabel = phase == .recording ? "Stop dictation" : busy ? "Cancel dictation" : "Start dictation"
        if phase == .recording { waveform.levels = Array((waveform.levels + [status?.level ?? 0]).suffix(30)) }
        waveform.isHidden = phase != .recording
        if let status, status.phase == .done, status.requestID == requestID, insertedID != requestID { deliver(status) }
        if let status, status.phase == .failed, status.requestID == requestID { localMessage = status.message }
        let message: String?
        switch phase {
        case .recording: message = "Listening…"
        case .transcribing: message = "Transcribing…"
        case .thinking: message = "Thinking…"
        case .failed: message = status?.message
        case .done: message = inserted == nil ? nil : "Done"
        default: message = nil
        }
        label.text = localMessage ?? message
        bar.isHidden = label.text == nil && inserted == nil
    }
    private func deliver(_ status: VoiceStatus) {
        guard let id = requestID else { return }
        pending.insertedID = id
        do { try channel.write(keyboard: pending) }
        catch { pending.insertedID = nil; show(error.localizedDescription); return }
        guard !pending.isStale(status: status) else { return }
        guard let result = status.result, !result.isEmpty, let context, let current = currentContext(), let proxy = host?.textDocumentProxy else {
            if let result = status.result { UIPasteboard.general.string = result; show("Text changed — result copied, paste it") }; return
        }
        if !pending.canInsert(selected: current.selected) {
            UIPasteboard.general.string = result; show("Text changed — result copied, paste it"); return
        }
        proxy.insertText(result)
        inserted = result; original = context.selected; undoContext = currentContext(); undoButton.isHidden = false
    }
    @objc private func undo() {
        guard let inserted, let expected = undoContext, currentContext() == expected, expected.selected.isEmpty, let proxy = host?.textDocumentProxy else {
            self.inserted = nil; undoButton.isHidden = true; show("Text changed. Undo is no longer available."); return
        }
        for _ in inserted { proxy.deleteBackward() }
        proxy.insertText(original)
        self.inserted = nil; undoButton.isHidden = true; show("Undone")
    }
    private func hostBundleID() -> String? {
        let selector = NSSelectorFromString("_hostBundleID")
        guard let parent = host?.parent, parent.responds(to: selector), let value = parent.perform(selector)?.takeUnretainedValue() as? String, !value.isEmpty, value != "<null>" else { return nil }
        return value
    }
    private func open(_ url: URL) {
        let selector = NSSelectorFromString("openURL:options:completionHandler:")
        var responder: UIResponder? = host
        while let current = responder {
            if current.isKind(of: NSClassFromString("UIApplication") ?? NSObject.self), current.responds(to: selector) {
                typealias Open = @convention(c) (AnyObject, Selector, NSURL, NSDictionary, @convention(block) (Bool) -> Void) -> Void
                let invoke = unsafeBitCast(current.method(for: selector), to: Open.self)
                invoke(current, selector, url as NSURL, [:]) { [weak self] success in
                    DispatchQueue.main.async { if !success { self?.fallbackOpen(url) } }
                }
                return
            }
            responder = current.next
        }
        fallbackOpen(url)
    }
    private func fallbackOpen(_ url: URL) {
        guard let context = host?.extensionContext else { show("Open the Gusan app to start voice"); return }
        context.open(url) { [weak self] success in DispatchQueue.main.async { if !success { self?.show("Open the Gusan app to start voice") } } }
    }
}
