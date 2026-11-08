import UIKit
import GusanKit

final class VoiceWaveform: UIView {
    private var animation: CADisplayLink?
    private var idleAnimating = false
    var levels: [Float] = [] { didSet { setNeedsDisplay() } }
    override init(frame: CGRect) { super.init(frame: frame); isOpaque = false; backgroundColor = .clear }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override var intrinsicContentSize: CGSize { CGSize(width: 65, height: 24) }
    override func didMoveToWindow() { super.didMoveToWindow(); updateAnimation() }
    @objc private func animate() { setNeedsDisplay() }
    private func updateAnimation() {
        if idleAnimating && window != nil {
            if animation == nil { animation = CADisplayLink(target: self, selector: #selector(animate)); animation?.add(to: .main, forMode: .common) }
        } else { animation?.invalidate(); animation = nil }
    }
    override func draw(_ rect: CGRect) {
        KeyboardTheme.blue.setStroke()
        let path = UIBezierPath(); path.lineWidth = 2
        let drawn = idleAnimating ? (0..<30).map { Float(0.12 + abs(sin(Double($0) * 0.48 + Date.timeIntervalSinceReferenceDate * 4)) * 0.4) } : levels
        for (index, level) in drawn.enumerated() {
            let x = CGFloat(index) * rect.width / 30
            let height = max(2, CGFloat(level) * rect.height)
            path.move(to: CGPoint(x: x, y: (rect.height - height) / 2)); path.addLine(to: CGPoint(x: x, y: (rect.height + height) / 2))
        }
        path.stroke()
    }
    func update(level: Float, animated: Bool) {
        idleAnimating = level <= 0 && animated && !UIAccessibility.isReduceMotionEnabled
        updateAnimation()
        if level > 0 { levels = Array((levels + [level]).suffix(30)) }
        else {
            let time = animated && !UIAccessibility.isReduceMotionEnabled ? Date.timeIntervalSinceReferenceDate * 4 : 0
            levels = (0..<30).map { Float(0.12 + abs(sin(Double($0) * 0.48 + time)) * 0.4) }
        }
    }
}

final class VoiceKeyController: NSObject {
    var onChange: (() -> Void)?
    var onStart: (() -> VoiceAction?)?
    var onFinish: (() -> Void)?
    var onTranscript: ((String, VoiceContext) -> Void)?
    private weak var host: UIInputViewController?
    private let store: SessionStore
    private let channel: VoiceChannel
    private var observer: DarwinObserver?
    private var timer: Timer?
    private var undoTimer: Timer?
    private struct Presentation: Equatable {
        let phase: VoicePhase
        let level: Float
        let message: String?
        let action: String?
        let canUndo: Bool
    }
    private var presentation: Presentation?
    private var pending = KeyboardVoiceState()
    private var deadline = KeyboardVoiceDeadline()
    var hasPendingRequest: Bool { pending.requestID != nil && pending.requestID != pending.insertedID && pending.requestID != cancelledID }
    private var status: VoiceStatus?
    private var cancelledID: UUID?
    private var timedOutID: UUID?
    private var action: VoiceAction?
    private var visible = false
    private var loading = false
    private var undoRecord: ReplacementUndo?
    private var undoSnapshot: VoiceContext?
    private var inserted: String?
    private var original = ""
    private var restoreUTF16 = 0
    private var undoExpires = Date.distantPast
    private var preview = false
    private(set) var phase = VoicePhase.off
    private(set) var level: Float = 0
    private(set) var message: String?
    private(set) var actionTitle: String?
    private var actionURL: URL?
    var tracksUndo: Bool { inserted != nil || undoRecord != nil }
    var canUndo: Bool {
        guard undoRecord != nil, Date() < undoExpires, let current = currentContext() else { return false }
        return canUndo(in: current)
    }
    func canUndo(in context: VoiceContext) -> Bool {
        Date() < undoExpires && undoRecord?.step(context: context) != nil
    }
    private func notifyChanged() {
        let value = Presentation(phase: phase, level: level, message: message, action: actionTitle, canUndo: canUndo)
        guard value != presentation else { return }
        presentation = value; onChange?()
    }
    private func updateFallbackTimer() {
        let waiting = pending.requestID != nil && pending.insertedID != pending.requestID && pending.requestID != cancelledID && phase != .failed
        guard visible && (waiting || phase != .off) else { timer?.invalidate(); timer = nil; return }
        guard timer == nil else { return }
        timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in self?.load() }
    }
    init(host: UIInputViewController, store: SessionStore) {
        self.host = host; self.store = store; channel = store.makeVoiceChannel()
        super.init()
    }
    func appear() {
        visible = true
        #if DEBUG
        preview = UserDefaults(suiteName: store.resolution.chosen)?.bool(forKey: "debugVoicePreview") == true
        #endif
        if !preview, let saved = channel.readKeyboardState() { pending = saved; action = saved.action }
        observer = channel.observe(.status) { [weak self] in self?.load() }
        updateFallbackTimer()
        load()
    }
    func disappear() { visible = false; observer = nil; timer?.invalidate(); timer = nil }
    private func load() {
        guard visible, !loading else { return }
        if preview { refresh(); return }
        loading = true
        Task { @MainActor [weak self] in
            guard let self else { return }
            let value = await channel.readStatusSnapshot()
            loading = false
            guard visible else { return }
            status = value; refresh()
        }
    }
    private func currentContext() -> VoiceContext? {
        guard let proxy = host?.textDocumentProxy else { return nil }
        return VoiceContext(selected: proxy.selectedText ?? "", before: proxy.documentContextBeforeInput ?? "", after: proxy.documentContextAfterInput ?? "", documentID: proxy.documentID?.uuidString)
    }
    func toggle() {
        if preview {
            if phase == .recording { onFinish?(); status?.phase = .thinking }
            else if phase == .thinking { status?.phase = .done; status?.result = "This is a polished voice preview."; status?.resultKind = .insert }
            else { start(); status = VoiceStatus(phase: .recording, requestID: pending.requestID) }
            refresh(); return
        }
        guard let host else { return }
        switch KeyboardVoice.tap(status: status, access: store.sessionState(fullAccess: host.hasFullAccess)) {
        case .error(let error):
            show(error.localizedDescription, action: error == .fullAccessRequired ? "Turn on Full Access" : "Sign in", url: error == .fullAccessRequired ? "gusan://open-settings" : "gusan://signin")
        case .start: start()
        case .openApp:
            start()
            guard let id = pending.requestID else { return }
            let launch = VoiceLaunch(requestID: id)
            do { try channel.write(launch: launch) } catch { show(error.localizedDescription); return }
            let defaults = UserDefaults(suiteName: store.resolution.chosen) ?? .standard
            if !defaults.bool(forKey: "voiceHopExplained") {
                defaults.set(true, forKey: "voiceHopExplained")
                show("iOS lets only the Gusan app hear the microphone. Add the Gusan control to Control Center to start from anywhere.")
            }
            var url = URLComponents(string: "gusan://voice")!
            url.queryItems = [URLQueryItem(name: "nonce", value: launch.nonce.uuidString)]
            if let bundle = hostBundleID() { url.queryItems?.append(URLQueryItem(name: "return", value: bundle)) }
            open(url.url!)
        case .stop: onFinish?(); send(.stop)
        case .cancel: cancel()
        }
    }
    private func start() {
        cancelledID = nil; message = nil; actionTitle = nil; undoRecord = nil; inserted = nil
        pending = KeyboardVoiceState(requestID: UUID(), context: currentContext())
        deadline = KeyboardVoiceDeadline()
        action = onStart?()
        pending.action = action
        updateFallbackTimer()
        if !preview { do { try channel.write(keyboard: pending) } catch { show(error.localizedDescription); return }; send(.start) }
    }
    private func send(_ kind: VoiceCommand.Kind) {
        guard let id = pending.requestID else { return }
        do {
            try channel.write(command: VoiceCommand(kind: kind, requestID: id, context: kind == .start ? pending.context : nil, action: kind == .start ? action : nil))
            channel.post(.command)
        } catch { show(error.localizedDescription) }
    }
    func cancel() {
        cancelledID = pending.requestID
        if !preview { send(.cancel) } else { status = nil }
        phase = .off; updateFallbackTimer(); show("Cancelled")
    }
    func show(_ text: String, action: String? = nil, url: String? = nil) {
        message = text; actionTitle = action; actionURL = url.flatMap(URL.init(string:)); notifyChanged()
    }
    func performAction() {
        if actionTitle == "Undo" { if canUndo { undo() }; return }
        if let actionURL { open(actionURL) }
    }
    private func refresh() {
        status = KeyboardVoiceFeedback.statusForDisplay(status, timedOutRequestID: timedOutID)
        if !preview, let expired = deadline.expiry(pending: pending, status: status) {
            timedOutID = pending.requestID
            cancelledID = pending.requestID
            if let id = pending.requestID { channel.discardRequest(id) }
            if expired == .processing { send(.cancel) }
            pending = KeyboardVoiceState(); phase = .off
            show(expired == .acknowledgement ? "Gusan didn't start — tap mic to retry" : "Timed out — Retry", action: expired == .processing ? "Retry" : nil)
            updateFallbackTimer(); return
        }
        documentDidChange()
        if Date() >= undoExpires, inserted != nil { inserted = nil; undoRecord = nil; message = nil }
        let active = preview || status?.isAlive() == true || KeyboardVoiceFeedback.displaysFailure(status, pending: pending)
        phase = status?.requestID == cancelledID && cancelledID != nil ? .off : active ? status?.phase ?? .off : .off
        level = preview ? Float(0.2 + abs(sin(Date.timeIntervalSinceReferenceDate * 3)) * 0.6) : status?.level ?? 0
        if let status, status.phase == .done, status.requestID == pending.requestID, pending.insertedID != pending.requestID, status.requestID != cancelledID { deliver(status) }
        if phase == .recording { message = nil; actionTitle = nil }
        else if phase == .thinking || phase == .transcribing {
            let transcript = channel.readTranscript()
            message = transcript?.requestID == pending.requestID ? transcript?.text : "Working…"
        } else if phase == .failed { message = status?.message; actionTitle = "Retry" }
        updateFallbackTimer(); notifyChanged()
    }
    private func deliver(_ status: VoiceStatus) {
        guard let id = pending.requestID, !pending.isStale(status: status), let result = status.result, !result.isEmpty, let context = pending.context else { return }
        pending.insertedID = id
        if !preview {
            try? channel.write(keyboard: pending)
            try? channel.consumeResult(requestID: id); channel.post(.command)
        }
        if action == .transcribeOnly { onTranscript?(result, context); return }
        insert(JobResult(text: result, kind: status.resultKind ?? (context.selected.isEmpty ? .insert : .replaceSelection)), context: context)
    }
    func insert(_ result: JobResult, context: VoiceContext) {
        guard let current = currentContext(), let proxy = host?.textDocumentProxy else { return }
        let edit = VoiceEdit.plan(result: result, context: context)
        let destination = VoiceInsertionDestination.resolve(snapshot: context, current: current)
        if destination == .planned, let edit {
            if edit.advanceUTF16 != 0 { proxy.adjustTextPosition(byCharacterOffset: edit.advanceUTF16) }
            for _ in 0..<edit.deleteCount { proxy.deleteBackward() }
            original = edit.original; restoreUTF16 = edit.restoreUTF16
        } else if destination == .cursor {
            original = ""; restoreUTF16 = 0
        } else {
            if host?.hasFullAccess == true { UIPasteboard.general.string = result.text; show("Couldn't insert here · Copied") }
            else { show("Couldn't insert here") }
            return
        }
        undoSnapshot = currentContext(); inserted = result.text
        proxy.insertText(result.text)
        undoRecord = nil; undoExpires = Date().addingTimeInterval(30)
        undoTimer?.invalidate()
        undoTimer = Timer.scheduledTimer(withTimeInterval: 30, repeats: false) { [weak self] _ in
            guard let self else { return }
            inserted = nil; undoRecord = nil; undoSnapshot = nil
            if actionTitle == "Undo" { message = nil; actionTitle = nil }
            notifyChanged()
        }
        documentDidChange(); show("Inserted", action: "Undo")
    }
    func documentDidChange() {
        guard undoRecord == nil, let inserted, let snapshot = undoSnapshot, let current = currentContext(),
              VoiceEdit.containsInsertedResult(inserted, context: current, snapshot: snapshot) else { return }
        undoRecord = ReplacementUndo(original: original, replacement: inserted, snapshot: snapshot, restoreUTF16: restoreUTF16)
        undoSnapshot = nil
    }
    func insertedText(_ text: String, hasSelection: Bool) {
        documentDidChange()
        if hasSelection || undoRecord?.append(text) == false { undoRecord = nil; undoSnapshot = nil }
    }
    func deletedText(hasSelection: Bool) {
        documentDidChange()
        if hasSelection || undoRecord?.deleteBackward() == false { undoRecord = nil; undoSnapshot = nil }
    }
    private func undo() {
        guard canUndo, let record = undoRecord, let current = currentContext(), let step = record.step(context: current), let proxy = host?.textDocumentProxy else { show("Text changed. Undo unavailable"); return }
        if step.advanceUTF16 != 0 { proxy.adjustTextPosition(byCharacterOffset: step.advanceUTF16) }
        for _ in 0..<step.deleteCount { proxy.deleteBackward() }
        proxy.insertText(step.text)
        if step.restoreUTF16 != 0 { proxy.adjustTextPosition(byCharacterOffset: step.restoreUTF16) }
        undoRecord = nil; inserted = nil; undoSnapshot = nil; show("Undone")
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
