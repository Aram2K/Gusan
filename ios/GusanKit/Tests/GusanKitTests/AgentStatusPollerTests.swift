import XCTest
@testable import GusanKit

@MainActor
final class AgentStatusPollerTests: XCTestCase {
    private final class Service: AgentService {
        private let lock = NSLock()
        private var active = 0
        private var peak = 0
        private var requests: [(UUID, Int)] = []
        let responses: [UUID: AgentTask]
        let failing: Set<UUID>

        init(tasks: [AgentTask], failing: Set<UUID> = []) {
            responses = Dictionary(uniqueKeysWithValues: tasks.map { ($0.id, $0) })
            self.failing = failing
        }

        var calls: [(UUID, Int)] { locked { requests } }
        var maximumActive: Int { locked { peak } }

        func status(_ id: UUID, waitSeconds: Int) async throws -> AgentTask {
            locked {
                active += 1; peak = max(peak, active)
                requests.append((id, waitSeconds))
            }
            defer { locked { active -= 1 } }
            try await Task.sleep(nanoseconds: 30_000_000)
            if failing.contains(id) { throw AgentError.code("upstream_error", status: 502) }
            guard let task = responses[id] else { throw AgentError.notFound }
            return task
        }

        func start(_ intent: AgentIntent, context: String?) async throws -> AgentTask { throw AgentError.notFound }
        func confirm(_ id: UUID) async throws -> AgentTask { throw AgentError.notFound }
        func cancel(_ id: UUID) async throws -> AgentTask { throw AgentError.notFound }
        func continueTask(_ id: UUID, message: String) async throws -> AgentTask { throw AgentError.notFound }
        func stop(_ id: UUID) async throws -> AgentTask { throw AgentError.notFound }
        func delete(_ id: UUID) async throws {}
        func list(limit: Int) async throws -> [AgentTask] { [] }
        func markSeen(_ id: UUID) async throws {}

        private func locked<T>(_ operation: () -> T) -> T {
            lock.lock(); defer { lock.unlock() }; return operation()
        }
    }

    func testRefreshPollsListedAndMissingActiveTasksWithBoundedConcurrency() async {
        let states: [AgentStatus] = [.queued, .running, .awaitingConfirmation, .awaitingLogin]
        let active = (0..<8).map { AgentTask(mode: .research, status: states[$0 % states.count], title: "Task \($0)") }
        let terminal = AgentTask(mode: .research, status: .completed, title: "Old result")
        let completed = active.map { task -> AgentTask in
            var result = task; result.status = .completed
            result.result = AgentResult(markdown: "Fresh result")
            return result
        }
        let service = Service(tasks: completed)
        let poller = AgentStatusPoller()
        let result = await poller.refresh(listed: Array(active.prefix(7)) + [terminal], known: active, service: service)
        XCTAssertTrue(result.failedIDs.isEmpty)
        XCTAssertEqual(result.tasks.count, 9)
        XCTAssertTrue(result.tasks.allSatisfy { $0.status == .completed })
        XCTAssertEqual(result.tasks.first?.result?.markdown, "Fresh result")
        XCTAssertEqual(Set(service.calls.map { $0.0 }), Set(active.map(\.id)))
        XCTAssertEqual(service.calls.count, 8)
        XCTAssertTrue(service.calls.allSatisfy { $0.1 == 0 })
        XCTAssertLessThanOrEqual(service.maximumActive, 3)
        XCTAssertGreaterThan(service.maximumActive, 1)
    }

    func testEmptyAndTerminalOnlyRefreshDoNotPoll() async {
        let service = Service(tasks: [])
        let poller = AgentStatusPoller()
        let empty = await poller.refresh(listed: [], known: [], service: service)
        XCTAssertTrue(empty.tasks.isEmpty); XCTAssertTrue(empty.failedIDs.isEmpty)
        let terminal = AgentTask(mode: .research, status: .cancelled, title: "Stopped")
        let result = await poller.refresh(listed: [terminal], known: [terminal], service: service)
        XCTAssertEqual(result.tasks, [terminal])
        XCTAssertTrue(service.calls.isEmpty)
    }

    func testOneFailedStatusKeepsItsSnapshotAndMergesOtherResults() async {
        let first = AgentTask(mode: .research, status: .queued, title: "First")
        let second = AgentTask(mode: .research, status: .running, title: "Second")
        var finished = second; finished.status = .completed
        let service = Service(tasks: [first, finished], failing: [first.id])
        let result = await AgentStatusPoller().refresh(listed: [first, second], known: [], service: service)
        XCTAssertEqual(result.failedIDs, [first.id])
        XCTAssertEqual(result.tasks, [first, finished])
    }

    func testOverlappingRefreshAndLoadShareTheSameStatusRequest() async throws {
        let task = AgentTask(mode: .research, status: .running, title: "Running")
        let service = Service(tasks: [task])
        let poller = AgentStatusPoller()
        async let refresh = poller.refresh(listed: [task], known: [], service: service)
        async let load = poller.status(task.id, service: service)
        let (result, loaded) = try await (refresh, load)
        XCTAssertEqual(result.tasks, [task]); XCTAssertEqual(loaded, task)
        XCTAssertEqual(service.calls.count, 1)
    }

    func testFailedFlightCanBeRetried() async {
        let task = AgentTask(mode: .research, status: .queued, title: "Queued")
        let service = Service(tasks: [task], failing: [task.id])
        let poller = AgentStatusPoller()
        for _ in 0..<2 {
            let result = await poller.refresh(listed: [task], known: [], service: service)
            XCTAssertEqual(result.failedIDs, [task.id])
        }
        XCTAssertEqual(service.calls.count, 2)
    }
}
