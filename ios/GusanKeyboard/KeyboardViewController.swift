import UIKit
import GusanKit

final class KeyboardViewController: UIInputViewController {
    private let store = SessionStore()
    private lazy var auth = AuthClient(store: store)
    private lazy var settings = SettingsStore(group: store.resolution.chosen)
    private let status = UILabel()
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let undoButton = UIButton(type: .system)
    private let keyArea = TypingKeyboardView()
    private var actionButtons: [UIButton] = []
    private var translateButton: UIButton?
    private var busy = false
    private var original: String?
    private var replacement: String?
    private var undoDocument: UUID?
    private var undoBefore: String?
    private var undoAfter: String?
    private var task: Task<Void, Never>?

    override func viewDidLoad() {
        super.viewDidLoad()
        let height = view.heightAnchor.constraint(equalToConstant: 260)
        height.priority = .defaultHigh; height.isActive = true
        let stack = UIStackView()
        stack.axis = .vertical; stack.spacing = 0; stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: view.leadingAnchor), stack.trailingAnchor.constraint(equalTo: view.trailingAnchor), stack.topAnchor.constraint(equalTo: view.topAnchor), stack.bottomAnchor.constraint(equalTo: view.bottomAnchor)])
        let bar = UIStackView()
        bar.axis = .vertical; bar.spacing = 0
        bar.heightAnchor.constraint(equalToConstant: 44).isActive = true
        let scroll = UIScrollView()
        scroll.showsHorizontalScrollIndicator = false
        let top = UIStackView()
        top.spacing = 6; top.translatesAutoresizingMaskIntoConstraints = false
        scroll.addSubview(top)
        NSLayoutConstraint.activate([
            top.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor, constant: 6),
            top.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor, constant: -6),
            top.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor),
            top.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor),
            top.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor)
        ])
        let logo = button("", action: #selector(defaultAction))
        logo.setImage(UIImage(named: "GusanLogo")?.withRenderingMode(.alwaysOriginal), for: .normal)
        logo.imageView?.contentMode = .scaleAspectFit
        logo.widthAnchor.constraint(equalToConstant: 30).isActive = true
        logo.accessibilityLabel = "Run default Gusan action"
        top.addArrangedSubview(logo); actionButtons.append(logo)
        for (title, mode) in [("Smart", SmartActionMode.smartAuto), ("Email", .writeEmail), ("To-do", .makeTodoList)] {
            top.addArrangedSubview(actionButton(title, mode: mode))
        }
        let translate = actionButton("Translate", mode: .translate)
        translateButton = translate
        top.addArrangedSubview(translate)
        top.addArrangedSubview(actionButton("Summarize", mode: .summarize))
        bar.addArrangedSubview(scroll)
        let stateRow = UIStackView(arrangedSubviews: [spinner, status, undoButton])
        stateRow.spacing = 8
        status.numberOfLines = 1; status.font = .systemFont(ofSize: 10); status.textColor = .secondaryLabel
        status.adjustsFontSizeToFitWidth = true; status.minimumScaleFactor = 0.6
        status.text = "Select text or type, then choose an action."
        undoButton.setTitle("Undo", for: .normal); undoButton.addTarget(self, action: #selector(undo), for: .touchUpInside)
        undoButton.isHidden = true
        undoButton.titleLabel?.font = .systemFont(ofSize: 11, weight: .semibold)
        undoButton.setContentHuggingPriority(.required, for: .horizontal)
        stateRow.heightAnchor.constraint(equalToConstant: 16).isActive = true
        stateRow.isLayoutMarginsRelativeArrangement = true
        stateRow.layoutMargins = UIEdgeInsets(top: 0, left: 8, bottom: 0, right: 8)
        bar.addArrangedSubview(stateRow)
        stack.addArrangedSubview(bar)
        stack.addArrangedSubview(keyArea)
        keyArea.globeButton.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        keyArea.onInsert = { [weak self] in self?.insert($0) }
        keyArea.onDelete = { [weak self] in self?.deleteOnce() }
        keyArea.onReturn = { [weak self] in self?.newline() }
        keyArea.onDoubleSpace = { [weak self] in self?.doubleSpace() }
        refreshLabels()
    }
    override func viewWillAppear(_ animated: Bool) { super.viewWillAppear(animated); refreshLabels() }
    override func viewWillLayoutSubviews() { super.viewWillLayoutSubviews(); updateTypingContext() }
    override func traitCollectionDidChange(_ previousTraitCollection: UITraitCollection?) {
        super.traitCollectionDidChange(previousTraitCollection)
        updateTypingContext()
    }
    override func textDidChange(_ textInput: UITextInput?) { super.textDidChange(textInput); updateTypingContext() }
    override func selectionDidChange(_ textInput: UITextInput?) { super.selectionDidChange(textInput); updateTypingContext() }
    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated); task?.cancel(); keyArea.cancelTouches()
    }
    private func updateTypingContext() {
        keyArea.updateContext(before: textDocumentProxy.documentContextBeforeInput, capitalization: textDocumentProxy.autocapitalizationType, returnType: textDocumentProxy.returnKeyType, appearance: textDocumentProxy.keyboardAppearance, needsGlobe: needsInputModeSwitchKey)
        let theme = KeyboardTheme(appearance: textDocumentProxy.keyboardAppearance, traits: traitCollection)
        view.backgroundColor = theme.background
        actionButtons.forEach { $0.backgroundColor = theme.letter; $0.tintColor = KeyboardTheme.blue }
    }
    private func button(_ title: String, action: Selector) -> UIButton {
        let b = UIButton(type: .system)
        b.setTitle(title, for: .normal); b.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
        b.contentEdgeInsets = UIEdgeInsets(top: 0, left: 10, bottom: 0, right: 10)
        b.layer.cornerRadius = 6
        b.addTarget(self, action: action, for: .touchUpInside)
        return b
    }
    private func actionButton(_ title: String, mode: SmartActionMode) -> UIButton {
        let b = button(title, action: #selector(actionPressed(_:)))
        b.accessibilityIdentifier = mode.rawValue; actionButtons.append(b); return b
    }
    private func refreshLabels() { translateButton?.setTitle("Translate → " + settings.translateTarget.uppercased(), for: .normal) }
    @objc private func defaultAction() { run(settings.defaultAction) }
    @objc private func actionPressed(_ sender: UIButton) {
        if let mode = sender.accessibilityIdentifier.flatMap(SmartActionMode.init(rawValue:)) { run(mode) }
    }
    private func run(_ mode: SmartActionMode) {
        guard !busy else { return }
        guard hasFullAccess else { status.text = "Allow Full Access for Gusan in Settings › General › Keyboard › Keyboards › Gusan"; return }
        guard store.read() != nil, store.diagnostics().isShared else { status.text = "Open the Gusan app and sign in"; return }
        let selected = textDocumentProxy.selectedText ?? ""
        let before = textDocumentProxy.documentContextBeforeInput ?? ""
        let after = textDocumentProxy.documentContextAfterInput ?? ""
        let text = selected.isEmpty ? before + after : selected
        guard !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { status.text = "Type or select some text first"; return }
        let document = textDocumentProxy.documentIdentifier
        busy = true; spinner.startAnimating(); status.text = "Working…"; undoButton.isHidden = true
        keyArea.inputEnabled = false
        actionButtons.forEach { $0.isEnabled = false }
        task = Task { @MainActor in
            defer { busy = false; keyArea.inputEnabled = true; spinner.stopAnimating(); actionButtons.forEach { $0.isEnabled = true }; updateTypingContext() }
            do {
                guard let session = try await auth.refresh() else { throw GusanError.unauthorized }
                let client = SmartActionClient()
                let result: String
                do { result = try await client.run(text: text, mode: mode, accessToken: session.accessToken, target: settings.translateTarget) }
                catch GusanError.unauthorized {
                    guard let refreshed = try await auth.refresh(force: true) else { throw GusanError.unauthorized }
                    result = try await client.run(text: text, mode: mode, accessToken: refreshed.accessToken, target: settings.translateTarget)
                }
                try Task.checkCancellation()
                guard textDocumentProxy.documentIdentifier == document,
                      (textDocumentProxy.selectedText ?? "") == selected,
                      (textDocumentProxy.documentContextBeforeInput ?? "") == before,
                      (textDocumentProxy.documentContextAfterInput ?? "") == after else {
                    status.text = "Text changed. Select your text and try again."; return
                }
                guard !result.isEmpty else { throw GusanError.invalidResponse }
                if selected.isEmpty {
                    textDocumentProxy.adjustTextPosition(byCharacterOffset: after.utf16.count)
                    for _ in text { textDocumentProxy.deleteBackward() }
                }
                textDocumentProxy.insertText(result)
                original = text; replacement = result
                undoDocument = textDocumentProxy.documentIdentifier
                undoBefore = textDocumentProxy.documentContextBeforeInput
                undoAfter = textDocumentProxy.documentContextAfterInput
                status.text = "Done"; undoButton.isHidden = false
            } catch is CancellationError { status.text = "Cancelled" }
            catch { status.text = error.localizedDescription }
        }
    }
    @objc private func undo() {
        guard !busy, let original, let replacement else { return }
        guard textDocumentProxy.documentIdentifier == undoDocument,
              textDocumentProxy.documentContextBeforeInput == undoBefore,
              textDocumentProxy.documentContextAfterInput == undoAfter,
              (textDocumentProxy.selectedText ?? "").isEmpty else {
            status.text = "Text changed. Undo is no longer available."; undoButton.isHidden = true; return
        }
        for _ in replacement { textDocumentProxy.deleteBackward() }
        textDocumentProxy.insertText(original)
        self.original = nil; self.replacement = nil; undoButton.isHidden = true; status.text = "Undone"
        updateTypingContext()
    }
    private func invalidateUndo() { original = nil; replacement = nil; undoButton.isHidden = true }
    private func insert(_ text: String) {
        guard !busy else { return }
        invalidateUndo(); textDocumentProxy.insertText(text); updateTypingContext()
    }
    private func newline() { insert("\n") }
    private func deleteOnce() {
        guard !busy else { return }
        invalidateUndo(); textDocumentProxy.deleteBackward(); updateTypingContext()
    }
    private func doubleSpace() {
        guard !busy else { return }
        invalidateUndo(); textDocumentProxy.deleteBackward(); textDocumentProxy.insertText(". "); updateTypingContext()
    }
}
