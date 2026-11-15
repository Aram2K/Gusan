import XCTest
@testable import GusanKit

final class StubProtocol: URLProtocol {
    static var handler: ((URLRequest) throws -> (Int, Data, [String: String]))!
    override class func canInit(with request: URLRequest) -> Bool { true }
    override class func canonicalRequest(for request: URLRequest) -> URLRequest { request }
    override func startLoading() {
        do {
            let (status, data, headers) = try Self.handler(request)
            client?.urlProtocol(self, didReceive: HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: headers)!, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch { client?.urlProtocol(self, didFailWithError: error) }
    }
    override func stopLoading() {}
}

final class MemoryStore: SessionStorage {
    var session: Session?
    func read() -> Session? { session }
    func write(_ session: Session) { self.session = session }
    func clear() { session = nil }
}

final class GusanKitTests: XCTestCase {
    func testPrivateKeychainFallbackPolicy() {
        XCTAssertFalse(SessionStore.sharedBackendAvailable(group: nil, keychainOK: true, fileOK: true))
        XCTAssertFalse(SessionStore.sharedBackendAvailable(group: "group.gusan", keychainOK: false, fileOK: false))
        XCTAssertTrue(SessionStore.sharedBackendAvailable(group: "group.gusan", keychainOK: true, fileOK: false))
        XCTAssertTrue(SessionStore.sharedBackendAvailable(group: "group.gusan", keychainOK: false, fileOK: true))
    }
    func transport() -> URLSession {
        let config = URLSessionConfiguration.ephemeral
        config.protocolClasses = [StubProtocol.self]
        return URLSession(configuration: config)
    }
    func json(_ value: Any) throws -> Data { try JSONSerialization.data(withJSONObject: value) }
    func requestBody(_ request: URLRequest) -> Data {
        if let data = request.httpBody { return data }
        guard let stream = request.httpBodyStream else { return Data() }
        stream.open(); defer { stream.close() }
        var data = Data()
        var buffer = [UInt8](repeating: 0, count: 1024)
        while stream.hasBytesAvailable {
            let count = stream.read(&buffer, maxLength: buffer.count)
            if count <= 0 { break }
            data.append(contentsOf: buffer.prefix(count))
        }
        return data
    }
    func response(_ status: Int, family: String = "") -> HTTPURLResponse {
        HTTPURLResponse(url: SmartActionClient.url, statusCode: status, httpVersion: nil, headerFields: ["X-Answer-Family": family])!
    }
    func testRequestsAndPrompts() throws {
        for mode in SmartActionMode.allCases {
            let request = try SmartActionClient().request(text: "  hello \n", mode: mode, accessToken: "token", target: "hy")
            XCTAssertEqual(request.url, SmartActionClient.url)
            XCTAssertEqual(request.httpMethod, "POST")
            XCTAssertEqual(request.timeoutInterval, 45)
            XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer token")
            XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
            let body = try XCTUnwrap(JSONSerialization.jsonObject(with: request.httpBody!) as? [String: Any])
            XCTAssertEqual(body["mode"] as? String, mode.rawValue)
            XCTAssertEqual(body["max_tokens"] as? Int, mode.maxTokens)
            let messages = try XCTUnwrap(body["messages"] as? [[String: String]])
            XCTAssertEqual(messages.last?["content"], Prompts.messages(text: "hello", mode: mode, target: "hy").last?.content)
            XCTAssertNil(body["stream"])
        }
        let smart = Prompts.messages(text: "hi", mode: .smartAuto)
        XCTAssertGreaterThan(smart.count, 10)
        XCTAssertTrue(smart[0].content.contains("voice-to-text executive assistant"))
        XCTAssertEqual(Array(smart.dropFirst().dropLast()).count % 2, 0)
        XCTAssertTrue(Prompts.messages(text: "x", mode: .writeEmail).last!.content.contains("comprehensive, professional email draft"))
        XCTAssertTrue(Prompts.messages(text: "x", mode: .makeTodoList).last!.content.contains("Do not output raw '- [ ]'"))
        XCTAssertTrue(Prompts.messages(text: "x", mode: .summarize).last!.content.contains("Capture all key decisions"))
        for target in Prompts.translateTargets.keys {
            XCTAssertEqual(Prompts.messages(text: "x", mode: .translate, target: target).last?.content, "Translate from auto to \(target). Output only the natural translation.\n\nText:\nx")
        }
        XCTAssertTrue(Prompts.messages(text: "x", mode: .translate, target: "unknown").last!.content.contains("to en."))
        XCTAssertThrowsError(try SmartActionClient().request(text: " \n", mode: .smartAuto, accessToken: "x")) { XCTAssertEqual($0 as? GusanError, .emptyInput) }
    }
    func testResponses() throws {
        XCTAssertEqual(try SmartActionClient.parse(json(["text": " hi "]), response: response(200)), "hi")
        XCTAssertEqual(try SmartActionClient.parse(json(["text": ""]), response: response(200)), "")
        for (status, error) in [(401, GusanError.unauthorized), (402, .proRequired), (403, .proRequired), (429, .dailyLimit), (503, .serverNotConfigured), (502, .failed), (500, .failed), (413, .tooLong), (400, .failed)] {
            XCTAssertThrowsError(try SmartActionClient.parse(Data(), response: response(status))) { XCTAssertEqual($0 as? GusanError, error) }
        }
        XCTAssertThrowsError(try SmartActionClient.parse(Data("bad".utf8), response: response(200)))
        for family in ["deepseek", "qwen"] {
            XCTAssertEqual(try SmartActionClient.parse(json(["text": "reason</think>answer"]), response: response(200, family: family)), "answer")
        }
        XCTAssertThrowsError(try SmartActionClient.parse(json(["text": "<think>unfinished"]), response: response(200))) { XCTAssertEqual($0 as? GusanError, .thoughtOnly) }
        XCTAssertEqual(SmartActionClient.stripThink(" <think>reason</think> answer "), "answer")
        XCTAssertEqual(SmartActionClient.stripThink("reason</think>answer"), "reason</think>answer")
        for literal in ["`</think>`", "```\n</think>\n```", "mention ``` inline `</think>`"] {
            XCTAssertEqual(SmartActionClient.stripThink(literal, templateOpened: true), literal)
        }
        XCTAssertEqual(SmartActionClient.stripThink("```\n</think>\n```\nreason</think>answer", templateOpened: true), "answer")
    }
    func testNetworkAndTimeout() async throws {
        let client = SmartActionClient(transport: transport())
        for (code, expected) in [(URLError.Code.timedOut, GusanError.timeout), (.notConnectedToInternet, .network)] {
            StubProtocol.handler = { _ in throw URLError(code) }
            do { _ = try await client.run(text: "x", mode: .smartAuto, accessToken: "x"); XCTFail() }
            catch { XCTAssertEqual(error as? GusanError, expected) }
        }
        StubProtocol.handler = { _ in XCTFail("Empty input must not send"); throw URLError(.badURL) }
        do { _ = try await client.run(text: "", mode: .smartAuto, accessToken: "x"); XCTFail() }
        catch { XCTAssertEqual(error as? GusanError, .emptyInput) }
    }
    func testProvision() throws {
        let data = try Data(contentsOf: Bundle.module.url(forResource: "profile", withExtension: "mobileprovision")!)
        XCTAssertEqual(AppGroupResolver.provisionGroups(data), ["group.other", "group.resigned.GUSAN"])
        let url = URL(fileURLWithPath: "/shared")
        let resolution = AppGroupResolver.resolve(provision: data, fallback: "fallback") { $0 == "fallback" ? url : nil }
        XCTAssertEqual(resolution.candidatesTried, ["group.resigned.GUSAN", "group.other", "fallback"])
        XCTAssertEqual(resolution.chosen, "fallback")
        XCTAssertEqual(AppGroupResolver.provisionGroups(Data("bad".utf8)), [])
        XCTAssertNil(AppGroupResolver.resolve(provision: nil, fallback: nil) { _ in url }.chosen)
        XCTAssertEqual(AppGroupResolver.resolve(provision: data, fallback: nil) { _ in url }.chosen, "group.resigned.GUSAN")
    }
    func payload() throws -> Data {
        try json(["access_token": "access", "refresh_token": "rotated", "expires_in": 3600, "user": ["id": "id", "user_metadata": ["email": "a@b.c", "email_verified": true]]])
    }
    func testAuthRequestsAndDecoding() async throws {
        let store = MemoryStore()
        let client = AuthClient(store: store, transport: transport())
        let pkce = PKCE(verifier: "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk")
        XCTAssertEqual(pkce.challenge, "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")
        XCTAssertEqual(try PKCE.generate().verifier.count, 43)
        let query = URLComponents(url: AuthClient.authorizeURL(pkce: pkce), resolvingAgainstBaseURL: false)!.queryItems!
        XCTAssertEqual(query.first { $0.name == "redirect_to" }?.value, "gusan://auth-callback")
        XCTAssertEqual(query.first { $0.name == "code_challenge_method" }?.value, "s256")
        let data = try payload()
        var paths: [String] = []
        StubProtocol.handler = { request in
            paths.append(request.url!.absoluteString)
            XCTAssertEqual(request.value(forHTTPHeaderField: "apikey"), AuthClient.publishableKey)
            XCTAssertEqual(request.httpMethod, "POST")
            let body = try JSONSerialization.jsonObject(with: self.requestBody(request)) as! [String: String]
            if request.url!.query == "grant_type=password" { XCTAssertEqual(body, ["email": "a", "password": "b"]) }
            if request.url!.query == "grant_type=pkce" { XCTAssertEqual(body, ["auth_code": "code", "code_verifier": pkce.verifier]) }
            if request.url!.path.contains("my_entitlement") {
                XCTAssertEqual(request.value(forHTTPHeaderField: "Authorization"), "Bearer access")
                return (200, try self.json([["is_pro": true, "plan": "monthly"]]), [:])
            }
            return (200, data, [:])
        }
        _ = try await client.signIn(email: "a", password: "b")
        _ = try await client.exchange(code: "code", pkce: pkce)
        let entitlement = try await client.entitlement(accessToken: "access")
        XCTAssertTrue(entitlement.isPro)
        await client.signOut()
        XCTAssertNil(store.session)
        XCTAssertEqual(paths.count, 4)
        let now = Date(timeIntervalSince1970: 100)
        let session = try AuthClient.decodeSession(data, now: now)
        XCTAssertEqual(session.expiresAt, now.addingTimeInterval(3540))
        XCTAssertEqual(session.email, "a@b.c")
        XCTAssertEqual(session.userID, "id")
        XCTAssertThrowsError(try AuthClient.decodeSession(Data("{}".utf8)))
    }
    func testRefreshRaceAndFailures() async throws {
        let store = MemoryStore()
        let old = Session(accessToken: "old", expiresAt: .distantPast, refreshToken: "old")
        let rotated = Session(accessToken: "new", expiresAt: .distantFuture, refreshToken: "new")
        let client = AuthClient(store: store, transport: transport())
        store.session = old
        StubProtocol.handler = { request in
            XCTAssertEqual(request.url?.query, "grant_type=refresh_token")
            store.session = rotated
            return (400, try self.json(["error_code": "refresh_token_already_used"]), [:])
        }
        let recovered = try await client.refresh()
        XCTAssertEqual(recovered, rotated)
        for status in [500, 429, 400] {
            store.session = old
            StubProtocol.handler = { _ in (status, Data("{}".utf8), [:]) }
            do { _ = try await client.refresh(); XCTFail() } catch {}
            XCTAssertEqual(store.session, old)
        }
        StubProtocol.handler = { _ in throw URLError(.notConnectedToInternet) }
        do { _ = try await client.refresh(); XCTFail() } catch {}
        XCTAssertEqual(store.session, old)
        StubProtocol.handler = { _ in (401, Data("Invalid Refresh Token".utf8), [:]) }
        do { _ = try await client.refresh(); XCTFail() } catch { XCTAssertEqual(error as? GusanError, .unauthorized) }
        XCTAssertNil(store.session)
        let empty = try await client.refresh()
        XCTAssertNil(empty)
        store.session = old
        StubProtocol.handler = { _ in (200, try self.payload(), [:]) }
        let refreshed = try await client.refresh()
        XCTAssertEqual(refreshed?.refreshToken, "rotated")
        XCTAssertEqual(refreshed?.refreshGeneration, 1)
        store.session = rotated
        let forced = try await client.refresh(force: true)
        XCTAssertEqual(forced?.refreshToken, "rotated")
        XCTAssertFalse(AuthClient.refreshTokenRejected(status: 500, data: Data("Invalid Refresh Token".utf8)))
    }
    func testSignInMessages() {
        XCTAssertEqual(AuthClient.signInMessage(Data(#"{"error":"invalid_grant","error_description":"Invalid login credentials"}"#.utf8)), "Incorrect email or password.")
        XCTAssertEqual(AuthClient.signInMessage(Data(#"{"code":400,"error_code":"email_not_confirmed","msg":"Email not confirmed"}"#.utf8)), "Please verify your email first - check your inbox.")
        XCTAssertEqual(AuthClient.signInMessage(Data("oops".utf8)), "Sign-in failed. Try again.")
    }
}
