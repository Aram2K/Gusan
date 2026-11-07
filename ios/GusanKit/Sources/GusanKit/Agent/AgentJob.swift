import Foundation

/// The agent task type for the shared job pipeline: polls an `AgentService` and turns task state
/// into `JobEvent`s. Progress steps become `.progress`, an awaiting-confirmation task becomes
/// `.needsConfirmation` (resolving it confirms or cancels on the server), and a finished task
/// becomes a `.result` card whose `insertableText` is the plain-text answer.
public struct AgentJob: GusanJob {
    public enum Start { case new(AgentIntent, context: String?), resume(UUID) }

    private let service: AgentService
    private let start: Start
    private let interval: UInt64
    private let onTask: (@Sendable (AgentTask) -> Void)?

    /// - Parameters:
    ///   - interval: pause between polls in nanoseconds (the live client also long-polls server-side).
    ///   - onTask: every fresh task snapshot, for UIs that show more than the card (status, task id, mode).
    public init(service: AgentService, start: Start, interval: UInt64 = 1_500_000_000, onTask: (@Sendable (AgentTask) -> Void)? = nil) {
        self.service = service; self.start = start; self.interval = interval; self.onTask = onTask
    }

    public func run() -> AsyncThrowingStream<JobEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var current: AgentTask
                    switch start {
                    case .new(let intent, let context):
                        continuation.yield(.progress(intent.mode == .research ? "Starting research…" : "Starting the task…"))
                        current = try await service.start(intent, context: context)
                    case .resume(let id):
                        current = try await service.status(id, waitSeconds: 0)
                    }
                    onTask?(current)
                    var reportedSteps = 0
                    var askedFor: String?
                    while !current.status.isTerminal {
                        try Task.checkCancellation()
                        if current.steps.count > reportedSteps, let step = current.latestStep {
                            reportedSteps = current.steps.count
                            continuation.yield(.progress(step))
                        }
                        if current.status == .awaitingConfirmation, let confirmation = current.confirmation, askedFor != confirmation.summary {
                            askedFor = confirmation.summary
                            let request = JobConfirmation(prompt: confirmation.summary)
                            continuation.yield(.needsConfirmation(request))
                            let accepted = try await request.response()
                            current = accepted ? try await service.confirm(current.id) : try await service.cancel(current.id)
                            onTask?(current)
                            continue
                        }
                        let waiting = current.status == .awaitingConfirmation
                        try await Task.sleep(nanoseconds: waiting ? max(interval, 5_000_000_000) : interval)
                        let fresh = try await service.status(current.id, waitSeconds: waiting ? 0 : 10)
                        if fresh != current { current = fresh; onTask?(current) }
                    }
                    switch current.status {
                    case .completed:
                        let result = current.result ?? AgentResult(markdown: current.outcome ?? "Finished")
                        let plain = result.plainText
                        continuation.yield(.result(JobResult(card: JobCard(title: current.title, body: result.markdown, insertableText: plain.isEmpty ? nil : plain))))
                        continuation.finish()
                    case .cancelled:
                        continuation.finish(throwing: CancellationError())
                    default:
                        continuation.finish(throwing: AgentError.code(current.errorCode ?? current.status.rawValue, status: 0))
                    }
                } catch { continuation.finish(throwing: error) }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
