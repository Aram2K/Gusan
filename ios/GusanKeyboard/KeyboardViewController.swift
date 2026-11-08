import UIKit
import GusanKit

final class KeyboardViewController: UIInputViewController {
    var onVoice: (() -> Void)?
    private lazy var store = SessionStore(role: .keyboard, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private lazy var settings = SettingsStore(group: store.resolution.chosen, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private lazy var voice = VoiceKeyController(host: self, store: store)
    private lazy var screen = ScreenKeyboardController(host: self, store: store, pasteboardAllowed: { [weak self] in self?.hasFullAccess == true && self?.settings.pasteboardFallback == true })
    private lazy var auth = AuthClient(store: store)
    private lazy var agent = AgentKeyboardController(store: store, auth: auth, host: self)
    private let strip = GusanStrip()
    private let keyArea = TypingKeyboardView()
    private var height: NSLayoutConstraint!
    private var screenTake = false
    private var agentTake = false
    private var firstFrame: Task<ScreenFrame?, Never>?
    private var lastFrame: Task<ScreenFrame?, Never>?
    private var job: Task<Void, Never>?
    private var working = false
    private var agentMessage: String?
    private var collapse: Timer?
    private var screenHint = false
    private var retryTake: (text: String, context: VoiceContext)?
    private var pendingInsert: VoicePendingInsert?
    private var sharedDefaults: UserDefaults { UserDefaults(suiteName: store.resolution.chosen) ?? .standard }
    private var context: VoiceContext {
        VoiceContext(selected: textDocumentProxy.selectedText ?? "", before: textDocumentProxy.documentContextBeforeInput ?? "", after: textDocumentProxy.documentContextAfterInput ?? "", documentID: textDocumentProxy.documentID.uuidString)
    }
    override func viewDidLoad() {
        super.viewDidLoad()
        height = view.heightAnchor.constraint(equalToConstant: 262)
        height.priority = .defaultHigh; height.isActive = true
        [strip, keyArea, agent.card].forEach { view.addSubview($0) }
        strip.mode.addTarget(self, action: #selector(toggleMode), for: .touchUpInside)
        strip.screen.addTarget(self, action: #selector(toggleScreen), for: .touchUpInside)
        strip.mic.addTarget(self, action: #selector(startVoice), for: .touchUpInside)
        strip.cancel.addTarget(self, action: #selector(cancelVoice), for: .touchUpInside)
        strip.action.addTarget(self, action: #selector(actionPressed), for: .touchUpInside)
        strip.status.addTarget(self, action: #selector(statusPressed), for: .touchUpInside)
        onVoice = { [weak self] in self?.voice.toggle() }
        voice.onChange = { [weak self] in self?.update() }
        voice.onStart = { [weak self] in self?.beginTake() }
        voice.onFinish = { [weak self] in self?.finishTake() }
        voice.onTranscript = { [weak self] text, context in self?.process(text, context: context) }
        screen.onChange = { [weak self] in self?.update() }
        screen.onLost = { [weak self] in
            self?.firstFrame?.cancel(); self?.lastFrame?.cancel()
            self?.firstFrame = nil; self?.lastFrame = nil
            self?.update()
        }
        agent.onSurfaceChange = { [weak self] in self?.view.setNeedsLayout(); self?.update() }
        agent.onOpenApp = { [weak self] in self?.openContainingApp($0) }
        agent.onStatus = { [weak self] text, _ in self?.agentMessage = text; self?.update() }
        let swipe = UISwipeGestureRecognizer(target: self, action: #selector(hideAgent))
        swipe.direction = .down; agent.card.addGestureRecognizer(swipe)
        keyArea.globeButton.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        keyArea.onInsert = { [weak self] in self?.insert($0) }
        keyArea.onDelete = { [weak self] in self?.deleteOnce() }
        keyArea.onReturn = { [weak self] in self?.insert("\n") }
        keyArea.onDoubleSpace = { [weak self] in self?.deleteOnce(); self?.insert(". ") }
        update()
    }
    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        settings.refresh()
        pendingInsert = hasFullAccess ? VoicePendingInsert.read(from: sharedDefaults) : nil
        screen.appear(); voice.appear(); agent.appear(); update()
    }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        screen.publishKeyboard(height: Double(view.bounds.height), screenHeight: Double(view.window?.windowScene?.screen.bounds.height ?? UIScreen.main.bounds.height))
    }
    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        keyArea.cancelTouches(); voice.disappear(); screen.disappear(); agent.disappear()
        collapse?.invalidate()
    }
    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let compact = traitCollection.verticalSizeClass == .compact
        height.constant = compact ? 208 : 262
        strip.frame = CGRect(x: 0, y: 0, width: view.bounds.width, height: 45)
        keyArea.frame = CGRect(x: 0, y: 45, width: view.bounds.width, height: max(0, view.bounds.height - 45))
        agent.card.frame = CGRect(x: 0, y: 45, width: view.bounds.width, height: max(0, keyArea.bounds.height - 54))
        view.bringSubviewToFront(agent.card)
    }
    override func textDidChange(_ textInput: UITextInput?) { super.textDidChange(textInput); voice.documentDidChange(); update() }
    override func selectionDidChange(_ textInput: UITextInput?) { super.selectionDidChange(textInput); update() }
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) { super.traitCollectionDidChange(previousTraitCollection); update() }
    private func update() {
        guard isViewLoaded else { return }
        let theme = KeyboardTheme(appearance: textDocumentProxy.keyboardAppearance, traits: traitCollection)
        view.backgroundColor = theme.background
        keyArea.keyClicks = settings.keyClicks; keyArea.doubleSpacePeriod = settings.doubleSpacePeriod
        keyArea.updateContext(before: textDocumentProxy.documentContextBeforeInput, capitalization: settings.autoCapitalization ? textDocumentProxy.autocapitalizationType : UITextAutocapitalizationType.none, returnType: textDocumentProxy.returnKeyType, appearance: textDocumentProxy.keyboardAppearance, needsGlobe: needsInputModeSwitchKey)
        agent.apply(theme)
        let selected = context.selected
        let title = selected.isEmpty ? (settings.keyboardScreenEnabled ? "Near cursor" : "") : "Selected · \(selected.split(whereSeparator: { $0.isWhitespace }).count) words"
        let active = working || [.recording, .transcribing, .thinking].contains(voice.phase)
        let hint = screenHint && !active && voice.message == nil
        let actionTitle = voice.canUndo ? "Undo" : hint ? "Open" : voice.actionTitle == "Undo" ? nil : voice.actionTitle
        strip.update(agent: settings.keyboardMode == .agent, screenOn: settings.keyboardScreenEnabled, live: screen.live && !screen.secureField, context: title, phase: working ? .thinking : voice.phase, level: voice.level, message: working ? voice.message ?? "Working…" : pendingInsert != nil ? "Result ready" : voice.message ?? agentMessage ?? (hint ? "Gusan Screen is off" : nil), action: pendingInsert != nil && !active ? "Insert" : actionTitle, theme: theme, pendingInsert: pendingInsert != nil && !active)
    }
    @objc private func toggleMode() {
        guard !working, ![.recording, .transcribing, .thinking].contains(voice.phase) else { return }
        settings.keyboardMode = settings.keyboardMode == .smart ? .agent : .smart; update()
    }
    @objc private func toggleScreen() { settings.keyboardScreenEnabled.toggle(); screenHint = settings.keyboardScreenEnabled && !screen.live; update() }
    @objc private func startVoice() { onVoice?() }
    @objc private func cancelVoice() {
        if pendingInsert != nil {
            VoicePendingInsert.clear(in: sharedDefaults); pendingInsert = nil; update(); return
        }
        job?.cancel(); firstFrame?.cancel(); lastFrame?.cancel()
        firstFrame = nil; lastFrame = nil; working = false; voice.cancel()
    }
    @objc private func actionPressed() {
        if let pendingInsert {
            let snapshot = context
            voice.insert(JobResult(text: pendingInsert.text, kind: snapshot.selected.isEmpty ? .insert : .replaceSelection), context: snapshot)
            VoicePendingInsert.clear(in: sharedDefaults); self.pendingInsert = nil; update()
        }
        else if voice.actionTitle == "Retry" {
            if let retryTake { process(retryTake.text, context: retryTake.context) }
            else { voice.toggle() }
        }
        else if screenHint && voice.message == nil { openContainingApp(URL(string: "gusan://screen")!) }
        else { voice.performAction() }
    }
    @objc private func statusPressed() { if agent.isActive { agent.show() } }
    @objc private func hideAgent() { agent.hide() }
    private func beginTake() -> VoiceAction? {
        retryTake = nil
        agentTake = settings.keyboardMode == .agent
        screenTake = !agentTake && settings.keyboardScreenEnabled && screen.live && !screen.secureField
        screenHint = settings.keyboardScreenEnabled && !screen.live
        lastFrame = nil; firstFrame = nil
        if screenTake {
            let provider = screen.frames
            firstFrame = Task { try? await provider.frame(timeout: 3) }
        }
        return agentTake || screenTake ? .transcribeOnly : nil
    }
    private func finishTake() {
        guard screenTake, screen.live, !screen.secureField else { return }
        let provider = screen.frames
        lastFrame = Task { try? await provider.frame(timeout: 3) }
    }
    private func process(_ text: String, context: VoiceContext) {
        retryTake = (text, context)
        if agentTake {
            guard !settings.privacyMode else {
                voice.show("Agent needs Gusan Cloud — off in Privacy Mode")
                return
            }
            let intent = AgentIntent.detect(text) ?? AgentIntent(mode: .research, instruction: text)
            agent.start(intent, context: context.selected)
            collapse?.invalidate()
            collapse = Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { [weak self] _ in self?.agent.hide() }
            return
        }
        working = true; voice.show(text)
        let first = firstFrame, last = lastFrame
        job = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { working = false; update() }
            do {
                let start = await first?.value
                let finish = await last?.value
                try Task.checkCancellation()
                guard let session = try await auth.refresh() else { throw GusanError.unauthorized }
                let frames = screen.live && !screen.secureField ? [start, finish].compactMap { $0 } : []
                let kind: JobResult.Kind = context.selected.isEmpty ? .insert : .replaceSelection
                let task: GusanJob
                if frames.isEmpty {
                    task = SmartActionJob(text: context.selected.isEmpty ? context.paragraph.before + context.paragraph.after : context.selected, action: .custom(instruction: text), accessToken: session.accessToken, kind: kind)
                } else {
                    task = ScreenInstructionJob(instruction: text, text: context.paragraph.before + context.paragraph.after, selected: context.selected, accessToken: session.accessToken, kind: kind, capturedFrames: frames)
                }
                let result = try await JobRunner().result(of: task)
                try Task.checkCancellation()
                voice.insert(result, context: context)
            } catch is CancellationError { voice.show("Cancelled") }
            catch GusanError.unauthorized { voice.show("Sign in to use Gusan", action: "Sign in", url: "gusan://signin") }
            catch { voice.show(error.localizedDescription, action: "Retry") }
        }
    }
    private func insert(_ text: String) {
        voice.insertedText(text, hasSelection: !context.selected.isEmpty)
        textDocumentProxy.insertText(text); update()
    }
    private func deleteOnce() {
        voice.deletedText(hasSelection: !context.selected.isEmpty)
        textDocumentProxy.deleteBackward(); update()
    }
    func openContainingApp(_ url: URL) {
        let selector = NSSelectorFromString("openURL:options:completionHandler:")
        var responder: UIResponder? = self
        while let current = responder {
            if current is UIApplication, current.responds(to: selector) {
                typealias Completion = @convention(block) (Bool) -> Void
                typealias Open = @convention(c) (AnyObject, Selector, NSURL, NSDictionary, Completion) -> Void
                let open = unsafeBitCast(current.method(for: selector), to: Open.self)
                let completion: Completion = { [weak self] opened in
                    if !opened { self?.voice.show("Open the Gusan app to sign in.") }
                }
                open(current, selector, url as NSURL, [:] as NSDictionary, completion)
                return
            }
            responder = current.next
        }
        voice.show("Open the Gusan app to sign in.")
    }
}
