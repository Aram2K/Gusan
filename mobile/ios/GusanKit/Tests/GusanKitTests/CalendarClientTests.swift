import XCTest
@testable import GusanKit

final class CalendarClientTests: XCTestCase {
    private final class Transport: HTTPTransport {
        var requests: [URLRequest] = []
        var status = 200
        var response = "{}"
        func data(for request: URLRequest) async throws -> (Data, URLResponse) {
            requests.append(request)
            return (Data(response.utf8), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!)
        }
    }
    private func body(_ request: URLRequest?) throws -> [String: Any] {
        try XCTUnwrap(JSONSerialization.jsonObject(with: XCTUnwrap(request?.httpBody)) as? [String: Any])
    }
    func testActionsAndParsing() async throws {
        let transport = Transport(), start = Date(timeIntervalSince1970: 1794182400)
        let client = CalendarClient(tokenProvider: { "jwt" }, transport: transport)
        transport.response = #"{"url":"https://accounts.google.com/o/oauth2/v2/auth?state=test"}"#
        let url = try await client.start()
        XCTAssertEqual(url.host, "accounts.google.com")
        XCTAssertEqual(try body(transport.requests.last)["action"] as? String, "google_start")
        XCTAssertEqual(transport.requests.last?.value(forHTTPHeaderField: "Authorization"), "Bearer jwt")
        XCTAssertEqual(transport.requests.last?.url, CalendarClient.functionURL)
        transport.response = #"{"google":{"connected":true}}"#
        let connected = try await client.status()
        XCTAssertTrue(connected)
        transport.response = #"{"blocks":[{"start":"2026-11-09T10:00:00.000Z","end":"2026-11-09T11:00:00Z","all_day":false,"title":"Private"},{"start":"2026-11-10","end":"2026-11-11","all_day":true,"title":"Day"}]}"#
        let blocks = try await client.busy(start: start, end: start.addingTimeInterval(86400), includeTitles: false, timeZone: TimeZone(secondsFromGMT: 3600)!)
        XCTAssertEqual(blocks.count, 2)
        XCTAssertNil(blocks.first?.title)
        XCTAssertTrue(blocks[1].isAllDay)
        XCTAssertEqual(blocks[1].start, ISO8601DateFormatter().date(from: "2026-11-09T23:00:00Z"))
        XCTAssertEqual(try body(transport.requests.last)["include_titles"] as? Bool, false)
        let titled = try await client.busy(start: start, end: start.addingTimeInterval(86400), includeTitles: true)
        XCTAssertEqual(titled.first?.title, "Private")
        transport.response = #"{"blocks":[]}"#
        let empty = try await client.busy(start: start, end: start.addingTimeInterval(86400), includeTitles: true)
        XCTAssertTrue(empty.isEmpty)
        transport.response = #"{"ok":true}"#
        try await client.disconnect()
        XCTAssertEqual(try body(transport.requests.last)["action"] as? String, "disconnect")
    }
    func testErrorsAndMalformedResponses() async throws {
        let transport = Transport()
        let service = CalendarClient(tokenProvider: { "jwt" }, transport: transport)
        for (status, code) in [(503, "google_not_configured"), (409, "google_reconnect"), (401, "unauthorized")] {
            transport.status = status; transport.response = "{\"error\":\"\(code)\"}"
            do { _ = try await service.status(); XCTFail("Expected error") }
            catch let error as CalendarClientError { XCTAssertEqual(error.code, code); XCTAssertEqual(error.status, status) }
        }
        transport.status = 200
        for response in ["{}", #"{"url":"https://evil.example"}"#] {
            transport.response = response
            do { _ = try await service.start(); XCTFail("Expected invalid response") } catch {}
        }
        transport.response = #"{"blocks":[{"start":"invalid","end":"invalid","all_day":false}]}"#
        do { _ = try await service.busy(start: Date(), end: Date().addingTimeInterval(60), includeTitles: false); XCTFail("Expected invalid dates") } catch {}
    }
    func testCallbackRouting() {
        XCTAssertTrue(CalendarClient.callbackConnected(URL(string: "gusan://calendar?google=connected")!))
        XCTAssertFalse(CalendarClient.callbackConnected(URL(string: "gusan://auth-callback?google=connected")!))
        XCTAssertFalse(CalendarClient.callbackConnected(URL(string: "gusan://calendar?google=error&reason=scope")!))
        XCTAssertFalse(CalendarClient.callbackConnected(URL(string: "https://calendar?google=connected")!))
    }
}
