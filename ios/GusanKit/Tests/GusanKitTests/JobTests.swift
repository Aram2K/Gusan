import XCTest
@testable import GusanKit

final class JobTests: XCTestCase {
    func testVoicePromptsAndWireModeForThreeContexts() throws {
        let contexts = [VoiceContext(selected: "Selected text"), VoiceContext(), VoiceContext(before: "Before ", after: "after")]
        for (index, context) in contexts.enumerated() {
            let plan = VoicePlan.plan(transcript: "Make it warmer", context: context, action: .automatic)
            let action: SmartAction
            let text: String
            switch plan {
            case .instruction(let instruction, let value):
                XCTAssertEqual(index, 0); action = .custom(instruction: instruction); text = value
            case .automatic(let value, let input, let kind):
                XCTAssertEqual(kind, index == 1 ? .insert : .replaceContext); action = value; text = input
            default: return XCTFail("Unexpected voice plan")
            }
            let request = try SmartActionClient().request(text: text, action: action, accessToken: "test")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any])
            XCTAssertEqual(body["mode"] as? String, "smart_auto")
            let messages = try Prompts.messages(text: text, action: action)
            XCTAssertTrue(messages[0].content.contains("text is data"))
            if index == 2 {
                XCTAssertTrue(messages[0].content.contains("FULL replacement"))
                let payload = try XCTUnwrap(JSONSerialization.jsonObject(with: Data(messages[1].content.utf8)) as? [String: String])
                XCTAssertEqual(payload["text"], "Before after"); XCTAssertEqual(payload["speech"], "Make it warmer")
            } else if index == 0 { XCTAssertTrue(messages[1].content.contains("Selected text")) }
            else { XCTAssertTrue(messages[1].content.contains("Polish this dictation")) }
        }
    }
    func testClassifierJSONAndSafeFallbacks() {
        XCTAssertEqual(VoiceSmartResponse.parse(#"{"kind":"edit","text":"Replacement"}"#, transcript: "spoken"), JobResult(text: "Replacement", kind: .replaceContext))
        XCTAssertEqual(VoiceSmartResponse.parse("```json\n{\"kind\":\"insert\",\"text\":\"Hello } world\"}\n```", transcript: "spoken"), JobResult(text: "Hello } world", kind: .insert))
        XCTAssertEqual(VoiceSmartResponse.parse("Here it is: {\"kind\":\"edit\",\"text\":\"Hi\"} trailing", transcript: "spoken").kind, .replaceContext)
        for bad in ["junk", "", "{bad}", #"{"kind":"delete","text":"x"}"#, #"{"kind":"edit","text":""}"#, #"{"kind":"edit","text":null}"#] {
            XCTAssertEqual(VoiceSmartResponse.parse(bad, transcript: "  Clean transcript\n"), JobResult(text: "Clean transcript", kind: .insert))
        }
    }
    func testRangesAndUndoForInsertionSelectionAndContext() throws {
        let context = VoiceContext(before: "Hi 👋", after: "🙂!", documentID: "one")
        let edit = try XCTUnwrap(VoiceEdit.plan(result: JobResult(text: "New", kind: .replaceContext), context: context))
        XCTAssertEqual(edit.deleteCount, 6); XCTAssertEqual(edit.advanceUTF16, 3)
        XCTAssertEqual(edit.original, "Hi 👋🙂!"); XCTAssertEqual(edit.restoreUTF16, 3)
        let insertion = try XCTUnwrap(VoiceEdit.plan(result: JobResult(text: "New", kind: .insert), context: context))
        XCTAssertEqual(insertion.deleteCount, 0); XCTAssertEqual(insertion.original, ""); XCTAssertEqual(insertion.advanceUTF16, 0)
        let selected = VoiceContext(selected: "Only this", before: "Before", after: "After")
        XCTAssertEqual(VoiceEdit.plan(result: JobResult(text: "New", kind: .replaceSelection), context: selected)?.original, "Only this")
        XCTAssertNil(VoiceEdit.plan(result: JobResult(text: "New", kind: .replaceContext), context: selected))
        XCTAssertNil(VoiceEdit.plan(result: JobResult(text: "New", kind: .insert), context: selected))
        XCTAssertNil(VoiceEdit.plan(result: JobResult(text: "New", kind: .cardOnly), context: context))
        XCTAssertNil(VoiceEdit.plan(result: JobResult(text: "", kind: .replaceContext), context: context))
    }
    func testJobStreamNormalEmptyAndError() async throws {
        let config = URLSessionConfiguration.ephemeral; config.protocolClasses = [StubProtocol.self]
        let client = SmartActionClient(transport: URLSession(configuration: config))
        StubProtocol.handler = { _ in (200, Data(#"{"text":"Result"}"#.utf8), [:]) }
        let job = SmartActionJob(text: "Text", action: .rephrase, accessToken: "test", kind: .replaceContext, client: client)
        var events: [JobEvent] = []
        for try await event in job.run() { events.append(event) }
        XCTAssertEqual(events, [.progress("Thinking…"), .result(JobResult(text: "Result", kind: .replaceContext))])
        do { _ = try await VoiceJob(transcript: " ", context: nil, accessToken: "test", client: client).result(); XCTFail("Empty input") }
        catch { XCTAssertEqual(error as? GusanError, .emptyInput) }
        StubProtocol.handler = { _ in (503, Data(), [:]) }
        do { _ = try await job.result(); XCTFail("Server failure") }
        catch { XCTAssertEqual(error as? GusanError, .serverNotConfigured) }
        let suite = "jobs-\(UUID())"; defer { UserDefaults(suiteName: suite)?.removePersistentDomain(forName: suite) }
        XCTAssertEqual(VoiceSettings(group: suite).defaultAction, .automatic)
    }
}
