import XCTest
import GusanKit

final class JobRunnerTests: XCTestCase {
    private struct ExternalJob: GusanJob {
        let result: JobResult?
        let error: Error?
        func run() -> AsyncThrowingStream<JobEvent, Error> {
            AsyncThrowingStream {
                $0.yield(.progress("Working"))
                if let result { $0.yield(.result(result)) }
                $0.finish(throwing: error)
            }
        }
    }
    private struct ConfirmedJob: GusanJob {
        let request: JobConfirmation
        func run() -> AsyncThrowingStream<JobEvent, Error> {
            AsyncThrowingStream { continuation in
                let task = Task {
                    do {
                        continuation.yield(.needsConfirmation(request))
                        guard try await request.response() else { throw CancellationError() }
                        try Task.checkCancellation()
                        continuation.yield(.result(JobResult(text: "Confirmed", kind: .insert)))
                        continuation.finish()
                    } catch { continuation.finish(throwing: error) }
                }
                continuation.onTermination = { _ in task.cancel() }
            }
        }
    }
    private actor Progress {
        var messages: [String] = []
        func append(_ message: String) { messages.append(message) }
    }
    func testExternalJobProgressAndCardWithoutInsertableText() async throws {
        let card = JobCard(title: "Completed", body: "Review the outcome")
        let job = ExternalJob(result: JobResult(card: card), error: nil)
        let progress = Progress()
        let result = try await JobRunner().result(of: job, progress: { await progress.append($0) })
        XCTAssertEqual(result.kind, .cardOnly); XCTAssertEqual(result.card, card); XCTAssertEqual(result.text, "")
        let messages = await progress.messages
        XCTAssertEqual(messages, ["Working"])
    }
    func testCardCodableAndExistingTextResultCompatibility() throws {
        let result = JobResult(card: JobCard(title: "Done", body: "Review first", insertableText: "Insert this"))
        XCTAssertEqual(try JSONDecoder().decode(JobResult.self, from: JSONEncoder().encode(result)), result)
        let old = try JSONDecoder().decode(JobResult.self, from: Data(#"{"text":"Text","kind":"insert"}"#.utf8))
        XCTAssertNil(old.card); XCTAssertEqual(old.kind, .insert)
    }
    func testConfirmationAcceptsOnlyOneDecision() async throws {
        let request = JobConfirmation(prompt: "Proceed?")
        let result = try await JobRunner().result(of: ConfirmedJob(request: request), confirmation: {
            XCTAssertEqual($0.prompt, "Proceed?")
            return true
        })
        XCTAssertEqual(result.text, "Confirmed")
        await request.resolve(false)
        let accepted = try await request.response()
        XCTAssertTrue(accepted)
    }
    func testDefaultAndExplicitDeclineCancelTheJob() async throws {
        for explicit in [false, true] {
            let request = JobConfirmation(prompt: "Proceed?")
            do {
                let handler: (@Sendable (JobConfirmation) async throws -> Bool)? = explicit ? { _ in false } : nil
                _ = try await JobRunner().result(of: ConfirmedJob(request: request), confirmation: handler)
                XCTFail("Declining must cancel")
            } catch is CancellationError {} catch { XCTFail("Unexpected error: \(error)") }
            let accepted = try await request.response()
            XCTAssertFalse(accepted)
        }
    }
    func testCancellationUnblocksConfirmation() async {
        let request = JobConfirmation(prompt: "Proceed?")
        let task = Task { try await request.response() }
        task.cancel()
        do { _ = try await task.value; XCTFail("Cancellation expected") }
        catch is CancellationError {} catch { XCTFail("Unexpected error: \(error)") }
    }
    func testEmptyAndErrorStreams() async {
        enum Failure: Error { case expected }
        do { _ = try await ExternalJob(result: nil, error: nil).result(); XCTFail("Empty stream") }
        catch { XCTAssertEqual(error as? GusanError, .invalidResponse) }
        do { _ = try await ExternalJob(result: nil, error: Failure.expected).result(); XCTFail("Producer error") }
        catch { XCTAssertTrue(error is Failure) }
    }
}
