import XCTest
@testable import GusanKit

final class CancellationTests: XCTestCase {
    struct FailingTransport: HTTPTransport {
        let error: Error
        func data(for request: URLRequest) async throws -> (Data, URLResponse) { throw error }
    }
    func testCancellationIsNeverANetworkError() async {
        for error in [URLError(.cancelled), CancellationError()] as [Error] {
            let transport = FailingTransport(error: error)
            do {
                _ = try await SmartActionClient(transport: transport).run(text: "hello", action: .rephrase, accessToken: "fake")
                XCTFail("Expected cancellation")
            } catch { XCTAssertTrue(error is CancellationError) }
            let store = MemoryStore()
            do {
                _ = try await AuthClient(store: store, transport: transport).signIn(email: "fake", password: "fake")
                XCTFail("Expected cancellation")
            } catch { XCTAssertTrue(error is CancellationError) }
            XCTAssertNil(store.read())
        }
    }
    func testAlreadyCancelledTaskDoesNotBecomeNetworkError() async {
        let task = Task {
            withUnsafeCurrentTask { $0?.cancel() }
            return try await SmartActionClient(transport: FailingTransport(error: URLError(.notConnectedToInternet))).run(text: "hello", action: .rephrase, accessToken: "fake")
        }
        do { _ = try await task.value; XCTFail("Expected cancellation") }
        catch { XCTAssertTrue(error is CancellationError) }
    }
}
