import Foundation

public actor JobConfirmation: Equatable {
    public nonisolated let id = UUID()
    public nonisolated let prompt: String
    private var decision: Bool?
    private var waiters: [CheckedContinuation<Bool, Never>] = []
    public init(prompt: String) { self.prompt = prompt }
    public nonisolated static func == (lhs: JobConfirmation, rhs: JobConfirmation) -> Bool { lhs.id == rhs.id }
    public func resolve(_ accepted: Bool) {
        guard decision == nil else { return }
        decision = accepted
        let pending = waiters; waiters.removeAll()
        pending.forEach { $0.resume(returning: accepted) }
    }
    public func response() async throws -> Bool {
        let accepted: Bool = await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                if let decision { continuation.resume(returning: decision) }
                else { waiters.append(continuation) }
            }
        } onCancel: {
            Task { await self.resolve(false) }
        }
        try Task.checkCancellation()
        return accepted
    }
}

public struct JobRunner: Sendable {
    public init() {}
    public func events(for job: any GusanJob) -> AsyncThrowingStream<JobEvent, Error> { job.run() }
    public func result(of job: any GusanJob, progress: @Sendable (String) async -> Void = { _ in }, confirmation: (@Sendable (JobConfirmation) async throws -> Bool)? = nil) async throws -> JobResult {
        for try await event in events(for: job) {
            try Task.checkCancellation()
            switch event {
            case .progress(let message): await progress(message)
            case .needsConfirmation(let request):
                do {
                    guard let confirmation else { throw CancellationError() }
                    let accepted = try await confirmation(request)
                    try Task.checkCancellation()
                    await request.resolve(accepted)
                    if !accepted { throw CancellationError() }
                } catch {
                    await request.resolve(false)
                    throw error
                }
            case .result(let result): return result
            }
        }
        try Task.checkCancellation()
        throw GusanError.invalidResponse
    }
}
