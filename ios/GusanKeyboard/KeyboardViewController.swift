import UIKit
import GusanKit

final class KeyboardViewController: UIInputViewController {
    var onVoice: (() -> Void)?
    private lazy var store = SessionStore(role: .keyboard, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private lazy var settings = SettingsStore(group: store.resolution.chosen, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private lazy var voice = VoiceKeyController(host: self, store: store)
    private lazy var screen = ScreenKeyboardController(host: self, store: store, pasteboardAllowed: { [weak self] in self?.hasFullAccess == true && self?.preferences.pasteboardFallback == true })
    private lazy var auth = AuthClient(store: store)
    private lazy var agent = AgentKeyboardController(store: store, auth: auth, host: self)
    private var preferences = KeyboardSettingsSnapshot()
    private var updateScheduled = false
    private var appliedDark: Bool?
    private var appeared = false
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
    private var retryTake: (text: String, context: VoiceContext)?
    private var pendingInsert: VoicePendingInsert?
    private var sharedDefaults: UserDefaults { UserDefaults(suiteName: store.resolution.chosen) ?? .standard }
    private var context: VoiceContext {
        VoiceContext(selected: textDocumentProxy.selectedText ?? "", before: textDocumentProxy.documentContextBeforeInput ?? "", after: textDocumentProxy.documentContextAfterInput ?? "", documentID: textDocumentProxy.documentID.uuidString)
    }
    override func viewDidLoad() {
        super.viewDidLoad()
        inputView?.allowsSelfSizing = true
        height = view.heightAnchor.constraint(equalToConstant: 262)
        height.priority = UILayoutPriority(999)
        height.identifier = "Gusan.keyboardHeight"
        height.isActive = true
        [strip, keyArea, agent.card].forEach { view.addSubview($0) }
        strip.translatesAutoresizingMaskIntoConstraints = false
        NSLayoutConstraint.activate([
            strip.leadingAnchor.constraint(equalTo: view.leadingAnchor),
            strip.trailingAnchor.constraint(equalTo: view.trailingAnchor),
            strip.topAnchor.constraint(equalTo: view.topAnchor),
            strip.heightAnchor.constraint(equalToConstant: 45)
        ])
        strip.mode.addTarget(self, action: #selector(toggleMode), for: .touchUpInside)
        strip.screen.addTarget(self, action: #selector(toggleScreen), for: .touchUpInside)
        strip.mic.addTarget(self, action: #selector(startVoice), for: .touchUpInside)
        strip.cancel.addTarget(self, action: #selector(cancelVoice), for: .touchUpInside)
        strip.action.addTarget(self, action: #selector(actionPressed), for: .touchUpInside)
        strip.status.addTarget(self, action: #selector(statusPressed), for: .touchUpInside)
        onVoice = { [weak self] in self?.voice.toggle() }
        voice.onChange = { [weak self] in
            guard let self else { return }
            if voice.phase == .failed || !voice.hasPendingRequest {
                firstFrame?.cancel(); lastFrame?.cancel(); firstFrame = nil; lastFrame = nil
            }
            scheduleUpdate()
        }
        voice.onStart = { [weak self] in self?.beginTake() }
        voice.onFinish = { [weak self] in self?.finishTake() }
        voice.onTranscript = { [weak self] text, context in self?.process(text, context: context) }
        screen.onChange = { [weak self] in self?.scheduleUpdate() }
        screen.onLost = { [weak self] in
            self?.firstFrame?.cancel(); self?.lastFrame?.cancel()
            self?.firstFrame = nil; self?.lastFrame = nil
            if let self, screenTake && (voice.hasPendingRequest || working) {
                screen.reportCapture(KeyboardScreenPlan.unavailableMessage)
            }
            self?.scheduleUpdate()
        }
        agent.onSurfaceChange = { [weak self] in self?.view.setNeedsLayout(); self?.scheduleUpdate() }
        agent.onOpenApp = { [weak self] in self?.openContainingApp($0) }
        agent.onStatus = { [weak self] text, _ in self?.agentMessage = text; self?.scheduleUpdate() }
        let swipe = UISwipeGestureRecognizer(target: self, action: #selector(hideAgent))
        swipe.cancelsTouchesInView = false; swipe.delaysTouchesBegan = false
        swipe.direction = .down; agent.card.addGestureRecognizer(swipe)
        keyArea.globeButton.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        keyArea.onInsert = { [weak self] in self?.insert($0) }
        keyArea.onDelete = { [weak self] in self?.deleteOnce() }
        keyArea.onReturn = { [weak self] in self?.insert("\n") }
        keyArea.onDoubleSpace = { [weak self] in self?.deleteOnce(); self?.insert(". ") }
        scheduleUpdate()
    }
    override func viewWillAppear(_ animated: Bool) {
        super.viewWillAppear(animated)
        preferences = KeyboardSettingsSnapshot(defaults: sharedDefaults)
        agentTake = preferences.keyboardMode == .agent
        scheduleUpdate()
    }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated)
        appeared = true
        var ancestor: UIView? = view
        while let current = ancestor {
            current.gestureRecognizers?.forEach { $0.delaysTouchesBegan = false }
            ancestor = current.superview
        }
        view.window?.gestureRecognizers?.forEach { $0.delaysTouchesBegan = false }
        DispatchQueue.main.async { [weak self] in
            guard let self, appeared else { return }
            settings.refresh(); preferences = KeyboardSettingsSnapshot(defaults: sharedDefaults)
            pendingInsert = hasFullAccess ? VoicePendingInsert.read(from: sharedDefaults, account: store.read().flatMap { $0.userID ?? $0.email }) : nil
            screen.appear(); voice.appear(); agent.appear(); scheduleUpdate()
        }
        screen.publishKeyboard(height: Double(view.bounds.height), screenHeight: Double(view.window?.windowScene?.screen.bounds.height ?? UIScreen.main.bounds.height))
    }
    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated)
        appeared = false
        keyArea.cancelTouches(); voice.disappear(); screen.disappear(); agent.disappear()
        firstFrame?.cancel(); lastFrame?.cancel(); firstFrame = nil; lastFrame = nil
        collapse?.invalidate()
    }
    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let compact = traitCollection.verticalSizeClass == .compact
        height.constant = compact ? 208 : 262
        keyArea.frame = CGRect(x: 0, y: 45, width: view.bounds.width, height: height.constant - 45)
        agent.card.frame = CGRect(x: 0, y: 45, width: view.bounds.width, height: max(0, keyArea.bounds.height - 54))
        view.bringSubviewToFront(agent.card)
    }
    override func textDidChange(_ textInput: UITextInput?) { super.textDidChange(textInput); if voice.tracksUndo { voice.documentDidChange() }; scheduleUpdate() }
    override func selectionDidChange(_ textInput: UITextInput?) { super.selectionDidChange(textInput); scheduleUpdate() }
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection)
        if previousTraitCollection?.verticalSizeClass != traitCollection.verticalSizeClass { view.setNeedsLayout() }
        scheduleUpdate()
    }
    private func scheduleUpdate() {
        guard !updateScheduled else { return }
        updateScheduled = true
        DispatchQueue.main.async { [weak self] in
            guard let self else { return }
            updateScheduled = false; update()
        }
    }
    private func update() {
        guard isViewLoaded else { return }
        let proxy = textDocumentProxy
        let snapshot = VoiceContext(selected: proxy.selectedText ?? "", before: proxy.documentContextBeforeInput ?? "", after: proxy.documentContextAfterInput ?? "", documentID: proxy.documentID.uuidString)
        let appearance = proxy.keyboardAppearance
        let capitalization = proxy.autocapitalizationType
        let returnType = proxy.returnKeyType
        let secure = proxy.isSecureTextEntry == true
        let theme = KeyboardTheme(appearance: appearance, traits: traitCollection)
        view.backgroundColor = theme.background
        keyArea.keyClicks = preferences.keyClicks; keyArea.doubleSpacePeriod = preferences.doubleSpacePeriod
        keyArea.updateContext(before: snapshot.before, capitalization: preferences.autoCapitalization ? capitalization : UITextAutocapitalizationType.none, returnType: returnType, appearance: appearance, needsGlobe: needsInputModeSwitchKey)
        if appliedDark != theme.dark { appliedDark = theme.dark; agent.apply(theme) }
        let selected = snapshot.selected
        let title = selected.isEmpty ? (preferences.keyboardScreenEnabled ? "Near cursor" : "") : "Selected · \(selected.split(whereSeparator: { $0.isWhitespace }).count) words"
        let active = working || [.recording, .transcribing, .thinking].contains(voice.phase)
        let actionTitle = voice.canUndo(in: snapshot) ? "Undo" : voice.actionTitle == "Undo" ? nil : voice.actionTitle
        // A task waiting on the user outranks stale strip text, but never an active take or a Smart result/error.
        let attention = active || voice.message != nil ? nil : agent.task.flatMap { AgentKeyboardAttention.resolve($0) }
        let agentStatus = !working && pendingInsert == nil && (attention != nil || (voice.message == nil && agentMessage != nil))
        let message = screen.feedback ?? (!hasFullAccess && !active && voice.message == nil ? "Allow Full Access" : working ? voice.message ?? "Working…" : pendingInsert != nil ? "Result ready" : attention?.text ?? voice.message ?? agentMessage)
        strip.update(agent: preferences.keyboardMode == .agent, screenOn: preferences.keyboardScreenEnabled, live: screen.live && !secure, context: title, phase: working ? .thinking : voice.phase, level: voice.level, message: message, action: pendingInsert != nil && !active ? "Insert" : actionTitle, theme: theme, pendingInsert: pendingInsert != nil && !active, agentStatus: agentStatus)
    }
    @objc private func toggleMode() {
        guard !working, ![.recording, .transcribing, .thinking].contains(voice.phase) else { return }
        settings.keyboardMode = preferences.keyboardMode == .smart ? .agent : .smart
        preferences = KeyboardSettingsSnapshot(defaults: sharedDefaults); scheduleUpdate()
    }
    @objc private func toggleScreen() {
        guard hasFullAccess else { openContainingApp(URL(string: "gusan://open-settings")!); return }
        if preferences.keyboardScreenEnabled && !screen.live { openContainingApp(URL(string: "gusan://screen")!); return }
        settings.keyboardScreenEnabled = !preferences.keyboardScreenEnabled
        preferences = KeyboardSettingsSnapshot(defaults: sharedDefaults); scheduleUpdate()
        if preferences.keyboardScreenEnabled && !screen.live { openContainingApp(URL(string: "gusan://screen")!) }
    }
    @objc private func startVoice() { onVoice?() }
    @objc private func cancelVoice() {
        if pendingInsert != nil {
            VoicePendingInsert.clear(in: sharedDefaults); pendingInsert = nil; scheduleUpdate(); return
        }
        job?.cancel(); firstFrame?.cancel(); lastFrame?.cancel()
        firstFrame = nil; lastFrame = nil; working = false; voice.cancel()
    }
    @objc private func actionPressed() {
        if !hasFullAccess { openContainingApp(URL(string: "gusan://open-settings")!); return }
        if let pendingInsert {
            guard hasFullAccess, VoicePendingInsert.read(from: sharedDefaults, account: store.read().flatMap { $0.userID ?? $0.email }) == pendingInsert else {
                self.pendingInsert = nil; scheduleUpdate(); return
            }
            let snapshot = context
            voice.insert(JobResult(text: pendingInsert.text, kind: snapshot.selected.isEmpty ? .insert : .replaceSelection), context: snapshot)
            VoicePendingInsert.clear(in: sharedDefaults); self.pendingInsert = nil; scheduleUpdate()
        }
        else if voice.actionTitle == "Retry" {
            if let retryTake { process(retryTake.text, context: retryTake.context) }
            else { voice.toggle() }
        }
        else { voice.performAction() }
    }
    @objc private func statusPressed() {
        if !hasFullAccess { openContainingApp(URL(string: "gusan://open-settings")!); return }
        if let task = agent.task, let attention = AgentKeyboardAttention.resolve(task) {
            openContainingApp(attention.url)
        } else if agent.isActive { agent.show() }
    }
    @objc private func hideAgent() { agent.hide() }
    private func beginTake() -> VoiceAction? {
        retryTake = nil
        agentTake = preferences.keyboardMode == .agent
        screenTake = !agentTake && KeyboardScreenPlan.shouldRequest(enabled: preferences.keyboardScreenEnabled, live: screen.live, secure: screen.secureField, fullAccess: hasFullAccess)
        lastFrame = nil; firstFrame = nil
        if KeyboardScreenPlan.shouldRequest(enabled: screenTake, live: screen.live, secure: screen.secureField, fullAccess: hasFullAccess) {
            let provider = screen.frames(phase: .start)
            firstFrame = Task { try? await provider.frame(timeout: 2) }
        }
        return agentTake || screenTake ? .transcribeOnly : nil
    }
    private func finishTake() {
        guard screenTake else { return }
        let first = firstFrame
        lastFrame = Task { @MainActor [weak self] in
            _ = await first?.value
            guard let self, !Task.isCancelled,
                  KeyboardScreenPlan.shouldRequest(enabled: screenTake, live: screen.live, secure: screen.secureField, fullAccess: hasFullAccess) else { return nil }
            return try? await screen.frames(phase: .finish).frame(timeout: 2)
        }
    }
    private func process(_ text: String, context: VoiceContext) {
        let first = firstFrame, last = lastFrame
        firstFrame = nil; lastFrame = nil
        retryTake = (text, context)
        if agentTake {
            let intent = AgentIntent.detect(text) ?? AgentIntent(mode: .research, instruction: text)
            agent.start(intent, context: context.selected)
            collapse?.invalidate()
            collapse = Timer.scheduledTimer(withTimeInterval: 3, repeats: false) { [weak self] _ in self?.agent.hide() }
            return
        }
        working = true; voice.show(text)
        job = Task { @MainActor [weak self] in
            guard let self else { return }
            defer { working = false; scheduleUpdate() }
            do {
                let start = await first?.value
                let finish = await last?.value
                try Task.checkCancellation()
                guard hasFullAccess else { throw VoiceError.fullAccessRequired }
                let plan = KeyboardScreenPlan(enabled: screenTake || preferences.keyboardScreenEnabled, live: screen.live, secure: screen.secureField, fullAccess: hasFullAccess, start: start, finish: finish)
                screen.reportCapture(plan.message)
                let frames = plan.frames
                guard let session = try await auth.refresh() else { throw GusanError.unauthorized }
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
        if voice.tracksUndo { voice.insertedText(text, hasSelection: textDocumentProxy.selectedText?.isEmpty == false) }
        textDocumentProxy.insertText(text); scheduleUpdate()
    }
    private func deleteOnce() {
        if voice.tracksUndo { voice.deletedText(hasSelection: textDocumentProxy.selectedText?.isEmpty == false) }
        textDocumentProxy.deleteBackward(); scheduleUpdate()
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
