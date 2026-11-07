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
    private let copyButton = UIButton(type: .system)
    private let reviewButton = UIButton(type: .system)
    private let settingsButton = UIButton(type: .system)
    private var settingsURL: URL?
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
    private var reviewResult: String?
    private var resultPanel: UIView?
    private var reviewContext: VoiceContext?
    private var reviewHeading: UILabel?
    init(host: UIInputViewController, store: SessionStore) {
        self.host = host; self.store = store
        channel = store.makeVoiceChannel()
        super.init()
        restorePending()
        micButton.setImage(UIImage(systemName: "mic.fill"), for: .normal)
        micButton.accessibilityLabel = "Start dictation"
        micButton.backgroundColor = .tertiarySystemBackground; micButton.layer.cornerRadius = 8
        micButton.addTarget(self, action: #selector(tap), for: .touchUpInside)
        label.font = .systemFont(ofSize: 12); label.numberOfLines = 2
        undoButton.setTitle("Undo", for: .normal); undoButton.addTarget(self, action: #selector(undo), for: .touchUpInside); undoButton.isHidden = true
        copyButton.setTitle("Copy", for: .normal); copyButton.accessibilityLabel = "Copy voice result"
        copyButton.addTarget(self, action: #selector(copyResult), for: .touchUpInside)
        reviewButton.setTitle("View", for: .normal); reviewButton.accessibilityLabel = "Review voice result"
        reviewButton.addTarget(self, action: #selector(viewResult), for: .touchUpInside)
        settingsButton.setTitle("Open Settings", for: .normal)
        settingsButton.addTarget(self, action: #selector(openSettings), for: .touchUpInside)
        for button in [copyButton, reviewButton, settingsButton] {
            button.isHidden = true
            button.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
            button.setContentCompressionResistancePriority(.required, for: .horizontal)
        }
        waveform.setContentHuggingPriority(.required, for: .horizontal)
        undoButton.setContentHuggingPriority(.required, for: .horizontal)
        bar.spacing = 8; bar.alignment = .center
        [waveform, label, undoButton, reviewButton, copyButton, settingsButton].forEach { bar.addArrangedSubview($0) }
        bar.isHidden = true
    }
    func appear() {
        restorePending()
        visible = true
        observer = channel.observe(.status) { [weak self] in self?.refresh() }
        refresh()
    }
    func toggle() { tap() }
    func dismiss() { inserted = nil; localMessage = nil; clearReview(); undoButton.isHidden = true; refresh() }
    func disappear() { visible = false; observer = nil; timer?.invalidate(); timer = nil; closeResult() }
    private func currentContext() -> VoiceContext? {
        guard let proxy = host?.textDocumentProxy else { return nil }
        return VoiceContext(selected: proxy.selectedText ?? "", before: proxy.documentContextBeforeInput ?? "", after: proxy.documentContextAfterInput ?? "", documentID: proxy.documentIdentifier.uuidString)
    }
    @objc private func tap() {
        guard let host else { return }
        let signedIn = store.read() != nil
        let status = channel.readStatus()
        settingsButton.isHidden = true; settingsURL = nil
        switch KeyboardVoice.tap(status: status, hasFullAccess: host.hasFullAccess, shared: channel.available, signedIn: signedIn, fallbackOff: channel.unavailableError == .fallbackOff) {
        case .error(let error):
            if error == .fullAccessRequired || error == .fallbackOff {
                settingsURL = URL(string: error == .fullAccessRequired ? "gusan://open-settings" : "gusan://keyboard-settings")
                settingsButton.setTitle(error == .fullAccessRequired ? "Open Settings" : "Settings", for: .normal)
                settingsButton.isHidden = false
            }
            show(error.localizedDescription)
        case .openApp:
            guard sendStart(), let requestID else { return }
            let launch = VoiceLaunch(requestID: requestID)
            do { try channel.write(launch: launch) }
            catch { show(error.localizedDescription); return }
            var url = URLComponents(string: "gusan://voice")!
            url.queryItems = [URLQueryItem(name: "nonce", value: launch.nonce.uuidString)]
            if let bundleID = hostBundleID() { url.queryItems?.append(URLQueryItem(name: "return", value: bundleID)) }
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
        inserted = nil; clearReview(); undoButton.isHidden = true
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
        if let status, status.phase == .failed, let id = status.requestID, id == requestID {
            localMessage = status.message
            if (try? channel.consumeResult(requestID: id)) != nil { channel.post(.command) }
        }
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
        bar.isHidden = label.text == nil && inserted == nil && reviewResult == nil
    }
    private func deliver(_ status: VoiceStatus) {
        guard let id = requestID else { return }
        pending.insertedID = id
        do { try channel.write(keyboard: pending) }
        catch { pending.insertedID = nil; show(error.localizedDescription); return }
        defer {
            do { try channel.consumeResult(requestID: id); channel.post(.command) }
            catch { show(error.localizedDescription) }
        }
        guard !pending.isStale(status: status) else { return }
        guard let result = status.result, !result.isEmpty, let context, let current = currentContext(), let proxy = host?.textDocumentProxy else {
            if let result = status.result { offerResult(result) }; return
        }
        if !pending.canInsert(context: current) {
            offerResult(result); return
        }
        proxy.insertText(result)
        inserted = result; original = context.selected; undoContext = currentContext(); undoButton.isHidden = false
    }
    private func clearReview() { closeResult(); reviewResult = nil; copyButton.isHidden = true; reviewButton.isHidden = true }
    private func offerResult(_ result: String) {
        guard !result.isEmpty else { return }
        reviewResult = result; copyButton.isHidden = false; reviewButton.isHidden = false
        show("Text changed. Review result.")
    }
    @objc private func copyResult() {
        guard let reviewResult else { return }
        UIPasteboard.general.string = reviewResult
        show("Result copied")
    }
    @objc private func openSettings() { if let settingsURL { open(settingsURL) } }
    @objc private func viewResult() {
        guard let result = reviewResult, let host else { return }
        closeResult()
        let panel = UIView(frame: host.view.bounds)
        panel.autoresizingMask = [.flexibleWidth, .flexibleHeight]; panel.backgroundColor = .systemGroupedBackground
        let heading = UILabel(); heading.text = "Voice result"; heading.font = .preferredFont(forTextStyle: .headline)
        heading.adjustsFontForContentSizeCategory = true; heading.numberOfLines = 0
        let text = UITextView(); text.text = result; text.isEditable = false; text.isSelectable = false
        text.font = .preferredFont(forTextStyle: .body); text.adjustsFontForContentSizeCategory = true
        text.backgroundColor = .secondarySystemGroupedBackground; text.layer.cornerRadius = 8
        let actions = UIStackView(); actions.spacing = 8; actions.distribution = .fillEqually
        func button(_ title: String, _ action: Selector) {
            let button = UIButton(type: .system); button.setTitle(title, for: .normal)
            button.addTarget(self, action: action, for: .touchUpInside)
            button.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
            actions.addArrangedSubview(button)
        }
        button("Copy", #selector(copyResult))
        if let expected = currentContext(), let documentID = context?.documentID, !documentID.isEmpty, expected.documentID == documentID, expected.selected.isEmpty {
            reviewContext = expected; button("Insert at cursor", #selector(insertReviewedResult))
        }
        button("Close", #selector(closeResult))
        let stack = UIStackView(arrangedSubviews: [heading, text, actions]); stack.axis = .vertical; stack.spacing = 8
        stack.translatesAutoresizingMaskIntoConstraints = false; panel.addSubview(stack)
        NSLayoutConstraint.activate([stack.topAnchor.constraint(equalTo: panel.topAnchor, constant: 12), stack.bottomAnchor.constraint(equalTo: panel.bottomAnchor, constant: -12), stack.leadingAnchor.constraint(equalTo: panel.leadingAnchor, constant: 12), stack.trailingAnchor.constraint(equalTo: panel.trailingAnchor, constant: -12)])
        resultPanel = panel; reviewHeading = heading; host.view.addSubview(panel)
    }
    @objc private func closeResult() { resultPanel?.removeFromSuperview(); resultPanel = nil; reviewContext = nil; reviewHeading = nil }
    @objc private func insertReviewedResult() {
        guard let result = reviewResult, let expected = reviewContext, currentContext() == expected, let proxy = host?.textDocumentProxy else {
            reviewHeading?.text = "Text changed. Use Copy."; return
        }
        proxy.insertText(result)
        inserted = result; original = ""; undoContext = currentContext()
        clearReview(); undoButton.isHidden = false; show("Done")
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
