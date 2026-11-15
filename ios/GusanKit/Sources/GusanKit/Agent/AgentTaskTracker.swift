import Foundation

/// Remembers which task states the user has already been told about, so polling from the app,
/// background refresh and the keyboard produce exactly one local notification per transition.
public final class AgentTaskTracker {
    public enum Event: Equatable {
        case needsConfirmation(AgentTask)
        case needsLogin(AgentTask)
        case needsCode(AgentTask)
        case finished(AgentTask)
    }

    private let defaults: UserDefaults
    private let key = "gusan.agent.notifiedStates"
    public static let activeTaskKey = "gusan.agent.activeTaskID"

    public init(defaults: UserDefaults) { self.defaults = defaults }

    private var notified: [String: String] {
        get { defaults.dictionary(forKey: key) as? [String: String] ?? [:] }
        set { defaults.set(newValue, forKey: key) }
    }

    /// Returns the transitions that still deserve a notification and records them as notified.
    public func consumeEvents(from tasks: [AgentTask]) -> [Event] {
        var seen = notified
        var events: [Event] = []
        for task in tasks {
            let state = task.rawStatus
            guard seen[task.id.uuidString] != state else { continue }
            seen[task.id.uuidString] = state
            if task.phase == .needsLogin { events.append(.needsLogin(task)); continue }
            if task.phase == .needsCode { events.append(.needsCode(task)); continue }
            switch task.status {
            case .awaitingConfirmation: events.append(.needsConfirmation(task))
            case .completed, .failed, .timedOut: events.append(.finished(task))
            case .cancelled where task.errorCode == "confirmation_expired": events.append(.finished(task))
            default: continue
            }
        }
        // Drop entries for tasks that no longer exist so the dictionary stays small.
        let live = Set(tasks.map { $0.id.uuidString })
        seen = seen.filter { live.contains($0.key) }
        notified = seen
        return events
    }

    /// Marks a task as already known in its current state (e.g. the keyboard showed the card itself).
    public func markNotified(_ task: AgentTask) {
        var seen = notified
        seen[task.id.uuidString] = task.rawStatus
        notified = seen
    }

    public func reset() { defaults.removeObject(forKey: key) }

    /// The task the keyboard most recently started, so the app can open it directly.
    public var activeTaskID: UUID? {
        get { defaults.string(forKey: Self.activeTaskKey).flatMap(UUID.init) }
        set { if let id = newValue { defaults.set(id.uuidString, forKey: Self.activeTaskKey) } else { defaults.removeObject(forKey: Self.activeTaskKey) } }
    }
}
