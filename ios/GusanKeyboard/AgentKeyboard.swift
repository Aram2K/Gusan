import UIKit
import GusanKit

extension SmartAction {
    /// The user's own words when an action might be an agent task ("find…", "book…") instead of a text edit.
    func agentRequest(contextText: String) -> String? {
        switch self {
        case .custom(let instruction): return instruction
        case .mode(.smartAuto): return contextText
        default: return nil
        }
    }
    var isCustomInstruction: Bool { if case .custom = self { return true } else { return false } }
}

/// Runs one agent task from the keyboard through the shared job pipeline (`AgentJob`), and drives
/// `AgentCardView` through progress → (confirmation) → result. The server-side task survives the
/// card being minimised and the keyboard being dismissed; the app's Activity tab shows the same task.
@MainActor
final class AgentKeyboardController {
    let card = AgentCardView()
    /// Toolbar status while the card is minimised: (message, accessory title). nil clears it.
    var onStatus: ((String?, String?) -> Void)?
    var onSurfaceChange: (() -> Void)?
    /// Set by the host: opens the containing app (keyboard extensions cannot call UIApplication).
    var onOpenApp: ((URL) -> Void)?
    private(set) var showingCard = false

    private let store: SessionStore
    private let auth: AuthClient
    private weak var host: UIInputViewController?
    private let tracker: AgentTaskTracker
    private var service: AgentService?
    private(set) var task: AgentTask?
    private var pending: JobConfirmation?
    private var error: String?
    private var errorRetryable = false
    private var job: Task<Void, Never>?
    private var visible = false
    private var lastIntent: AgentIntent?
    private var lastContext = ""
    private var confirmations = 0
    private lazy var settings = SettingsStore(group: store.resolution.chosen)

    init(store: SessionStore, auth: AuthClient, host: UIInputViewController) {
        self.store = store; self.auth = auth; self.host = host
        tracker = AgentTaskTracker(defaults: UserDefaults(suiteName: store.resolution.chosen) ?? .standard)
        card.onConfirm = { [weak self] in self?.confirm() }
        card.onCancel = { [weak self] in self?.cancel() }
        card.onInsert = { [weak self] in self?.insert() }
        card.onOpenApp = { [weak self] in self?.openApp() }
        card.onDismiss = { [weak self] in self?.dismiss() }
        card.onMinimise = { [weak self] in self?.hide() }
        card.onRetry = { [weak self] in self?.retry() }
        card.isHidden = true
    }

    var isActive: Bool { task != nil || error != nil }

    // MARK: Lifecycle

    func appear() {
        visible = true
        settings.refresh()
        if settings.privacyMode { if isActive { dismiss() }; return }
        #if DEBUG
        // Simulator screenshots: the app seeds a demo instruction on a named pasteboard (`--gusan-keyboard-agent-demo`).
        if task == nil, let board = UIPasteboard(name: UIPasteboard.Name("gusan.debug.agentDemo"), create: false), let demo = board.string, let intent = AgentIntent.detect(demo) {
            board.string = ""
            start(intent, context: ""); return
        }
        #endif
        if let task, !task.status.isTerminal { if job == nil { run(.resume(task.id), announce: false) } }
        else if task == nil, error == nil, let id = tracker.activeTaskID { run(.resume(id), announce: false) }
    }

    /// Stops polling only; the task keeps running on Gusan Cloud and is picked up again on appear.
    func disappear() { visible = false; job?.cancel(); job = nil; pending = nil }

    func apply(_ theme: KeyboardTheme) { card.apply(theme) }

    // MARK: Actions

    func start(_ intent: AgentIntent, context: String) {
        job?.cancel(); pending = nil
        error = nil; errorRetryable = false
        lastIntent = intent; lastContext = context; confirmations = 0
        task = AgentTask(mode: intent.mode, status: .queued, title: intent.title)
        show()
        render()
        run(.new(intent, context: context.isEmpty ? nil : context), announce: true)
    }

    func show() { guard isActive else { return }; showingCard = true; card.isHidden = false; onSurfaceChange?(); onStatus?(nil, nil) }

    func hide() {
        showingCard = false; card.isHidden = true; onSurfaceChange?()
        if let task, !task.status.isTerminal || task.seenAt == nil { onStatus?(toolbarLine(task), "View") } else { onStatus?(nil, nil) }
    }

    private func retry() {
        guard let lastIntent else { dismiss(); return }
        start(lastIntent, context: lastContext)
    }

    private func confirm() {
        guard let pending else { return }
        self.pending = nil
        confirmations += 1
        card.setBusy(true)
        Task { await pending.resolve(true) }
    }

    private func cancel() {
        card.setBusy(true)
        if let pending { self.pending = nil; Task { await pending.resolve(false) }; return }
        guard let service, let id = task?.id else { dismiss(); return }
        job?.cancel(); job = nil
        Task { @MainActor [weak self] in
            do { let updated = try await service.cancel(id); self?.update(updated) }
            catch { self?.card.flash((error as? AgentError)?.errorDescription ?? error.localizedDescription) }
            self?.card.setBusy(false)
        }
    }

    private func insert() {
        guard let text = task?.result?.plainText, !text.isEmpty, let proxy = host?.textDocumentProxy else { return }
        proxy.insertText(text)
        UIAccessibility.post(notification: .announcement, argument: "Inserted")
        dismiss()
    }

    private func openApp() {
        let url = task.map { "gusan://task?id=\($0.id.uuidString.lowercased())" } ?? "gusan://task"
        onOpenApp?(URL(string: url)!)
    }

    private func dismiss() {
        job?.cancel(); job = nil; pending = nil
        if let service, let task, task.status.isTerminal, task.seenAt == nil { Task { try? await service.markSeen(task.id) } }
        task = nil; error = nil
        tracker.activeTaskID = nil
        showingCard = false; card.isHidden = true
        onStatus?(nil, nil); onSurfaceChange?()
    }

    // MARK: Job pipeline

    private func run(_ start: AgentJob.Start, announce: Bool) {
        let service = makeService()
        self.service = service
        let agentJob = AgentJob(service: service, start: start, interval: service is StubAgentService ? 1_200_000_000 : 1_500_000_000) { [weak self] task in
            Task { @MainActor in self?.update(task) }
        }
        job = Task { @MainActor [weak self] in
            do {
                for try await event in agentJob.run() {
                    guard let self else { return }
                    switch event {
                    case .progress(let text):
                        if self.task?.status == .queued || self.task?.status == .running { self.card.showStep(text) }
                    case .needsConfirmation(let request):
                        self.pending = request
                        self.card.setBusy(false)
                        self.render()
                    case .result:
                        self.card.setBusy(false)
                        self.render()
                    }
                }
            } catch is CancellationError {
                self?.card.setBusy(false); self?.render()
            } catch {
                guard let self else { return }
                self.card.setBusy(false)
                if self.task?.status.isTerminal == true { self.render(); return }
                if case .resume = start, (error as? AgentError) == .notFound { self.tracker.activeTaskID = nil; self.task = nil; self.render(); return }
                self.task = nil; self.error = (error as? AgentError)?.errorDescription ?? error.localizedDescription
                self.errorRetryable = (error as? AgentError)?.isRetryable ?? true
                UIAccessibility.post(notification: .announcement, argument: self.error)
                self.render()
            }
            self?.job = nil
        }
    }

    private func update(_ fresh: AgentTask) {
        let previous = task?.status
        task = fresh
        tracker.activeTaskID = fresh.status.isTerminal ? nil : fresh.id
        tracker.markNotified(fresh)
        if previous != fresh.status, previous != nil, fresh.status == .awaitingConfirmation || fresh.status.isTerminal {
            if settingsHaptics { UINotificationFeedbackGenerator().notificationOccurred(fresh.status == .completed || fresh.status == .awaitingConfirmation ? .success : .error) }
            if !showingCard { onStatus?(toolbarLine(fresh), "View") }
            UIAccessibility.post(notification: .announcement, argument: fresh.summaryLine)
        }
        if fresh.status != .awaitingConfirmation { pending = nil }
        if previous == nil, !showingCard { onStatus?(toolbarLine(fresh), "View") }
        render()
    }

    private var settingsHaptics: Bool { (UserDefaults(suiteName: store.resolution.chosen) ?? .standard).object(forKey: "haptics") as? Bool ?? true }

    private func toolbarLine(_ task: AgentTask) -> String {
        switch task.status {
        case .queued, .running: return (task.mode == .research ? "Researching… " : "Working on it… ") + (task.latestStep ?? "")
        case .awaitingConfirmation: return "Agent needs your OK"
        case .completed: return "Agent result ready"
        case .failed, .timedOut: return "Agent task failed"
        case .cancelled: return "Agent task cancelled"
        }
    }

    private func render() {
        card.canRetry = lastIntent != nil
        card.reconfirm = confirmations > 0
        if let error { card.render(.failed(message: error, retryable: errorRetryable)); return }
        guard let task else { card.render(.empty); return }
        card.render(.task(task))
    }

    private func makeService() -> AgentService {
        if let service { return service }
        #if DEBUG
        if store.read()?.accessToken == "gusan-debug-no-network" {
            let stub = StubAgentService(); stub.delay = 600_000_000; return stub
        }
        #endif
        return AgentClient(auth: auth)
    }
}

/// Compact card that replaces the keys: progress, confirmation, result or error. Every control is
/// at least 44pt tall; VoiceOver gets labels, hints and announcements.
final class AgentCardView: UIView {
    enum State { case empty, task(AgentTask), failed(message: String, retryable: Bool) }

    /// Whether "Run again"/"Retry" can restart the same request (the controller still has the intent).
    var canRetry = false
    /// The server asked for the same confirmation again (its first delivery was uncertain).
    var reconfirm = false
    var onRetry: (() -> Void)?
    var onConfirm: (() -> Void)?
    var onCancel: (() -> Void)?
    var onInsert: (() -> Void)?
    var onOpenApp: (() -> Void)?
    var onDismiss: (() -> Void)?
    var onMinimise: (() -> Void)?

    private let sheet = UIView()
    private let icon = UIImageView()
    private let spinner = UIActivityIndicatorView(style: .medium)
    private let title = UILabel()
    private let status = UILabel()
    private let body = UITextView()
    private let minimise = UIButton(type: .system)
    private let primary = UIButton(type: .system)
    private let secondary = UIButton(type: .system)
    private let tertiary = UIButton(type: .system)
    private var theme = KeyboardTheme(appearance: nil, traits: UITraitCollection.current)
    private var flashTimer: Timer?
    private var state: State = .empty

    override init(frame: CGRect) {
        super.init(frame: frame)
        addSubview(sheet)
        [icon, spinner, title, status, body, minimise, primary, secondary, tertiary].forEach { sheet.addSubview($0) }
        sheet.layer.cornerRadius = 14; sheet.layer.cornerCurve = .continuous
        icon.contentMode = .scaleAspectFit; icon.preferredSymbolConfiguration = UIImage.SymbolConfiguration(pointSize: 18, weight: .semibold)
        title.font = .systemFont(ofSize: 14, weight: .semibold); title.numberOfLines = 2
        status.font = .systemFont(ofSize: 12, weight: .medium); status.numberOfLines = 1
        body.isEditable = false; body.isSelectable = false; body.font = .preferredFont(forTextStyle: .footnote); body.adjustsFontForContentSizeCategory = true
        body.textContainerInset = UIEdgeInsets(top: 6, left: 4, bottom: 6, right: 4); body.layer.cornerRadius = 10; body.layer.cornerCurve = .continuous
        body.alwaysBounceVertical = true; body.showsVerticalScrollIndicator = true
        minimise.setImage(UIImage(systemName: "keyboard"), for: .normal)
        minimise.accessibilityLabel = "Back to typing"; minimise.accessibilityHint = "Keeps the task running; tap View in the status bar to return"
        minimise.addTarget(self, action: #selector(minimisePressed), for: .touchUpInside)
        for button in [primary, secondary, tertiary] {
            button.titleLabel?.font = .systemFont(ofSize: 15, weight: .semibold)
            button.layer.cornerRadius = 12; button.layer.cornerCurve = .continuous
            button.addTarget(self, action: #selector(buttonPressed(_:)), for: .touchUpInside)
        }
        accessibilityViewIsModal = false
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func apply(_ theme: KeyboardTheme) { self.theme = theme; restyle() }

    func setBusy(_ busy: Bool) {
        [primary, secondary, tertiary].forEach { $0.isEnabled = !busy; $0.alpha = busy ? 0.5 : 1 }
        busy ? spinner.startAnimating() : spinner.stopAnimating()
    }

    func flash(_ message: String) {
        status.text = message; status.textColor = theme.danger
        UIAccessibility.post(notification: .announcement, argument: message)
        flashTimer?.invalidate()
        flashTimer = Timer.scheduledTimer(withTimeInterval: 5, repeats: false) { [weak self] _ in self?.restyle() }
    }

    func render(_ state: State) { self.state = state; restyle(); setNeedsLayout() }

    /// Live progress line while a task runs, without re-rendering the whole card.
    func showStep(_ text: String) { body.text = "Latest: " + text; body.accessibilityLabel = body.text }

    private var buttons: [(UIButton, String, Bool)] = []

    private func restyle() {
        backgroundColor = theme.background
        sheet.backgroundColor = theme.letter
        title.textColor = theme.foreground; status.textColor = theme.dark ? UIColor.white.withAlphaComponent(0.75) : .darkGray
        body.backgroundColor = theme.dark ? UIColor.white.withAlphaComponent(0.08) : UIColor.black.withAlphaComponent(0.05)
        body.textColor = theme.foreground
        minimise.tintColor = KeyboardTheme.blue
        buttons = []
        spinner.color = theme.dark ? .white : .gray
        var announce: String?
        switch state {
        case .empty:
            title.text = nil; status.text = nil; body.text = nil; icon.image = nil; spinner.stopAnimating()
        case .failed(let message, let retryable):
            icon.image = UIImage(systemName: "exclamationmark.triangle.fill"); icon.tintColor = theme.danger
            icon.isHidden = false
            title.text = "Couldn't start the task"; status.text = nil; body.text = message
            spinner.stopAnimating()
            if retryable, canRetry { buttons = [(secondary, "OK", false), (primary, "Retry", true)] }
            else if message.contains("Activity") { buttons = [(secondary, "OK", false), (primary, "Open in app", true)] }
            else { buttons = [(primary, "OK", true)] }
        case .task(let task):
            title.text = task.title
            let running = task.status == .queued || task.status == .running
            running ? spinner.startAnimating() : spinner.stopAnimating()
            icon.isHidden = running
            switch task.status {
            case .queued, .running:
                status.text = task.mode == .research ? "Research · read-only" : "Do-it task · asks before acting"
                body.text = task.latestStep.map { "Latest: " + $0 } ?? "Starting an agent…"
                buttons = [(secondary, "Cancel", false)]
            case .awaitingConfirmation:
                icon.image = UIImage(systemName: "hand.raised.fill"); icon.tintColor = theme.brandOrange
                status.text = reconfirm ? "Confirm again" : (task.confirmation?.kindLabel ?? "Needs your OK")
                var text = task.confirmation?.summary ?? "The agent wants to continue."
                if reconfirm { text = "Your first confirmation may not have reached the agent. Nothing was done twice.\n\n" + text }
                text += "\n\nNothing is submitted, bought or sent until you confirm. This request expires after 15 minutes"
                if let deadline = task.deadlineAt { text += " (and no later than \(Self.clock.string(from: deadline)))" }
                body.text = text + "."
                buttons = [(secondary, "Cancel", false), (primary, reconfirm ? "Confirm again" : "Confirm", true)]
                announce = (reconfirm ? "Confirm again. " : "Needs your OK. ") + (task.confirmation?.summary ?? "")
            case .completed:
                icon.image = UIImage(systemName: "checkmark.circle.fill"); icon.tintColor = theme.success
                status.text = "Done · " + (task.mode == .research ? "Research" : "Do-it task")
                body.text = task.result?.plainText ?? task.outcome ?? "Finished"
                buttons = [(tertiary, "Open in app", false), (primary, "Insert", true)]
                if task.result?.plainText.isEmpty ?? true { buttons = [(secondary, "Dismiss", false), (primary, "Open in app", true)] }
                announce = "Done. " + MarkdownBlocks.firstLine(task.result?.markdown ?? "")
            case .failed, .timedOut, .cancelled:
                icon.image = UIImage(systemName: task.status == .cancelled ? "xmark.circle.fill" : "exclamationmark.triangle.fill")
                icon.tintColor = task.status == .cancelled ? .gray : theme.danger
                status.text = task.confirmationExpired ? "Confirmation expired" : task.status.label
                body.text = task.confirmationExpired ? "The agent waited 15 minutes for your OK and then stopped. Nothing was submitted, bought or sent." : task.summaryLine
                buttons = [(secondary, "Dismiss", false), (primary, "Open in app", true)]
                if canRetry, task.status != .cancelled || task.confirmationExpired { buttons = [(secondary, "Dismiss", false), (tertiary, "Open in app", false), (primary, "Run again", true)] }
            }
        }
        for button in [primary, secondary, tertiary] { button.isHidden = true }
        for (button, name, filled) in buttons {
            button.isHidden = false; button.setTitle(name, for: .normal)
            button.backgroundColor = filled ? KeyboardTheme.blue : theme.special
            button.setTitleColor(filled ? .white : (name == "Cancel" ? theme.danger : theme.foreground), for: .normal)
            button.accessibilityHint = hint(for: name)
        }
        body.accessibilityLabel = body.text
        if let announce { UIAccessibility.post(notification: .layoutChanged, argument: announce) }
    }

    private func hint(for name: String) -> String {
        switch name {
        case "Confirm", "Confirm again": return "Lets the agent take exactly the action described"
        case "Cancel": return "Stops the task; nothing is submitted"
        case "Retry", "Run again": return "Starts the same request again"
        case "Insert": return "Pastes the result at the cursor"
        case "Open in app": return "Shows the full result in the Gusan app"
        default: return "Closes this card"
        }
    }

    static let clock: DateFormatter = { let f = DateFormatter(); f.timeStyle = .short; f.dateStyle = .none; return f }()

    override func layoutSubviews() {
        super.layoutSubviews()
        sheet.frame = bounds.insetBy(dx: 8, dy: 6)
        let w = sheet.bounds.width
        icon.frame = CGRect(x: 12, y: 10, width: 24, height: 24); spinner.frame = icon.frame
        minimise.frame = CGRect(x: w - 48, y: 0, width: 44, height: 44)
        title.frame = CGRect(x: 44, y: 6, width: w - 100, height: 34)
        status.frame = CGRect(x: 44, y: 40, width: w - 56, height: 16)
        let buttonsHeight: CGFloat = buttons.isEmpty ? 0 : 44
        let bodyTop: CGFloat = 60
        body.frame = CGRect(x: 12, y: bodyTop, width: w - 24, height: max(0, sheet.bounds.height - bodyTop - buttonsHeight - (buttons.isEmpty ? 10 : 18)))
        let gap: CGFloat = 8
        let count = CGFloat(buttons.count)
        let width = count > 0 ? (w - 24 - gap * (count - 1)) / count : 0
        for (index, entry) in buttons.enumerated() {
            entry.0.frame = CGRect(x: 12 + CGFloat(index) * (width + gap), y: sheet.bounds.height - 44 - 10, width: width, height: 44)
            entry.0.titleLabel?.font = .systemFont(ofSize: buttons.count > 2 ? 14 : 15, weight: .semibold)
            entry.0.titleLabel?.adjustsFontSizeToFitWidth = true; entry.0.titleLabel?.minimumScaleFactor = 0.8
        }
    }

    @objc private func minimisePressed() { onMinimise?() }
    @objc private func buttonPressed(_ sender: UIButton) {
        switch sender.currentTitle {
        case "Confirm", "Confirm again": onConfirm?()
        case "Retry", "Run again": onRetry?()
        case "Cancel": onCancel?()
        case "Insert": onInsert?()
        case "Open in app": onOpenApp?()
        default: onDismiss?()
        }
    }
}
