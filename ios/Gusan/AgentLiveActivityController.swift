import ActivityKit
import Foundation
import GusanKit

@available(iOS 16.1, *)
@MainActor
final class AgentLiveActivityController {
    private typealias AgentActivity = Activity<AgentActivityAttributes>
    private typealias State = AgentActivityAttributes.ContentState
    private var activities: [UUID: AgentActivity] = [:]
    private var observers: [UUID: Task<Void, Never>] = [:]
    private var workers: [UUID: Task<Void, Never>] = [:]
    private var pending: [UUID: State] = [:]
    private var sent: [UUID: State] = [:]
    private var sentAt: [UUID: Date] = [:]
    private var failedAt: [UUID: Date] = [:]
    private let defaults: UserDefaults
    private let startedKey = "gusan.agent.activities.started"
    private var startedIDs: [String]

    init(defaults: UserDefaults) {
        self.defaults = defaults
        startedIDs = defaults.stringArray(forKey: startedKey) ?? []
        for activity in AgentActivity.activities {
            attach(activity)
            remember(activity.attributes.taskID)
        }
    }

    func synchronize(_ tasks: [AgentTask], sessions: AgentSessionStore) {
        for task in tasks {
            let state = Self.state(for: task)
            if activities[task.id] == nil {
                guard !task.status.isTerminal,
                      !startedIDs.contains(task.id.uuidString),
                      ActivityAuthorizationInfo().areActivitiesEnabled,
                      Date().timeIntervalSince(failedAt[task.id] ?? .distantPast) >= 60 else { continue }
                let attributes = AgentActivityAttributes(sessionID: sessions.sessionID(for: task.id), taskID: task.id, mode: task.mode.rawValue, title: String(task.title.prefix(120)))
                do {
                    let activity: AgentActivity
                    if #available(iOS 16.2, *) {
                        activity = try AgentActivity.request(attributes: attributes, content: ActivityContent(state: state, staleDate: nil), pushType: nil)
                    } else {
                        activity = try AgentActivity.request(attributes: attributes, contentState: state, pushType: nil)
                    }
                    attach(activity); remember(task.id)
                    sent[task.id] = state; sentAt[task.id] = Date()
                } catch { failedAt[task.id] = Date() }
            } else {
                enqueue(state, for: task.id)
            }
        }
    }

    func endFinishedSession(_ sessionID: UUID) {
        for (taskID, activity) in activities where activity.attributes.sessionID == sessionID || taskID == sessionID {
            let state = pending[taskID] ?? sent[taskID] ?? Self.currentState(activity)
            if state.isFinished { end(activity, state: state) }
        }
    }

    func dismissAll() {
        for activity in Array(activities.values) { end(activity, state: pending[activity.attributes.taskID] ?? Self.currentState(activity)) }
    }

    private func attach(_ activity: AgentActivity) {
        let id = activity.attributes.taskID
        activities[id] = activity
        sent[id] = Self.currentState(activity)
        observers[id]?.cancel()
        observers[id] = Task { [weak self] in
            for await state in activity.activityStateUpdates {
                guard !Task.isCancelled else { return }
                if state == .dismissed || state == .ended {
                    self?.forget(id)
                    return
                }
            }
        }
    }

    private func remember(_ id: UUID) {
        guard !startedIDs.contains(id.uuidString) else { return }
        startedIDs = Array((startedIDs + [id.uuidString]).suffix(200))
        defaults.set(startedIDs, forKey: startedKey)
    }

    private func enqueue(_ state: State, for id: UUID) {
        guard pending[id] != state, sent[id] != state else { return }
        pending[id] = state
        guard workers[id] == nil else { return }
        workers[id] = Task { [weak self] in
            guard let self else { return }
            defer { self.workers[id] = nil }
            while let next = self.pending[id], let activity = self.activities[id], !Task.isCancelled {
                let delay = next.phase == self.sent[id]?.phase ? max(0, 3 - Date().timeIntervalSince(self.sentAt[id] ?? .distantPast)) : 0
                if delay > 0 {
                    do { try await Task.sleep(nanoseconds: UInt64(delay * 1_000_000_000)) } catch { return }
                }
                guard !Task.isCancelled, let latest = self.pending.removeValue(forKey: id) else { return }
                if #available(iOS 16.2, *) {
                    await activity.update(ActivityContent(state: latest, staleDate: nil))
                } else {
                    await activity.update(using: latest)
                }
                self.sent[id] = latest; self.sentAt[id] = Date()
            }
        }
    }

    private func end(_ activity: AgentActivity, state: State) {
        forget(activity.attributes.taskID)
        Task {
            if #available(iOS 16.2, *) {
                await activity.end(ActivityContent(state: state, staleDate: nil), dismissalPolicy: .immediate)
            } else {
                await activity.end(using: state, dismissalPolicy: .immediate)
            }
        }
    }

    private func forget(_ id: UUID) {
        activities[id] = nil; pending[id] = nil; sent[id] = nil; sentAt[id] = nil
        workers.removeValue(forKey: id)?.cancel()
        observers.removeValue(forKey: id)?.cancel()
    }

    private static func currentState(_ activity: AgentActivity) -> State {
        if #available(iOS 16.2, *) { return activity.content.state }
        return activity.contentState
    }

    private static func state(for task: AgentTask) -> State {
        let phase: String
        switch task.phase {
        case .queued, .working: phase = "working"
        case .needsConfirmation: phase = "confirm"
        case .needsLogin: phase = "login"
        case .needsCode: phase = "code"
        case .completed: phase = "done"
        case .failed, .timedOut: phase = "failed"
        case .stopped, .cancelled: phase = "stopped"
        }
        return State(phase: phase, latestStep: task.latestStep ?? "", stepCount: task.steps.count, resultPreview: task.result?.plainText ?? "", updatedAt: task.updatedAt ?? task.createdAt)
    }
}
