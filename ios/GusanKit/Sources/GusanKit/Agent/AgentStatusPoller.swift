import Foundation

@MainActor
public final class AgentStatusPoller {
    public struct Refresh {
        public let tasks: [AgentTask]
        public let failedIDs: [UUID]
    }

    private struct Flight {
        let token: UUID
        let task: Task<AgentTask, Error>
    }

    private let limit: Int
    private var active = 0
    private var waiters: [CheckedContinuation<Void, Never>] = []
    private var flights: [UUID: Flight] = [:]

    public init(limit: Int = 3) { self.limit = max(1, limit) }

    public func reset() {
        for flight in flights.values { flight.task.cancel() }
        flights.removeAll()
    }

    public func status(_ id: UUID, service: AgentService) async throws -> AgentTask {
        try Task.checkCancellation()
        if let flight = flights[id] { return try await flight.task.value }
        let token = UUID()
        let worker = Task { @MainActor in
            await acquire()
            defer { release() }
            try Task.checkCancellation()
            return try await service.status(id, waitSeconds: 0)
        }
        flights[id] = Flight(token: token, task: worker)
        defer { if flights[id]?.token == token { flights[id] = nil } }
        return try await worker.value
    }

    public func refresh(listed: [AgentTask], known: [AgentTask], service: AgentService) async -> Refresh {
        var seen = Set<UUID>()
        let tasks = (listed + known.filter { !$0.status.isTerminal }).filter { seen.insert($0.id).inserted }
        var updates: [UUID: AgentTask] = [:]
        var failedIDs: [UUID] = []
        await withTaskGroup(of: (UUID, AgentTask?).self) { group in
            for task in tasks where !task.status.isTerminal {
                group.addTask { [self] in
                    (task.id, try? await status(task.id, service: service))
                }
            }
            for await (id, task) in group {
                if let task { updates[id] = task }
                else { failedIDs.append(id) }
            }
        }
        return Refresh(tasks: tasks.map { updates[$0.id] ?? $0 }, failedIDs: failedIDs)
    }

    private func acquire() async {
        if active < limit { active += 1; return }
        await withCheckedContinuation { waiters.append($0) }
    }

    private func release() {
        if waiters.isEmpty { active -= 1 }
        else { waiters.removeFirst().resume() }
    }
}
