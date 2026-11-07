import UIKit
import GusanKit

final class KeyboardViewController: UIInputViewController {
    private let store = SessionStore()
    private lazy var auth = AuthClient(store: store)
    private lazy var settings = SettingsStore(group: store.resolution.chosen)
    private let status = UILabel()
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let undoButton = UIButton(type: .system)
    private let globe = UIButton(type: .system)
    private var actionButtons: [UIButton] = []
    private var translateButton: UIButton?
    private var busy = false
    private var original: String?
    private var replacement: String?
    private var undoDocument: UUID?
    private var undoBefore: String?
    private var undoAfter: String?
    private var deleteTimer: Timer?
    private var task: Task<Void, Never>?

    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .secondarySystemBackground
        let height = view.heightAnchor.constraint(equalToConstant: 240)
        height.priority = .defaultHigh; height.isActive = true
        let stack = UIStackView()
        stack.axis = .vertical; stack.spacing = 10; stack.translatesAutoresizingMaskIntoConstraints = false
        view.addSubview(stack)
        NSLayoutConstraint.activate([stack.leadingAnchor.constraint(equalTo: view.leadingAnchor, constant: 10), stack.trailingAnchor.constraint(equalTo: view.trailingAnchor, constant: -10), stack.topAnchor.constraint(equalTo: view.topAnchor, constant: 10), stack.bottomAnchor.constraint(equalTo: view.bottomAnchor, constant: -10)])
        let top = row()
        let logo = button("", action: #selector(defaultAction))
        logo.setImage(UIImage(named: "GusanLogo")?.withRenderingMode(.alwaysOriginal), for: .normal)
        logo.imageView?.contentMode = .scaleAspectFit
        logo.accessibilityLabel = "Run default Gusan action"
        top.addArrangedSubview(logo); actionButtons.append(logo)
        for (title, mode) in [("Smart", SmartActionMode.smartAuto), ("Email", .writeEmail), ("To-do", .makeTodoList)] {
            top.addArrangedSubview(actionButton(title, mode: mode))
        }
        stack.addArrangedSubview(top)
        let second = row()
        let translate = actionButton("Translate", mode: .translate)
        translateButton = translate
        second.addArrangedSubview(translate)
        second.addArrangedSubview(actionButton("Summarize", mode: .summarize))
        stack.addArrangedSubview(second)
        let stateRow = UIStackView(arrangedSubviews: [spinner, status, undoButton])
        stateRow.spacing = 8
        status.numberOfLines = 3; status.font = .systemFont(ofSize: 12); status.textColor = .secondaryLabel
        status.text = "Type or select text, then choose an action."
        undoButton.setTitle("Undo", for: .normal); undoButton.addTarget(self, action: #selector(undo), for: .touchUpInside)
        undoButton.isHidden = true
        undoButton.setContentHuggingPriority(.required, for: .horizontal)
        stack.addArrangedSubview(stateRow)
        let bottom = row()
        globe.setImage(UIImage(systemName: "globe"), for: .normal)
        globe.addTarget(self, action: #selector(handleInputModeList(from:with:)), for: .allTouchEvents)
        globe.accessibilityLabel = "Next keyboard"
        bottom.addArrangedSubview(globe)
        bottom.addArrangedSubview(button("space", action: #selector(space)))
        let delete = button("⌫", action: #selector(deleteOnce))
        delete.accessibilityLabel = "Delete"
        delete.addGestureRecognizer(UILongPressGestureRecognizer(target: self, action: #selector(holdDelete(_:))))
        bottom.addArrangedSubview(delete)
        bottom.addArrangedSubview(button("return", action: #selector(newline)))
        stack.addArrangedSubview(bottom)
        refreshLabels()
    }
    override func viewWillAppear(_ animated: Bool) { super.viewWillAppear(animated); refreshLabels() }
    override func viewWillLayoutSubviews() { super.viewWillLayoutSubviews(); globe.isHidden = !needsInputModeSwitchKey }
    override func viewWillDisappear(_ animated: Bool) {
        super.viewWillDisappear(animated); task?.cancel(); deleteTimer?.invalidate()
    }
    private func row() -> UIStackView {
        let row = UIStackView(); row.distribution = .fillEqually; row.spacing = 8; return row
    }
    private func button(_ title: String, action: Selector) -> UIButton {
        let b = UIButton(type: .system)
        b.setTitle(title, for: .normal); b.titleLabel?.font = .systemFont(ofSize: 14, weight: .medium)
        b.backgroundColor = .tertiarySystemBackground; b.layer.cornerRadius = 8
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
        actionButtons.forEach { $0.isEnabled = false }
        task = Task { @MainActor in
            defer { busy = false; spinner.stopAnimating(); actionButtons.forEach { $0.isEnabled = true } }
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
    }
    private func invalidateUndo() { original = nil; replacement = nil; undoButton.isHidden = true }
    @objc private func space() { guard !busy else { return }; invalidateUndo(); textDocumentProxy.insertText(" ") }
    @objc private func newline() { guard !busy else { return }; invalidateUndo(); textDocumentProxy.insertText("\n") }
    @objc private func deleteOnce() { guard !busy else { return }; invalidateUndo(); textDocumentProxy.deleteBackward() }
    @objc private func holdDelete(_ gesture: UILongPressGestureRecognizer) {
        if gesture.state == .began {
            deleteOnce()
            deleteTimer = Timer.scheduledTimer(withTimeInterval: 0.08, repeats: true) { [weak self] _ in self?.deleteOnce() }
        } else if gesture.state != .changed { deleteTimer?.invalidate(); deleteTimer = nil }
    }
}
