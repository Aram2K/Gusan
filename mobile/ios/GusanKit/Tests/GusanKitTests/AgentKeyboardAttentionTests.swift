import XCTest
@testable import GusanKit

final class AgentKeyboardAttentionTests: XCTestCase {
    func testLoginAndCodeUseSupportedSessionDeepLink() {
        var task = AgentTask(mode: .research, status: .awaitingLogin, title: "Flight")
        let login = AgentKeyboardAttention.resolve(task)
        XCTAssertEqual(login?.text, "Needs your login")
        XCTAssertEqual(login?.url.absoluteString, "gusan://session/\(task.id.uuidString.lowercased())")
        task.setServerStatus("awaiting_code")
        XCTAssertEqual(task.status, .running)
        XCTAssertEqual(AgentKeyboardAttention.resolve(task)?.text, "Needs a code")
        XCTAssertEqual(AgentKeyboardAttention.resolve(task)?.url, login?.url)
    }
    func testOtherAndUnknownStatesDoNotHijackStatusTaps() {
        for status in [AgentStatus.queued, .running, .awaitingConfirmation, .completed, .failed, .cancelled, .timedOut] {
            let task = AgentTask(mode: .research, status: status, title: "")
            XCTAssertNil(AgentKeyboardAttention.resolve(task))
        }
        var task = AgentTask(mode: .research, status: .running, title: "")
        task.setServerStatus("future_status")
        XCTAssertNil(AgentKeyboardAttention.resolve(task))
    }
}
