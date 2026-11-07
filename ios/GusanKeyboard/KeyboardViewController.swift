import UIKit
import GusanKit

final class KeyboardViewController: UIInputViewController {
    var onVoice: (() -> Void)?
    private struct Context: Equatable {
        let document: UUID
        let selected: String
        let before: String
        let after: String
        var text: String { selected.isEmpty ? before + after : selected }
        var summary: String {
            if text.isEmpty { return "No text: Ask Gusan writes it" }
            let count = text.split(whereSeparator: { $0.isWhitespace }).count
            return "\(selected.isEmpty ? "Around cursor" : "Selected") · \(count) \(count == 1 ? "word" : "words")"
        }
    }
    private enum Surface { case typing, panel, instruction, explanation }
    private lazy var store = SessionStore(role: .keyboard, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private lazy var voice = VoiceKeyController(host: self, store: store)
    private lazy var auth = AuthClient(store: store)
    private lazy var settings = SettingsStore(group: store.resolution.chosen, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private let toolbar = GusanToolbar()
    private let panel = GusanPanel()
    private let instructionHeader = InstructionHeader()
    private let keyArea = TypingKeyboardView()
    private let explanation = UILabel()
    private let explanationScroll = UIScrollView()
    private var surface: Surface = .typing
    private var instruction = ""
    private var instructionContext: Context?
    private var message: String?
    private var detail: String?
    private var accessory: String?
    private var busy = false
    private var original: String?
    private var replacement: String?
    private var undoContext: Context?
    private var task: Task<Void, Never>?
    private var height: NSLayoutConstraint!
    private var resultTimer: Timer?
    private var retryAction: SmartAction?
    private var retryContext: Context?
    private var connected = false

    override func viewDidLoad() {
        super.viewDidLoad()
        height = view.heightAnchor.constraint(equalToConstant: 260)
        height.priority = .defaultHigh; height.isActive = true
        [toolbar, panel, instructionHeader, keyArea, explanationScroll].forEach { view.addSubview($0) }
        explanationScroll.addSubview(explanation)
        explanation.numberOfLines = 0; explanation.font = .systemFont(ofSize: 15)
        toolbar.logo.addTarget(self, action: #selector(togglePanel), for: .touchUpInside)
        toolbar.logo.addGestureRecognizer(UILongPressGestureRecognizer(target: self, action: #selector(defaultAction(_:))))
        toolbar.mic.addTarget(self, action: #selector(startVoice), for: .touchUpInside)
        onVoice = { [weak self] in self?.voice.toggle() }
        view.addSubview(voice.bar)
        voice.bar.backgroundColor = .secondarySystemBackground; voice.bar.layer.cornerRadius = 8
        toolbar.accessory.addTarget(self, action: #selector(accessoryPressed), for: .touchUpInside)
        toolbar.message.addTarget(self, action: #selector(expandMessage), for: .touchUpInside)
        panel.onClose = { [weak self] in self?.showTyping() }
        panel.onAsk = { [weak self] in self?.beginInstruction() }
        panel.onAction = { [weak self] in guard let self else { return }; self.impact(); self.run($0, context: self.currentContext) }
        instructionHeader.onSuggestion = { [weak self] in self?.setInstruction($0) }
        instructionHeader.onRun = { [weak self] in self?.runInstruction() }
        instructionHeader.onCancel = { [weak self] in self?.cancelInstruction() }
        keyArea.globeButton.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        keyArea.onInsert = { [weak self] in self?.insert($0) }
        keyArea.onDelete = { [weak self] in self?.deleteOnce() }
        keyArea.onReturn = { [weak self] in self?.returnPressed() }
        keyArea.onDoubleSpace = { [weak self] in self?.doubleSpace() }
        updateSurface()
    }
    private var currentContext: Context {
        Context(document: textDocumentProxy.documentIdentifier, selected: textDocumentProxy.selectedText ?? "", before: textDocumentProxy.documentContextBeforeInput ?? "", after: textDocumentProxy.documentContextAfterInput ?? "")
    }
    override func viewWillAppear(_ animated: Bool) { super.viewWillAppear(animated); updateSurface(); voice.appear() }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated); connected = true
        _ = store.read(); _ = store.diagnostics(); settings.refresh(); updateContext()
    }
    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let width = view.bounds.width
        toolbar.frame = CGRect(x: 0, y: 0, width: width, height: 44)
        voice.bar.frame = CGRect(x: 50, y: 5, width: max(0, width - 100), height: 34)
        let extra: CGFloat = surface == .instruction ? 74 : 0
        instructionHeader.frame = CGRect(x: 0, y: 44, width: width, height: extra)
        keyArea.frame = CGRect(x: 0, y: 44 + extra, width: width, height: max(0, view.bounds.height - 44 - extra))
        panel.frame = CGRect(x: 0, y: 44, width: width, height: max(0, view.bounds.height - 44))
        explanationScroll.frame = panel.frame
        let explanationHeight = max(panel.bounds.height - 24, explanation.sizeThatFits(CGSize(width: width - 36, height: .greatestFiniteMagnitude)).height)
        explanation.frame = CGRect(x: 18, y: 12, width: width - 36, height: explanationHeight)
        explanationScroll.contentSize = CGSize(width: width, height: explanationHeight + 24)
        updateContext()
    }
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection); updateContext()
    }
    override func textDidChange(_ textInput: UITextInput?) { super.textDidChange(textInput); updateContext() }
    override func selectionDidChange(_ textInput: UITextInput?) { super.selectionDidChange(textInput); updateContext() }
    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated); task?.cancel(); keyArea.cancelTouches()
        voice.disappear()
    }
    private func updateContext() {
        guard connected else { return }
        let editing = surface == .instruction
        keyArea.returnTitle = editing ? "Run" : nil
        keyArea.keyClicks = settings.keyClicks; keyArea.doubleSpacePeriod = settings.doubleSpacePeriod
        keyArea.updateContext(before: editing ? instruction : textDocumentProxy.documentContextBeforeInput, capitalization: settings.autoCapitalization ? (editing ? .sentences : textDocumentProxy.autocapitalizationType) : UITextAutocapitalizationType.none, returnType: editing ? .go : textDocumentProxy.returnKeyType, appearance: textDocumentProxy.keyboardAppearance, needsGlobe: needsInputModeSwitchKey)
        let theme = KeyboardTheme(appearance: textDocumentProxy.keyboardAppearance, traits: traitCollection)
        view.backgroundColor = theme.background; toolbar.apply(theme)
        let context = instructionContext ?? currentContext
        panel.update(theme: theme, target: settings.translateTarget, defaultAction: settings.keyboardAction.action, context: context.text)
        instructionHeader.update(text: instruction, theme: theme)
        explanation.textColor = theme.foreground
        toolbar.message.setTitle(message ?? context.summary, for: .normal)
        toolbar.accessory.setTitle(accessory, for: .normal); toolbar.accessory.isHidden = accessory == nil
        toolbar.accessory.accessibilityHint = accessory == "Sign in" ? "Opens the Gusan app" : "Activates the status action"
        toolbar.mic.isEnabled = !busy; toolbar.logo.isEnabled = !busy
        toolbar.setNeedsLayout()
    }
    private func updateSurface() {
        keyArea.cancelTouches()
        panel.isHidden = surface != .panel; explanationScroll.isHidden = surface != .explanation
        instructionHeader.isHidden = surface != .instruction
        keyArea.isHidden = surface == .panel || surface == .explanation
        keyArea.inputEnabled = true
        height.constant = 260
        updateContext(); view.setNeedsLayout()
    }
    private func showTyping() { instructionContext = nil; surface = .typing; updateSurface() }
    private func impact() { if settings.haptics { UIImpactFeedbackGenerator(style: .light).impactOccurred() } }
    private func notify(_ type: UINotificationFeedbackGenerator.FeedbackType) { if settings.haptics { UINotificationFeedbackGenerator().notificationOccurred(type) } }
    @objc private func defaultAction(_ gesture: UILongPressGestureRecognizer) {
        guard gesture.state == .began, !busy else { return }
        impact()
        run(settings.keyboardAction.action, context: currentContext)
    }
    @objc private func togglePanel() {
        guard !busy else { return }
        impact()
        if surface == .instruction { cancelInstruction(); return }
        surface = surface == .panel ? .typing : .panel
        message = nil; detail = nil; accessory = nil; updateSurface()
        let destination: UIView = surface == .panel ? panel : keyArea
        destination.alpha = 0
        destination.transform = UIAccessibility.isReduceMotionEnabled ? .identity : CGAffineTransform(translationX: 0, y: 8)
        UIView.animate(withDuration: UIAccessibility.isReduceMotionEnabled ? 0.15 : 0.35, delay: 0, usingSpringWithDamping: 0.85, initialSpringVelocity: 0, options: [.beginFromCurrentState]) {
            destination.alpha = 1; destination.transform = .identity
        }
    }
    @objc func startVoice() {
        guard !busy else { return }
        impact()
        if let onVoice { onVoice() } else { showMessage("Voice is coming soon") }
    }
    private func showMessage(_ text: String, detail: String? = nil, accessory: String? = nil) {
        voice.dismiss()
        message = text; self.detail = detail; self.accessory = accessory; updateContext()
    }
    @objc private func expandMessage() {
        guard !busy, let detail else { return }
        explanation.text = detail + "\n\nTap the Gusan logo to return to actions."
        explanationScroll.setContentOffset(.zero, animated: false)
        surface = .explanation; updateSurface()
    }
    @objc private func accessoryPressed() {
        switch accessory {
        case "Sign in": openContainingApp(URL(string: "gusan://signin?from=keyboard")!)
        case "Undo": undo()
        case "Cancel": task?.cancel()
        case "How to enable", "Details": expandMessage()
        case "See plans": openContainingApp(URL(string: "https://gusan.aibuben.xyz")!)
        case "Retry": if let retryAction, let retryContext { run(retryAction, context: retryContext) }
        default: break
        }
    }
    private func beginInstruction() {
        guard !busy else { return }
        if instructionContext != currentContext { instructionContext = currentContext; instruction = "" }
        message = nil; accessory = nil; detail = nil
        surface = .instruction; updateSurface()
    }
    func setInstruction(_ text: String) { instruction = text; updateContext() }
    private func cancelInstruction() {
        instruction = ""; instructionContext = nil; message = nil; accessory = nil; detail = nil; showTyping()
    }
    private func runInstruction() {
        guard let context = instructionContext else { return }
        run(.custom(instruction: instruction), context: context)
    }
    private func run(_ action: SmartAction, context: Context) {
        voice.dismiss()
        guard !busy else { return }
        guard hasFullAccess else {
            if surface != .instruction { showTyping() }
            notify(.error)
            showMessage("Gusan needs Full Access to reach Gusan Cloud", detail: "Allow Full Access for Gusan in Settings › General › Keyboard › Keyboards › Gusan › Allow Full Access. Text is sent to Gusan Cloud only when you run an action.\n\n" + store.diagnostics().summary, accessory: "How to enable")
            return
        }
        guard store.read() != nil, store.diagnostics().isShared else {
            notify(.error)
            if surface != .instruction { showTyping() }
            showMessage("Sign in to use Gusan", detail: store.diagnostics().summary, accessory: "Sign in"); return
        }
        #if DEBUG
        if store.read()?.accessToken == "gusan-debug-no-network" {
            showMessage("DEBUG session available", detail: store.diagnostics().summary, accessory: "Details"); return
        }
        #endif
        do { _ = try Prompts.messages(text: context.text, action: action, target: settings.translateTarget) }
        catch { showMessage(error.localizedDescription, detail: error.localizedDescription); return }
        guard currentContext == context else { showMessage("Text changed. Select it and try again."); return }
        busy = true; resultTimer?.invalidate(); invalidateUndo(); toolbar.spinner.startAnimating()
        retryAction = action; retryContext = context
        let progress: String
        switch action { case .rephrase: progress = "Rephrasing…"; case .fixGrammar: progress = "Fixing grammar…"; default: progress = "Working…" }
        showMessage(progress, accessory: "Cancel"); updateSurface()
        task = Task { @MainActor in
            defer { busy = false; toolbar.spinner.stopAnimating(); keyArea.inputEnabled = true; updateContext() }
            do {
                guard let session = try await auth.refresh() else { throw GusanError.unauthorized }
                let client = SmartActionClient()
                let result: String
                do { result = try await client.run(text: context.text, action: action, accessToken: session.accessToken, target: settings.translateTarget) }
                catch GusanError.unauthorized {
                    guard let refreshed = try await auth.refresh(force: true) else { throw GusanError.unauthorized }
                    result = try await client.run(text: context.text, action: action, accessToken: refreshed.accessToken, target: settings.translateTarget)
                }
                try Task.checkCancellation()
                guard currentContext == context else { showMessage("Text changed. Select it and try again."); return }
                guard !result.isEmpty else { throw GusanError.invalidResponse }
                if context.selected.isEmpty {
                    textDocumentProxy.adjustTextPosition(byCharacterOffset: context.after.utf16.count)
                    for _ in context.text { textDocumentProxy.deleteBackward() }
                }
                textDocumentProxy.insertText(result)
                original = context.text; replacement = result; undoContext = currentContext
                instructionContext = nil; instruction = ""; showTyping()
                showMessage("Replaced", accessory: "Undo")
                notify(.success)
                UIAccessibility.post(notification: .announcement, argument: "Replaced. Undo available.")
                resultTimer = Timer.scheduledTimer(withTimeInterval: 8, repeats: false) { [weak self] _ in
                    guard let self, self.message == "Replaced" else { return }
                    self.message = nil; self.accessory = nil; self.updateContext()
                }
            } catch is CancellationError { showMessage("Cancelled") }
            catch GusanError.unauthorized { showMessage("Sign in to use Gusan", accessory: "Sign in") }
            catch GusanError.proRequired {
                notify(.error)
                showMessage("Gusan Pro is needed for AI actions", accessory: "See plans")
            }
            catch {
                notify(.error)
                let retry = [GusanError.network, .timeout, .failed, .serverNotConfigured, .invalidResponse].contains(error as? GusanError ?? .emptyInput)
                let title = error as? GusanError == .network ? "You're offline. Tap for details." : "Action failed. Tap for details."
                showMessage(title, detail: error.localizedDescription, accessory: retry ? "Retry" : nil)
            }
        }
    }
    private func undo() {
        guard !busy, let original, let replacement else { return }
        guard currentContext == undoContext, currentContext.selected.isEmpty else {
            invalidateUndo(); showMessage("Text changed. Undo is no longer available."); return
        }
        for _ in replacement { textDocumentProxy.deleteBackward() }
        textDocumentProxy.insertText(original)
        invalidateUndo(); showMessage("Undone")
    }
    private func invalidateUndo() { original = nil; replacement = nil; undoContext = nil; if accessory == "Undo" { accessory = nil } }
    private func cancelForInput() -> Bool {
        guard busy else { return false }
        task?.cancel(); showMessage("Cancelled")
        return true
    }
    private func insert(_ text: String) {
        let cancelled = cancelForInput()
        if surface == .instruction { instruction += text }
        else { invalidateUndo(); if !cancelled { message = nil; detail = nil; accessory = nil }; textDocumentProxy.insertText(text) }
        updateContext()
    }
    private func returnPressed() { if surface == .instruction { runInstruction() } else { insert("\n") } }
    private func deleteOnce() {
        let cancelled = cancelForInput()
        if surface == .instruction { if !instruction.isEmpty { instruction.removeLast() } }
        else { invalidateUndo(); if !cancelled { message = nil; detail = nil; accessory = nil }; textDocumentProxy.deleteBackward() }
        updateContext()
    }
    private func doubleSpace() {
        _ = cancelForInput()
        if surface == .instruction { if instruction.last == " " { instruction.removeLast() }; instruction += ". " }
        else { invalidateUndo(); textDocumentProxy.deleteBackward(); textDocumentProxy.insertText(". ") }
        updateContext()
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
                    if !opened { self?.showMessage("Open the Gusan app to sign in.") }
                }
                open(current, selector, url as NSURL, [:] as NSDictionary, completion)
                return
            }
            responder = current.next
        }
        showMessage("Open the Gusan app to sign in.")
    }
}
