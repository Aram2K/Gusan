import BackgroundTasks
import EventKit
import GusanKit
import SwiftUI

private actor CalendarEventReader {
    private let events = EKEventStore()

    func requestAccess() async throws -> Bool {
        try await events.requestFullAccessToEvents()
    }

    func snapshot(includeTitles: Bool) throws -> CalendarSnapshot {
        try Task.checkCancellation()
        let now = Date(), calendar = Calendar.current
        let start = calendar.startOfDay(for: now)
        guard let end = calendar.date(byAdding: .day, value: 8, to: start) else { throw CancellationError() }
        let calendars = events.calendars(for: .event)
        let predicate = events.predicateForEvents(withStart: start, end: end, calendars: calendars)
        let records = events.events(matching: predicate).compactMap { event -> CalendarEventRecord? in
            guard let start = event.startDate, let end = event.endDate else { return nil }
            let declined = event.attendees?.contains { $0.isCurrentUser && $0.participantStatus == .declined } == true
            return CalendarEventRecord(start: start, end: end, isAllDay: event.isAllDay, isFree: event.availability == .free,
                                       isDeclined: declined, isCancelled: event.status == .canceled, title: includeTitles ? event.title : nil)
        }
        try Task.checkCancellation()
        return CalendarSnapshot(generatedAt: now, windowStart: start, windowEnd: end, timeZoneID: calendar.timeZone.identifier, sources: ["iphone"],
                                blocks: CalendarEventFilter.blocks(from: records, windowStart: start, windowEnd: end, includeTitles: includeTitles))
    }
}

@MainActor
final class CalendarConnections: ObservableObject {
    enum State: Equatable {
        case notConnected, connected, denied, restricted
        var label: String {
            switch self {
            case .notConnected: return "Not connected"
            case .connected: return "Connected"
            case .denied: return "Access denied"
            case .restricted: return "Access restricted"
            }
        }
    }
    static let refreshTaskID = "com.aram2k.gusan.calendar-refresh"
    @Published private(set) var state: State = .notConnected
    @Published private(set) var connecting = false
    @Published private(set) var refreshing = false
    @Published private(set) var includeTitles: Bool
    @Published private(set) var lastUpdated: Date?
    @Published private(set) var error: String?
    private let store: CalendarSnapshotStore
    private let reader = CalendarEventReader()
    private var observer: NSObjectProtocol?
    private var debounce: Task<Void, Never>?
    private var connection: Task<Void, Never>?
    private var refreshTask: Task<CalendarSnapshot, Error>?
    private var generation = 0
    private var refreshAgain = false
    private var requestingPermission = false

    init(group: String?) {
        store = CalendarSnapshotStore(group: group)
        includeTitles = store.calendarIncludeTitles
        lastUpdated = store.snapshot?.generatedAt
        syncState()
        observer = NotificationCenter.default.addObserver(forName: .EKEventStoreChanged, object: nil, queue: nil) { [weak self] _ in
            Task { @MainActor in self?.eventsChanged() }
        }
    }
    deinit { if let observer { NotificationCenter.default.removeObserver(observer) } }

    private func syncState() {
        let permission = EKEventStore.authorizationStatus(for: .event)
        let updated: State
        switch permission {
        case .denied: updated = .denied
        case .restricted: updated = .restricted
        default: updated = permission == .fullAccess && store.calendarIPhoneEnabled ? .connected : .notConnected
        }
        if state != updated { state = updated }
        if includeTitles != store.calendarIncludeTitles { includeTitles = store.calendarIncludeTitles }
        let date = store.snapshot?.generatedAt
        if lastUpdated != date { lastUpdated = date }
    }

    func setIPhoneEnabled(_ enabled: Bool) {
        if !enabled {
            invalidate()
            store.calendarIPhoneEnabled = false
            syncState(); scheduleBackgroundRefresh()
            return
        }
        guard !requestingPermission, !connecting, state != .connected else { return }
        requestingPermission = true
        connecting = true; error = nil
        let version = generation
        connection = Task {
            defer {
                requestingPermission = false
                if version == generation { connecting = false; connection = nil }
            }
            do {
                let granted = try await reader.requestAccess()
                try Task.checkCancellation()
                guard version == generation else { return }
                store.calendarIPhoneEnabled = granted
                syncState()
                if granted { await refresh(); scheduleBackgroundRefresh() }
            } catch is CancellationError {} catch {
                guard version == generation else { return }
                self.error = "Calendar access couldn't be requested. Try again in Settings."
                syncState()
            }
        }
    }

    func setIncludeTitles(_ enabled: Bool) {
        guard includeTitles != enabled else { return }
        invalidate()
        store.calendarIncludeTitles = enabled
        syncState()
        Task { await refresh() }
    }

    private func invalidate() {
        generation += 1
        connection?.cancel(); connection = nil
        refreshTask?.cancel(); refreshTask = nil
        debounce?.cancel(); debounce = nil
        refreshAgain = false
        if connecting { connecting = false }
        if refreshing { refreshing = false }
        if error != nil { error = nil }
    }

    func sessionChanged() {
        invalidate()
        syncState()
        scheduleBackgroundRefresh()
    }

    func sceneChanged(_ phase: ScenePhase) {
        if phase == .active { Task { await refresh() }; scheduleBackgroundRefresh() }
        else if phase == .background { scheduleBackgroundRefresh() }
    }

    private func eventsChanged() {
        debounce?.cancel()
        debounce = Task {
            do { try await Task.sleep(nanoseconds: 750_000_000) } catch { return }
            await refresh()
        }
    }

    func refresh() async {
        syncState()
        guard store.calendarIPhoneEnabled else { return }
        guard state == .connected else {
            store.calendarIPhoneEnabled = false
            syncState()
            return
        }
        if let task = refreshTask { refreshAgain = true; _ = try? await task.value; return }
        let version = generation, titles = includeTitles
        refreshing = true
        let task = Task { try await reader.snapshot(includeTitles: titles) }
        refreshTask = task
        defer {
            if version == generation {
                refreshTask = nil; refreshing = false
                if refreshAgain { refreshAgain = false; Task { await refresh() } }
            }
        }
        do {
            let snapshot = try await withTaskCancellationHandler(operation: { try await task.value }, onCancel: { task.cancel() })
            try Task.checkCancellation()
            guard version == generation, store.calendarIPhoneEnabled else { return }
            store.replaceSource("iphone", snapshot: snapshot)
            if error != nil { error = nil }
            syncState()
        } catch is CancellationError {} catch {
            guard version == generation else { return }
            let message = "Calendar couldn't be refreshed. Your last snapshot will expire automatically."
            if self.error != message { self.error = message }
        }
    }

    static func registerBackgroundRefresh(_ connections: CalendarConnections) {
        BGTaskScheduler.shared.register(forTaskWithIdentifier: refreshTaskID, using: nil) { task in
            guard let refresh = task as? BGAppRefreshTask else { task.setTaskCompleted(success: false); return }
            Task { @MainActor in
                connections.scheduleBackgroundRefresh()
                let work = Task { await connections.refresh() }
                refresh.expirationHandler = { work.cancel() }
                await work.value
                refresh.setTaskCompleted(success: !work.isCancelled && connections.error == nil)
            }
        }
        connections.scheduleBackgroundRefresh()
    }

    private func scheduleBackgroundRefresh() {
        guard store.calendarIPhoneEnabled else {
            BGTaskScheduler.shared.cancel(taskRequestWithIdentifier: Self.refreshTaskID)
            return
        }
        let request = BGAppRefreshTaskRequest(identifier: Self.refreshTaskID)
        request.earliestBeginDate = Date(timeIntervalSinceNow: 15 * 60)
        try? BGTaskScheduler.shared.submit(request)
    }
}
