import BackgroundTasks
import Combine
import GusanKit
import SwiftUI
import UserNotifications

/// App-side owner of agent tasks: loads them from Gusan Cloud, polls while the app is active,
/// refreshes in the background when iOS allows it, and posts local notifications for results and
/// confirmations. There is no APNs push (free Apple ID), so everything here is polling.
@MainActor
final class AgentCenter: NSObject, ObservableObject {
    static let refreshTaskID = "com.aram2k.gusan.agent-refresh"
    static let confirmCategory = "GUSAN_AGENT_CONFIRM"
    static let resultCategory = "GUSAN_AGENT_RESULT"

    @Published private(set) var tasks: [AgentTask] = []
    @Published private(set) var loading = false
    @Published var error: String?
    @Published private(set) var notificationStatus: UNAuthorizationStatus = .notDetermined
    @Published var lastRefresh: Date?

    private unowned let model: AppModel
    private var service: AgentService?
    private var serviceToken: String?
    private var poller: Task<Void, Never>?
    private var active = false
    private lazy var tracker = AgentTaskTracker(defaults: UserDefaults(suiteName: model.store.resolution.chosen) ?? .standard)
    lazy var sessionStore = AgentSessionStore(defaults: UserDefaults(suiteName: model.store.resolution.chosen) ?? .standard)

    init(model: AppModel) {
        self.model = model
        super.init()
        UNUserNotificationCenter.current().delegate = self
        UNUserNotificationCenter.current().setNotificationCategories([
            UNNotificationCategory(identifier: Self.confirmCategory, actions: [
                UNNotificationAction(identifier: "confirm", title: "Confirm", options: [.authenticationRequired]),
                UNNotificationAction(identifier: "cancel", title: "Cancel task", options: [.destructive, .authenticationRequired]),
            ], intentIdentifiers: []),
            UNNotificationCategory(identifier: Self.resultCategory, actions: [], intentIdentifiers: []),
        ])
        Task { await refreshNotificationStatus() }
    }

    var signedIn: Bool { model.session != nil }
    var activeCount: Int { tasks.filter { !$0.status.isTerminal }.count }
    var atActiveLimit: Bool { activeCount >= AgentTask.maxActive }
    /// Tasks the user confirmed from this device; if one is awaiting again, the server is asking to confirm again.
    @Published private(set) var confirmedIDs: Set<UUID> = []
    /// Full instructions of tasks started on this device (the server keeps only a 120-char title).
    private var instructions: [UUID: (instruction: String, context: String?)] = [:]
    /// Whether "Run again" can reproduce the original request faithfully.
    func canRunAgain(_ task: AgentTask) -> Bool { instructions[task.id] != nil }
    var threads: [AgentSessionStore.Thread] { sessionStore.threads(for: tasks) }
    func message(for task: AgentTask) -> String { sessionStore.records.first { $0.id == task.id }?.message ?? task.title }
    func needsReconfirm(_ task: AgentTask) -> Bool { task.status == .awaitingConfirmation && confirmedIDs.contains(task.id) }

    var unseenCount: Int { tasks.filter(\.needsAttention).count }
    var hasLiveTasks: Bool { tasks.contains { !$0.status.isTerminal } }
    var isDemo: Bool { model.session?.accessToken == "gusan-debug-no-network" }

    func task(_ id: UUID) -> AgentTask? { tasks.first { $0.id == id } }

    // MARK: Service

    private func currentService() -> AgentService? {
        guard let session = model.session else { service = nil; serviceToken = nil; return nil }
        if let service, serviceToken == session.userID ?? session.accessToken { return service }
        #if DEBUG
        if session.accessToken == "gusan-debug-no-network" {
            let stub = StubAgentService(seeded: ProcessInfo.processInfo.arguments.contains("--gusan-agent-demo") ? StubAgentService.demoTasks() : [])
            stub.delay = 400_000_000
            service = stub; serviceToken = session.userID ?? session.accessToken; return stub
        }
        #endif
        let client = AgentClient(auth: model.auth)
        service = client; serviceToken = session.userID ?? session.accessToken
        return client
    }

    // MARK: Loading and polling

    func refresh() async {
        guard let service = currentService() else { tasks = []; return }
        loading = tasks.isEmpty
        defer { loading = false }
        do {
            var fetched = try await service.list(limit: 100)
            let listedIDs = Set(fetched.map(\.id))
            for task in tasks where !listedIDs.contains(task.id) && !task.status.isTerminal {
                fetched.append(try await service.status(task.id, waitSeconds: 0))
            }
            // The table hides the pending confirmation text; ask the function for it.
            for index in fetched.indices where fetched[index].status == .awaitingConfirmation && fetched[index].confirmation == nil {
                if let full = try? await service.status(fetched[index].id, waitSeconds: 0) { fetched[index] = full }
            }
            let fetchedIDs = Set(fetched.map(\.id))
            tasks = (fetched + tasks.filter { !fetchedIDs.contains($0.id) }).sorted { $0.createdAt > $1.createdAt }
            lastRefresh = Date()
            error = nil
            await deliver(tracker.consumeEvents(from: fetched))
        } catch {
            self.error = (error as? AgentError)?.errorDescription ?? error.localizedDescription
        }
    }

    /// Call from the scene phase: polls every few seconds while something is running, otherwise rarely.
    func setActive(_ active: Bool) {
        self.active = active
        poller?.cancel(); poller = nil
        guard active else { scheduleBackgroundRefresh(); return }
        if notificationStatus == .notDetermined, tracker.activeTaskID != nil { Task { await requestNotifications() } }
        poller = Task { @MainActor [weak self] in
            while let self, self.active, !Task.isCancelled {
                await self.refresh()
                let interval: UInt64 = self.hasLiveTasks ? 4_000_000_000 : 45_000_000_000
                try? await Task.sleep(nanoseconds: interval)
            }
        }
    }

    func sessionChanged() {
        service = nil; tasks = []; instructions = [:]; confirmedIDs = []
        Task { await refresh() }
    }

    // MARK: Actions

    @discardableResult
    func start(_ intent: AgentIntent, context: String? = nil, continuing parentID: UUID? = nil) async throws -> AgentTask {
        guard let service = currentService() else { throw AgentError.unauthorized }
        let task = try await service.start(intent, context: context)
        instructions[task.id] = (intent.instruction, context)
        sessionStore.record(taskID: task.id, message: intent.instruction, continuing: parentID, date: task.createdAt)
        tracker.markNotified(task)
        tasks.insert(task, at: 0)
        if notificationStatus == .notDetermined { await requestNotifications() }
        return task
    }

    func confirm(_ id: UUID) async {
        confirmedIDs.insert(id)
        await mutate(id) { try await $0.confirm(id) }
    }

    /// Starts the same request again (e.g. after a confirmation expired), with the original wording when known.
    func runAgain(_ task: AgentTask) async throws -> AgentTask {
        guard let original = instructions[task.id] else {
            throw NSError(domain: "Gusan.AgentRetry", code: 1, userInfo: [NSLocalizedDescriptionKey: "The original input is no longer available. Start again from Home."])
        }
        return try await start(AgentIntent(mode: task.mode, instruction: original.instruction), context: original.context, continuing: task.id)
    }

    @discardableResult
    func continueTask(_ id: UUID, message: String) async throws -> AgentTask {
        guard let service = currentService() else { throw AgentError.unauthorized }
        let task = try await service.continueTask(id, message: message)
        sessionStore.record(taskID: task.id, message: message, continuing: id, date: task.createdAt)
        tracker.markNotified(task)
        if let index = tasks.firstIndex(where: { $0.id == task.id }) { tasks[index] = task }
        else { tasks.insert(task, at: 0) }
        return task
    }

    func stop(_ id: UUID) async { await mutate(id) { try await $0.stop(id) } }

    func loadSession(_ id: UUID) async {
        let root = sessionStore.sessionID(for: id)
        let ids = Set(sessionStore.tasks(in: root).map(\.id) + [root, id])
        for taskID in ids { await load(taskID) }
    }

    func cancel(_ id: UUID) async {
        await mutate(id) { try await $0.cancel(id) }
    }

    /// Fetches one task that is not in the loaded page (older history, or a brand-new deep link).
    func load(_ id: UUID) async {
        guard task(id) == nil, let service = currentService(), let fetched = try? await service.status(id, waitSeconds: 0) else { return }
        if task(id) == nil { tasks.append(fetched); tasks.sort { $0.createdAt > $1.createdAt } }
    }

    func markSeen(_ id: UUID) {
        guard let index = tasks.firstIndex(where: { $0.id == id }), tasks[index].status.isTerminal, tasks[index].seenAt == nil, let service = currentService() else { return }
        tasks[index].seenAt = Date()
        Task { try? await service.markSeen(id) }
        UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [id.uuidString])
    }

    private func mutate(_ id: UUID, _ operation: (AgentService) async throws -> AgentTask) async {
        guard let service = currentService() else { return }
        do {
            let updated = try await operation(service)
            tracker.markNotified(updated)
            if let index = tasks.firstIndex(where: { $0.id == id }) { tasks[index] = updated } else { tasks.insert(updated, at: 0) }
            UNUserNotificationCenter.current().removeDeliveredNotifications(withIdentifiers: [id.uuidString])
        } catch {
            self.error = (error as? AgentError)?.errorDescription ?? error.localizedDescription
            if !active {
                let content = UNMutableNotificationContent()
                content.title = "Couldn't update the task"
                content.body = self.error ?? "Open Gusan to check it."
                content.userInfo = ["taskID": id.uuidString]
                try? await UNUserNotificationCenter.current().add(UNNotificationRequest(identifier: id.uuidString + ".error", content: content, trigger: nil))
            }
        }
    }

    // MARK: Notifications

    func refreshNotificationStatus() async {
        notificationStatus = await UNUserNotificationCenter.current().notificationSettings().authorizationStatus
    }

    func requestNotifications() async {
        _ = try? await UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge])
        await refreshNotificationStatus()
    }

    private func deliver(_ events: [AgentTaskTracker.Event]) async {
        guard !events.isEmpty else { return }
        await refreshNotificationStatus()
        guard notificationStatus == .authorized || notificationStatus == .provisional else { return }
        let center = UNUserNotificationCenter.current()
        for event in events {
            let content = UNMutableNotificationContent()
            let task: AgentTask
            switch event {
            case .needsLogin(let pending), .needsCode(let pending):
                task = pending
                content.title = "Gusan " + pending.phase.label.lowercased()
                content.body = pending.summaryLine
                content.categoryIdentifier = Self.resultCategory
                content.interruptionLevel = .timeSensitive
            case .needsConfirmation(let pending):
                task = pending
                content.title = "Gusan needs your OK"
                content.body = pending.confirmation?.summary ?? pending.title
                content.categoryIdentifier = Self.confirmCategory
                content.interruptionLevel = .timeSensitive
            case .finished(let done):
                task = done
                content.title = done.status == .completed ? (done.mode == .research ? "Research finished" : "Task done") : "Agent task \(done.status.label.lowercased())"
                content.body = done.status == .completed ? MarkdownBlocks.firstLine(done.result?.markdown ?? done.title) : (done.title + " — " + done.summaryLine)
                content.categoryIdentifier = Self.resultCategory
            }
            content.subtitle = task.title
            content.sound = .default
            content.threadIdentifier = "gusan-agent"
            content.userInfo = ["taskID": task.id.uuidString]
            try? await center.add(UNNotificationRequest(identifier: task.id.uuidString, content: content, trigger: nil))
        }
    }

    // MARK: Background refresh (best effort; iOS decides when it runs)

    static func registerBackgroundRefresh(_ center: AgentCenter) {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshTaskID, using: nil) { task in
            guard let refresh = task as? BGAppRefreshTask else { task.setTaskCompleted(success: false); return }
            Task { @MainActor in
                center.scheduleBackgroundRefresh()
                let work = Task { await center.refresh() }
                refresh.expirationHandler = { work.cancel() }
                await work.value
                refresh.setTaskCompleted(success: center.error == nil)
            }
        }
    }

    func scheduleBackgroundRefresh() {
        guard signedIn else { return }
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: hasLiveTasks ? 60 : 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}

extension AgentCenter: UNUserNotificationCenterDelegate {
    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, willPresent notification: UNNotification) async -> UNNotificationPresentationOptions {
        [.banner, .sound, .list]
    }

    nonisolated func userNotificationCenter(_ center: UNUserNotificationCenter, didReceive response: UNNotificationResponse) async {
        guard let raw = response.notification.request.content.userInfo["taskID"] as? String, let id = UUID(uuidString: raw) else { return }
        let action = response.actionIdentifier
        await MainActor.run {
            switch action {
            case "confirm": Task { await self.confirm(id) }
            case "cancel": Task { await self.cancel(id) }
            default: self.model.openSessionID = id; self.model.selectedTab = .sessions
            }
        }
    }
}
