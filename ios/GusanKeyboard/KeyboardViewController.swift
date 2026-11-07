import UIKit
import GusanKit

final class KeyboardViewController: UIInputViewController {
    var onVoice: (() -> Void)?
    private struct Context: Equatable {
        let document: UUID
        let selected: String
        let before: String
        let after: String
        var paragraph: VoiceContext { voiceContext.paragraph }
        var text: String { selected.isEmpty ? paragraph.before + paragraph.after : selected }
        var voiceContext: VoiceContext { VoiceContext(selected: selected, before: before, after: after, documentID: document.uuidString) }
        var summary: String {
            if text.isEmpty { return "No text: Ask Gusan writes it" }
            let count = text.split(whereSeparator: { $0.isWhitespace }).count
            return "\(selected.isEmpty ? "Near cursor" : "Selected") · \(count) \(count == 1 ? "word" : "words")"
        }
    }
    private enum Surface { case typing, panel, instruction, explanation }
    private lazy var store = SessionStore(role: .keyboard, pasteboardAccessAllowed: { [weak self] in self?.hasFullAccess == true })
    private lazy var voice = VoiceKeyController(host: self, store: store, micKey: toolbar.mic)
    private lazy var screen = ScreenKeyboardController(host: self, store: store, pasteboardAllowed: { [weak self] in self?.hasFullAccess == true && self?.settings.pasteboardFallback == true })
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
    private var snapshotReturnSurface: Surface?
    private var busy = false
    private var original: String?
    private var replacement: String?
    private var undoSnapshot: VoiceContext?
    private var undoRecord: ReplacementUndo?
    private var undoIsRedo = false
    private var task: Task<Void, Never>?
    private var height: NSLayoutConstraint!
    private var retryAction: SmartAction?
    private var retryContext: Context?
    private var retryScreen = false
    private var connected = false
    private var fontCategory: UIContentSizeCategory?
    private var accessState = SessionState(signedIn: false, fullAccess: false, shareable: false)

    override func viewDidLoad() {
        super.viewDidLoad()
        if #available(iOS 17.0, *) {
            registerForTraitChanges([UITraitUserInterfaceStyle.self, UITraitPreferredContentSizeCategory.self]) { (controller: KeyboardViewController, _: UITraitCollection) in
                controller.updateContext()
            }
        }
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
        view.addSubview(voice.panel)
        voice.onPanelVisibility = { [weak self] visible in
            guard let self else { return }
            self.keyArea.cancelTouches()
            let changes = {
                [self.toolbar, self.voice.bar, self.panel, self.instructionHeader, self.keyArea, self.explanationScroll].forEach { $0.alpha = visible ? 0 : 1 }
                self.voice.panel.isHidden = !visible
            }
            if UIAccessibility.isReduceMotionEnabled { changes() }
            else { UIView.transition(with: self.view, duration: 0.2, options: [.transitionCrossDissolve, .allowAnimatedContent], animations: changes) }
        }
        voice.bar.backgroundColor = .secondarySystemBackground; voice.bar.layer.cornerRadius = 8
        toolbar.accessory.addTarget(self, action: #selector(accessoryPressed), for: .touchUpInside)
        toolbar.addSubview(screen.chip)
        screen.onChange = { [weak self] in self?.screenChanged() }
        screen.onLost = { [weak self] in self?.screenLost() }
        screen.onStopped = { [weak self] in
            self?.showMessage("Gusan Screen stopped")
            UIAccessibility.post(notification: .announcement, argument: "Gusan Screen stopped.")
        }
        screen.onOffer = { [weak self] in
            self?.showMessage("Start Gusan Screen in the Gusan app to ask about your screen", detail: "Gusan Screen shares a picture of your screen only when you ask with the Screen chip. Start it from Home › Gusan Screen in the Gusan app, or from Control Center's screen recording control.", accessory: "Open")
        }
        toolbar.message.addTarget(self, action: #selector(expandMessage), for: .touchUpInside)
        toolbar.context.addTarget(self, action: #selector(expandContext), for: .touchUpInside)
        panel.onClose = { [weak self] in self?.showTyping() }
        panel.onSnapshot = { [weak self] in self?.expandContext() }
        panel.onAsk = { [weak self] in self?.beginInstruction() }
        panel.onAction = { [weak self] in guard let self else { return }; self.run($0, context: self.currentContext) }
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
        Context(document: textDocumentProxy.documentID, selected: textDocumentProxy.selectedText ?? "", before: textDocumentProxy.documentContextBeforeInput ?? "", after: textDocumentProxy.documentContextAfterInput ?? "")
    }
    override func viewWillAppear(_ animated: Bool) { super.viewWillAppear(animated); updateSurface(); voice.appear(); screen.appear() }
    override func viewDidAppear(_ animated: Bool) {
        super.viewDidAppear(animated); connected = true
        accessState = store.sessionState(fullAccess: hasFullAccess)
        settings.refresh(); updateContext()
        screen.publishKeyboard(height: Double(keyboardHeight), screenHeight: Double(view.window?.windowScene?.screen.bounds.height ?? UIScreen.main.bounds.height))
    }
    override func viewWillLayoutSubviews() {
        super.viewWillLayoutSubviews()
        let width = view.bounds.width
        let barHeight = toolbar.preferredHeight
        toolbar.frame = CGRect(x: 0, y: 0, width: width, height: barHeight)
        toolbar.layoutIfNeeded(); layoutScreenChip()
        voice.bar.frame = CGRect(x: 50, y: toolbar.contextHeight, width: max(0, width - 100), height: toolbar.controlsHeight)
        voice.panel.frame = view.bounds
        let extra: CGFloat = surface == .instruction && !compactLayout ? instructionHeader.preferredHeight : 0
        instructionHeader.frame = CGRect(x: 0, y: compactLayout ? toolbar.contextHeight : barHeight, width: width, height: instructionHeader.preferredHeight)
        keyArea.frame = CGRect(x: 0, y: barHeight + extra, width: width, height: max(0, view.bounds.height - barHeight - extra))
        panel.frame = CGRect(x: 0, y: barHeight, width: width, height: max(0, view.bounds.height - barHeight))
        explanationScroll.frame = panel.frame
        let explanationHeight = max(panel.bounds.height - 24, explanation.sizeThatFits(CGSize(width: width - 36, height: .greatestFiniteMagnitude)).height)
        explanation.frame = CGRect(x: 18, y: 12, width: width - 36, height: explanationHeight)
        explanationScroll.contentSize = CGSize(width: width, height: explanationHeight + 24)
        updateContext()
    }
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection); updateContext()
    }
    override func textDidChange(_ textInput: UITextInput?) { super.textDidChange(textInput); voice.documentDidChange(); captureUndo(); updateContext() }
    override func selectionDidChange(_ textInput: UITextInput?) { super.selectionDidChange(textInput); updateContext() }
    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated); task?.cancel(); keyArea.cancelTouches()
        voice.disappear(); screen.disappear()
    }
    private func updateContext() {
        guard connected else { return }
        updateTypography()
        instructionHeader.compact = compactLayout
        panel.compact = compactLayout
        let compactInstruction = compactLayout && surface == .instruction
        [toolbar.logo, toolbar.mic, toolbar.message, toolbar.accessory].forEach { $0.isHidden = compactInstruction }
        toolbar.spinner.isHidden = compactInstruction || !toolbar.spinner.isAnimating
        voice.bar.alpha = compactInstruction || !voice.panel.isHidden ? 0 : 1
        height.constant = keyboardHeight
        let editing = surface == .instruction
        keyArea.returnTitle = editing ? "Run" : nil
        keyArea.keyClicks = settings.keyClicks; keyArea.doubleSpacePeriod = settings.doubleSpacePeriod
        keyArea.updateContext(before: editing ? instruction : textDocumentProxy.documentContextBeforeInput, capitalization: settings.autoCapitalization ? (editing ? .sentences : textDocumentProxy.autocapitalizationType) : UITextAutocapitalizationType.none, returnType: editing ? .go : textDocumentProxy.returnKeyType, appearance: textDocumentProxy.keyboardAppearance, needsGlobe: needsInputModeSwitchKey)
        let theme = KeyboardTheme(appearance: textDocumentProxy.keyboardAppearance, traits: traitCollection)
        applyTheme(theme)
        let context = instructionContext ?? currentContext
        panel.update(theme: theme, target: settings.translateTarget, defaultAction: settings.keyboardAction.action, context: context.text)
        let screenInstruction = editing && screen.armed
        instructionHeader.update(text: instruction, theme: theme, busy: busy, placeholder: screenInstruction ? "Ask about your screen…" : "Tell Gusan what to do…", suggestions: screenInstruction ? ScreenPrompts.suggestions : InstructionHeader.defaultSuggestions)
        explanation.textColor = theme.foreground
        updateContextTitle(screen.summary(context.summary), theme: theme)
        toolbar.context.accessibilityValue = (screen.live ? "Gusan Screen is live. " : "") + context.summary
        toolbar.message.setTitle(snapshotReturnSurface != nil ? "Text snapshot" : message ?? "", for: .normal)
        let statusAction = snapshotReturnSurface != nil ? "Back" : accessory ?? (undoRecord == nil ? nil : undoIsRedo ? "Redo" : "Undo")
        toolbar.accessory.setTitle(statusAction, for: .normal); toolbar.accessory.isHidden = statusAction == nil
        if compactInstruction { toolbar.accessory.isHidden = true }
        toolbar.accessory.isEnabled = (statusAction != "Undo" && statusAction != "Redo") || undoRecord?.step(context: currentContext.voiceContext) != nil
        toolbar.accessory.accessibilityHint = accessory == "Sign in" ? "Opens the Gusan app" : "Activates the status action"
        toolbar.mic.isEnabled = !busy; toolbar.logo.isEnabled = !busy
        screen.update(theme: theme, busy: busy, traits: traitCollection)
        toolbar.logo.accessibilityValue = accessState.actionError?.localizedDescription
        toolbar.setNeedsLayout(); layoutScreenChip()
    }
    /// While Gusan Screen is live the context line starts with a red dot, like the system indicator.
    private func updateContextTitle(_ title: String, theme: KeyboardTheme) {
        let font = toolbar.context.titleLabel?.font ?? .systemFont(ofSize: 12, weight: .medium)
        guard screen.live else { toolbar.context.setAttributedTitle(nil, for: .normal); toolbar.context.setTitle(title, for: .normal); return }
        let text = NSMutableAttributedString(string: "● ", attributes: [.foregroundColor: UIColor.systemRed, .font: font])
        text.append(NSAttributedString(string: title, attributes: [.foregroundColor: theme.foreground, .font: font]))
        toolbar.context.setAttributedTitle(text, for: .normal)
    }
    /// The Screen chip sits left of the mic, as tall as the mic, and takes its width from the message area.
    private func layoutScreenChip() {
        let chip = screen.chip
        guard !chip.isHidden else { return }
        let size = chip.intrinsicContentSize
        let height = max(size.height, toolbar.mic.frame.height)
        let right = toolbar.accessory.isHidden ? toolbar.mic.frame.minX : toolbar.accessory.frame.minX
        chip.frame = CGRect(x: right - size.width - 6, y: toolbar.contextHeight + (toolbar.controlsHeight - height) / 2, width: size.width, height: height)
        toolbar.message.frame.size.width = max(0, chip.frame.minX - toolbar.message.frame.minX - 6)
    }
    private func screenChanged() {
        if screen.armed {
            if surface != .instruction { beginInstruction() }
            if screen.needsArmReminder { screen.markArmReminderShown(); showMessage("Your next Run sends a picture of this screen") }
            UIAccessibility.post(notification: .layoutChanged, argument: instructionHeader)
            UIAccessibility.post(notification: .announcement, argument: "Screen on. Ask about your screen; Run sends a picture of it.")
        } else {
            if surface == .instruction, instruction.isEmpty, instructionContext != nil, !busy { cancelInstruction() }
            if screen.available { UIAccessibility.post(notification: .announcement, argument: "Screen off.") }
        }
        updateContext()
    }
    /// The broadcast ended mid-instruction: never let the question fall through to a plain text action.
    private func screenLost() {
        guard surface == .instruction, !busy else { return }
        cancelInstruction()
        showMessage("Gusan Screen stopped. Start it again to ask about your screen.")
        UIAccessibility.post(notification: .announcement, argument: "Gusan Screen stopped. Your question was cancelled.")
    }
    private func applyTheme(_ theme: KeyboardTheme) {
        view.backgroundColor = theme.background
        keyArea.applyTheme(theme)
        toolbar.apply(theme)
        voice.applyTheme(theme)
    }
    private func updateSurface() {
        keyArea.cancelTouches()
        panel.isHidden = surface != .panel; explanationScroll.isHidden = surface != .explanation
        instructionHeader.isHidden = surface != .instruction
        keyArea.isHidden = surface == .panel || surface == .explanation
        keyArea.inputEnabled = true
        height.constant = keyboardHeight
        updateContext(); view.setNeedsLayout()
    }
    private func showTyping(cancelPending: Bool = true) {
        if cancelPending { task?.cancel() }
        snapshotReturnSurface = nil; instructionContext = nil; surface = .typing; updateSurface()
    }
    private var compactLayout: Bool {
        traitCollection.verticalSizeClass == .compact || view.window?.windowScene?.interfaceOrientation.isLandscape == true
    }
    private var keyboardHeight: CGFloat {
        let requested = toolbar.preferredHeight + panel.preferredHeight + (surface == .instruction && !compactLayout ? instructionHeader.preferredHeight : 0)
        let screen = view.window?.windowScene?.screen.bounds ?? UIScreen.main.bounds
        return compactLayout ? min(requested, floor(min(screen.width, screen.height) * 0.55)) : requested
    }
    private func updateTypography() {
        guard fontCategory != traitCollection.preferredContentSizeCategory else { return }
        fontCategory = traitCollection.preferredContentSizeCategory
        toolbar.updateTypography(traitCollection)
        panel.updateTypography(traitCollection)
        instructionHeader.updateTypography(traitCollection)
        voice.updateTypography(traitCollection)
        explanation.font = keyboardFont(15, style: .body, maximum: 22, traits: traitCollection)
        height.constant = keyboardHeight
        view.setNeedsLayout()
    }
    @objc private func defaultAction(_ gesture: UILongPressGestureRecognizer) {
        guard gesture.state == .began, !busy else { return }
        run(settings.keyboardAction.action, context: currentContext)
    }
    @objc private func togglePanel() {
        guard !busy else { return }
        accessState = store.sessionState(fullAccess: hasFullAccess)
        if surface == .instruction { cancelInstruction(); return }
        surface = surface == .panel ? .typing : .panel
        snapshotReturnSurface = nil
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
        invalidateUndo()
        if let onVoice { onVoice() } else { showMessage("Tap the mic to open the Gusan app and start a voice session the first time") }
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
    @objc private func expandContext() {
        let context = instructionContext ?? currentContext
        if surface != .explanation { snapshotReturnSurface = surface }
        explanation.text = context.summary + "\n\n" + (context.text.isEmpty ? "No existing text will be replaced." : context.text)
        explanationScroll.setContentOffset(.zero, animated: false)
        surface = .explanation
        updateSurface()
    }
    @objc private func accessoryPressed() {
        if let previous = snapshotReturnSurface {
            snapshotReturnSurface = nil; surface = previous; updateSurface(); return
        }
        switch accessory ?? (undoRecord == nil ? nil : undoIsRedo ? "Redo" : "Undo") {
        case "Sign in": openContainingApp(URL(string: "gusan://signin?from=keyboard")!)
        case "Undo", "Redo": undo()
        case "Cancel": task?.cancel()
        case "How to enable", "Details": expandMessage()
        case "See plans": openContainingApp(URL(string: "https://gusan.aibuben.xyz")!)
        case "Retry": if let retryAction, let retryContext { run(retryAction, context: retryContext, useScreen: retryScreen) }
        case "Open": openContainingApp(URL(string: "gusan://screen")!)
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
        screen.armed = false
    }
    private func runInstruction() {
        guard let context = instructionContext else { return }
        run(.custom(instruction: instruction), context: context, useScreen: screen.armed)
    }
    private func run(_ action: SmartAction, context: Context, useScreen: Bool = false) {
        voice.dismiss()
        guard !busy else { return }
        let access = previewActions ? SessionState(signedIn: true, fullAccess: true, shareable: true) : store.sessionState(fullAccess: hasFullAccess)
        accessState = access
        if access.actionError == .fullAccessRequired {
            if surface != .instruction { showTyping() }
            showMessage("Gusan needs Full Access to reach Gusan Cloud", detail: "Allow Full Access for Gusan in Settings › General › Keyboard › Keyboards › Gusan › Allow Full Access. Text is sent to Gusan Cloud only when you run an action.\n\n" + store.diagnostics().summary, accessory: "How to enable")
            return
        }
        if let error = access.actionError {
            if surface != .instruction { showTyping() }
            showMessage(error == .notSignedIn ? "Sign in to use Gusan" : "Gusan can't read shared sign-in", detail: store.diagnostics().summary, accessory: error == .notSignedIn ? "Sign in" : "Details"); return
        }
        #if DEBUG
        if !previewActions, store.read()?.accessToken == "gusan-debug-no-network" {
            showMessage("DEBUG session available", detail: store.diagnostics().summary, accessory: "Details"); return
        }
        #endif
        do { _ = try Prompts.messages(text: context.text, action: action, target: settings.translateTarget) }
        catch { showMessage(error.localizedDescription, detail: error.localizedDescription); return }
        guard currentContext == context else { showMessage("Text changed. Select it and try again."); return }
        busy = true; invalidateUndo(); toolbar.spinner.startAnimating()
        retryAction = action; retryContext = context; retryScreen = useScreen
        let progress: String
        switch action { case .rephrase: progress = "Rephrasing…"; case .fixGrammar: progress = "Fixing grammar…"; default: progress = useScreen ? "Looking at your screen…" : "Working…" }
        showMessage(progress, accessory: "Cancel"); updateSurface()
        task = Task { @MainActor in
            defer { busy = false; toolbar.spinner.stopAnimating(); keyArea.inputEnabled = true; updateContext() }
            do {
                let outcome = try await actionResult(action, context: context, useScreen: useScreen)
                let result = outcome.text
                try Task.checkCancellation()
                guard currentContext == context else { showMessage("Text changed. Select it and try again."); return }
                guard !result.isEmpty else { throw GusanError.invalidResponse }
                if outcome.kind == .replaceContext {
                    textDocumentProxy.adjustTextPosition(byCharacterOffset: context.paragraph.after.utf16.count)
                    for _ in context.text { textDocumentProxy.deleteBackward() }
                }
                undoSnapshot = currentContext.voiceContext
                textDocumentProxy.insertText(result)
                original = outcome.kind == .insert ? "" : context.text; replacement = result; captureUndo(restoreUTF16: outcome.kind == .replaceContext ? context.paragraph.after.utf16.count : 0)
                instructionContext = nil; instruction = ""; showTyping(cancelPending: false); screen.armed = false
                showMessage("Replaced", accessory: "Undo")
                UIAccessibility.post(notification: .announcement, argument: "Replaced. Undo available.")
            } catch is CancellationError { showMessage("Cancelled") }
            catch GusanError.unauthorized { showMessage("Sign in to use Gusan", accessory: "Sign in") }
            catch GusanError.proRequired {
                showMessage("Gusan Pro is needed for AI actions", accessory: "See plans")
            }
            catch let error as ScreenError {
                showMessage(error == .noFrame ? "No picture from Gusan Screen. Tap for details." : "Gusan Screen isn't available. Tap for details.", detail: error.localizedDescription + "\n\n" + screen.diagnostics, accessory: error == .noFrame ? "Retry" : nil)
            }
            catch {
                let retry = [GusanError.network, .timeout, .failed, .serverNotConfigured, .invalidResponse].contains(error as? GusanError ?? .emptyInput)
                let title = error as? GusanError == .network ? "You're offline. Tap for details." : "Action failed. Tap for details."
                showMessage(title, detail: error.localizedDescription, accessory: retry ? "Retry" : nil)
            }
        }
    }
    private var previewActions: Bool {
        #if DEBUG
        UserDefaults(suiteName: store.resolution.chosen)?.bool(forKey: "debugActionPreview") == true
        #else
        false
        #endif
    }
    /// Screen answers go in at the cursor (or replace the selection); they never rewrite the surrounding text.
    private func actionResult(_ action: SmartAction, context: Context, useScreen: Bool = false) async throws -> JobResult {
        #if DEBUG
        if previewActions { return try await InstructionPreviewJob().result() }
        #endif
        guard let session = try await auth.refresh() else { throw GusanError.unauthorized }
        let kind: JobResult.Kind = context.selected.isEmpty ? (context.text.isEmpty ? .insert : .replaceContext) : .replaceSelection
        func job(_ token: String) throws -> GusanJob {
            guard useScreen, case .custom(let instruction) = action else { return SmartActionJob(text: context.text, action: action, accessToken: token, target: settings.translateTarget, kind: kind) }
            return try screen.job(instruction: instruction, text: context.text, selected: context.selected, accessToken: token, kind: kind)
        }
        let progress: @Sendable (String) async -> Void = { [weak self] message in await MainActor.run { self?.showMessage(message, accessory: "Cancel") } }
        do { return try await JobRunner().result(of: try job(session.accessToken), progress: progress) }
        catch GusanError.unauthorized {
            guard let refreshed = try await auth.refresh(force: true) else { throw GusanError.unauthorized }
            return try await JobRunner().result(of: try job(refreshed.accessToken), progress: progress)
        }
    }
    private func undo() {
        guard !busy, let record = undoRecord else { return }
        guard let step = record.step(context: currentContext.voiceContext) else {
            invalidateUndo(); showMessage("Text changed. Undo is no longer available."); return
        }
        if step.advanceUTF16 != 0 { textDocumentProxy.adjustTextPosition(byCharacterOffset: step.advanceUTF16) }
        for _ in 0..<step.deleteCount { textDocumentProxy.deleteBackward() }
        if !step.text.isEmpty { textDocumentProxy.insertText(step.text) }
        if step.restoreUTF16 != 0 { textDocumentProxy.adjustTextPosition(byCharacterOffset: step.restoreUTF16) }
        undoRecord = record.inverted(); undoIsRedo.toggle(); accessory = nil
        showMessage(undoIsRedo ? "Undone" : "Redone")
    }
    private var undoRestoreUTF16 = 0
    private func captureUndo(restoreUTF16: Int? = nil) {
        if let restoreUTF16 { undoRestoreUTF16 = restoreUTF16 }
        guard undoRecord == nil, let original, let replacement, let snapshot = undoSnapshot,
              VoiceEdit.containsInsertedResult(replacement, context: currentContext.voiceContext, snapshot: snapshot) else { return }
        undoRecord = ReplacementUndo(original: original, replacement: replacement, snapshot: snapshot, restoreUTF16: undoRestoreUTF16)
        undoSnapshot = nil
    }
    private func invalidateUndo() { original = nil; replacement = nil; undoSnapshot = nil; undoRecord = nil; undoIsRedo = false; if accessory == "Undo" || accessory == "Redo" { accessory = nil } }
    private func cancelForInput() -> Bool {
        guard busy else { return false }
        task?.cancel(); showMessage("Cancelled")
        return true
    }
    private func insert(_ text: String) {
        let cancelled = cancelForInput()
        if surface == .instruction { instruction += text }
        else {
            captureUndo(); undoSnapshot = nil
            if !currentContext.selected.isEmpty || undoRecord?.append(text) == false { invalidateUndo() }
            voice.insertedText(text, hasSelection: !currentContext.selected.isEmpty)
            if !cancelled { message = nil; detail = nil; accessory = nil }; textDocumentProxy.insertText(text)
        }
        updateContext()
    }
    private func returnPressed() { if surface == .instruction { runInstruction() } else { insert("\n") } }
    private func deleteOnce() {
        let cancelled = cancelForInput()
        if surface == .instruction { if !instruction.isEmpty { instruction.removeLast() } }
        else {
            captureUndo(); undoSnapshot = nil
            if !currentContext.selected.isEmpty || undoRecord?.deleteBackward() == false { invalidateUndo() }
            voice.deletedText(hasSelection: !currentContext.selected.isEmpty)
            if !cancelled { message = nil; detail = nil; accessory = nil }; textDocumentProxy.deleteBackward()
        }
        updateContext()
    }
    private func doubleSpace() {
        _ = cancelForInput()
        if surface == .instruction { if instruction.last == " " { instruction.removeLast() }; instruction += ". " }
        else { deleteOnce(); insert(". ") }
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

#if DEBUG
private struct InstructionPreviewJob: GusanJob {
    func run() -> AsyncThrowingStream<JobEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    try await Task.sleep(nanoseconds: 10_000_000_000)
                    continuation.yield(.result(JobResult(text: "DEBUG action completed", kind: .replaceContext)))
                    continuation.finish()
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
#endif
