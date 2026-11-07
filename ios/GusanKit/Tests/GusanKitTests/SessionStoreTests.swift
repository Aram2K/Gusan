import XCTest
@testable import GusanKit

final class SessionStoreTests: XCTestCase {
    final class Backend: SessionBackend {
        let name: String
        var values: [String: Data] = [:]
        var readable = true
        var writable = true
        var cleared: [String] = []
        init(_ name: String) { self.name = name }
        func read(_ key: String) -> SessionBackendRead { SessionBackendRead(data: readable ? values[key] : nil, readable: readable) }
        func write(_ data: Data, key: String) -> Bool {
            guard writable else { return false }; values[key] = data; return true
        }
        func clear(_ key: String) { cleared.append(key); if writable { values[key] = nil } }
        func session(_ session: Session) { values["session"] = try! JSONEncoder().encode(session) }
    }
    let resolution = AppGroupResolution(candidatesTried: [], chosen: nil, container: nil)
    func session(_ age: TimeInterval, expiry: TimeInterval = 500) -> Session {
        Session(accessToken: "fake-\(age)", expiresAt: Date(timeIntervalSince1970: expiry), refreshToken: "fake", updatedAt: Date(timeIntervalSince1970: age))
    }
    func testNewestWinsAndExpiryBreaksTies() {
        let keychain = Backend("Keychain"), file = Backend("App-group file"), pasteboard = Backend("Team pasteboard")
        let store = SessionStore(resolution: resolution, backends: [keychain, file, pasteboard], privateBackend: Backend("private"))
        XCTAssertNil(store.read())
        keychain.session(session(10)); file.session(session(20)); pasteboard.session(session(30))
        XCTAssertEqual(store.read(), session(30))
        file.session(session(30, expiry: 900))
        XCTAssertEqual(store.read(), session(30, expiry: 900))
        pasteboard.values["session"] = Data("bad".utf8)
        XCTAssertEqual(store.read(), session(30, expiry: 900))
    }
    func testWritesRotatedSessionToAllAndClearsAll() throws {
        let stores = [Backend("Keychain"), Backend("App-group file"), Backend("Team pasteboard")]
        let privateStore = Backend("private"); privateStore.session(session(1))
        let store = SessionStore(resolution: resolution, backends: stores, privateBackend: privateStore)
        store.write(session(10)); store.write(session(20))
        for backend in stores { XCTAssertEqual(try JSONDecoder().decode(Session.self, from: XCTUnwrap(backend.values["session"])), session(20)) }
        XCTAssertNil(privateStore.values["session"])
        store.clear()
        for backend in stores + [privateStore] { XCTAssertNil(backend.values["session"]); XCTAssertTrue(backend.cleared.contains("session")) }
        XCTAssertNil(store.read())
    }
    func testPrivateCopySurvivesFailedMigrationAndMovesOnlyAfterSuccess() {
        let shared = Backend("Team pasteboard"), privateStore = Backend("private")
        shared.writable = false; privateStore.session(session(1))
        let store = SessionStore(resolution: resolution, backends: [shared], privateBackend: privateStore)
        XCTAssertEqual(store.read(), session(1)); XCTAssertNotNil(privateStore.values["session"])
        store.write(session(2))
        XCTAssertEqual(store.read(), session(2)); XCTAssertNotNil(privateStore.values["session"])
        shared.writable = true
        XCTAssertEqual(store.read(), session(2)); XCTAssertNil(privateStore.values["session"])
        XCTAssertNotNil(shared.values["session"])
        privateStore.session(session(99))
        XCTAssertEqual(store.read(), session(2), "A stale private login must never override a shared session")
    }
    func testDiagnosticsAreTokenFreeSharedHeartbeats() throws {
        let shared = Backend("Team pasteboard"), denied = Backend("Keychain")
        denied.readable = false; denied.writable = false
        let app = SessionStore(resolution: resolution, backends: [denied, shared], privateBackend: Backend("private"))
        app.write(session(1))
        let diagnostics = app.diagnostics()
        XCTAssertTrue(diagnostics.isShared); XCTAssertTrue(diagnostics.pasteboardOK); XCTAssertFalse(diagnostics.keychainOK)
        XCTAssertTrue(diagnostics.local.stores[1].hasSession)
        let encoded = try XCTUnwrap(shared.values["report.app"])
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("fake"))
        let keyboard = SessionStore(resolution: resolution, backends: [denied, shared], privateBackend: Backend("private"), role: .keyboard)
        XCTAssertEqual(keyboard.read(), session(1))
        XCTAssertEqual(keyboard.diagnostics().peer?.role, .app)
        XCTAssertEqual(app.diagnostics().peer?.role, .keyboard)
        XCTAssertNotNil(shared.values["session"], "Reports must not overwrite credentials")
    }
    func testUnavailablePasteboardOnMacDoesNotReportSharing() {
        #if !canImport(UIKit)
        let backend = NamedSessionPasteboard()
        XCTAssertFalse(backend.read("session").readable)
        XCTAssertFalse(backend.write(Data(), key: "session"))
        #endif
    }
}
