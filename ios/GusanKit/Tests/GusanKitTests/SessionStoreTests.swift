import XCTest
@testable import GusanKit

final class SessionStoreTests: XCTestCase {
    final class Backend: SessionBackend {
        let name: String
        var values: [String: Data] = [:]
        var readable = true
        var writable = true
        var cleared: [String] = []
        var onRead: ((String) -> Void)?
        var sessionWrites = 0
        init(_ name: String) { self.name = name }
        func read(_ key: String) -> SessionBackendRead { onRead?(key); return SessionBackendRead(data: readable ? values[key] : nil, readable: readable) }
        func write(_ data: Data, key: String) -> Bool {
            guard writable else { return false }; if key == "session" { sessionWrites += 1 }; values[key] = data; return true
        }
        func clear(_ key: String) { cleared.append(key); if writable { values[key] = nil } }
        func session(_ session: Session) { values["session"] = try! JSONEncoder().encode(session) }
    }
    let resolution = AppGroupResolution(candidatesTried: [], chosen: nil, container: nil)
    func session(_ age: TimeInterval, expiry: TimeInterval = 500) -> Session {
        Session(accessToken: "fake-\(age)", expiresAt: Date(timeIntervalSince1970: expiry), refreshToken: "fake", updatedAt: Date(timeIntervalSince1970: age))
    }
    func peer(_ backend: Backend, role: SessionRole = .keyboard, group: String? = nil, keychain: String? = nil, stores: [StoreHealth] = []) {
        let report = SessionReport(role: role, timestamp: Date(), groupID: group, keychainGroupID: keychain, stores: stores, fullAccess: true)
        backend.values["report." + role.rawValue] = try! JSONEncoder().encode(report)
    }
    func testVoiceDiagnosticsCombineKeyboardHealthAndExportWithoutSecrets() throws {
        let healthy = StoreHealth(name: "App-group file", readable: true, writable: true, hasSession: false)
        let denied = StoreHealth(name: "App-group file", readable: false, writable: false, hasSession: false)
        let board = StoreHealth(name: "Team pasteboard", readable: true, writable: true, hasSession: false)
        let app = SessionReport(role: .app, timestamp: Date(), groupID: "group", keychainGroupID: nil, stores: [healthy, board], fullAccess: nil, voiceChannel: "App Group")
        func diagnostics(_ access: Bool, _ file: StoreHealth = healthy, voice: String? = nil, channel: String = "App Group") -> SessionDiagnostics {
            let keyboard = SessionReport(role: .keyboard, timestamp: Date(), groupID: "group", keychainGroupID: nil, stores: [file, board], fullAccess: access, voiceChannel: voice)
            return SessionDiagnostics(local: app, peer: keyboard, pasteboardFallback: "not needed", localVoiceChannel: channel)
        }
        XCTAssertEqual(diagnostics(true).voiceChannel, "App Group")
        XCTAssertEqual(diagnostics(false).voiceChannel, "Unavailable (keyboard reports Full Access off)")
        XCTAssertEqual(diagnostics(true, denied).voiceChannel, "Unavailable (keyboard cannot use the App Group)")
        XCTAssertTrue(diagnostics(true, voice: "Unavailable (pasteboard fallback is off)").voiceChannel.contains("pasteboard fallback is off"))
        XCTAssertEqual(diagnostics(true, voice: "App Group", channel: "Pasteboard fallback").voiceChannel, "Unavailable (app and keyboard voice channels differ)")
        let empty = SessionDiagnostics(local: app, peer: nil, pasteboardFallback: "not needed", localVoiceChannel: "App Group")
        XCTAssertEqual(empty.voiceChannel, "Unavailable (keyboard has not reported)")
        let data = try diagnostics(false).encodedReport()
        let report = try XCTUnwrap(JSONSerialization.jsonObject(with: data) as? [String: Any])
        XCTAssertEqual(report["voiceChannel"] as? String, "Unavailable (keyboard reports Full Access off)")
        XCTAssertEqual(report["localVoiceChannel"] as? String, "App Group")
        XCTAssertFalse(String(decoding: data, as: UTF8.self).contains("accessToken"))
        XCTAssertFalse(String(decoding: data, as: UTF8.self).contains("context"))
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
        peer(stores[2])
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
        peer(shared)
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
        XCTAssertNil(shared.values["session"])
        _ = app.diagnostics()
        let keyboard = SessionStore(resolution: resolution, backends: [denied, shared], privateBackend: Backend("private"), role: .keyboard)
        _ = keyboard.diagnostics()
        let diagnostics = app.diagnostics()
        XCTAssertTrue(diagnostics.isShared); XCTAssertTrue(diagnostics.pasteboardOK); XCTAssertFalse(diagnostics.keychainOK)
        XCTAssertTrue(diagnostics.local.stores[1].hasSession)
        let encoded = try XCTUnwrap(shared.values["report.app"])
        XCTAssertFalse(String(decoding: encoded, as: UTF8.self).contains("fake"))
        XCTAssertEqual(keyboard.read(), session(1))
        XCTAssertEqual(keyboard.diagnostics().peer?.role, .app)
        XCTAssertEqual(app.diagnostics().peer?.role, .keyboard)
        XCTAssertNotNil(shared.values["session"], "Reports must not overwrite credentials")
    }
    func testSameNormalGroupDeletesPasteboardAndPreservesItsSession() {
        for name in ["Keychain", "App-group file"] {
            let normal = Backend(name), board = Backend("Team pasteboard")
            let local = AppGroupResolution(candidatesTried: [], chosen: "same", container: nil)
            peer(board, group: name == "App-group file" ? "same" : "other", keychain: name == "Keychain" ? "same" : "other", stores: [.init(name: name, readable: true, writable: true, hasSession: false)])
            board.session(session(3))
            let store = SessionStore(resolution: local, backends: [normal, board], privateBackend: Backend("private"))
            XCTAssertEqual(store.read(), session(3))
            XCTAssertNil(board.values["session"])
            XCTAssertNotNil(normal.values["session"])
            store.write(session(4))
            XCTAssertNil(board.values["session"])
            XCTAssertEqual(store.diagnostics().pasteboardFallback, "not needed")
        }
    }
    func testDifferentGroupsUseFallbackSymmetricallyDespiteReadableStores() {
        for role in [SessionRole.app, .keyboard] {
            let board = Backend("Team pasteboard")
            let local = AppGroupResolution(candidatesTried: [], chosen: "ours", container: nil)
            peer(board, role: role == .app ? .keyboard : .app, group: "theirs", keychain: "theirs", stores: ["Keychain", "App-group file"].map { .init(name: $0, readable: true, writable: true, hasSession: false) })
            let store = SessionStore(resolution: local, backends: [board], privateBackend: Backend("private"), role: role)
            store.write(session(5))
            XCTAssertNotNil(board.values["session"])
            XCTAssertEqual(store.diagnostics().pasteboardFallback, "in use")
            store.clear()
            XCTAssertNil(board.values["session"])
        }
    }
    func testNoPeerDefersFallbackAndRetainsPrivateCopyUntilReportArrives() {
        let board = Backend("Team pasteboard"), privateStore = Backend("private")
        let store = SessionStore(resolution: resolution, backends: [board], privateBackend: privateStore)
        store.write(session(5))
        XCTAssertNil(board.values["session"])
        XCTAssertEqual(store.read(), session(5))
        XCTAssertNotNil(privateStore.values["session"])
        XCTAssertNotNil(store.diagnostics().local)
        XCTAssertNotNil(board.values["report.app"])
        XCTAssertNil(board.values["session"])
        peer(board)
        XCTAssertEqual(store.read(), session(5))
        XCTAssertNotNil(board.values["session"])
        XCTAssertNil(privateStore.values["session"])
    }
    func testDisablingFallbackDeletesCopyAndKeepsHeartbeatReports() {
        var enabled = true
        let board = Backend("Team pasteboard"), privateStore = Backend("private")
        peer(board)
        let store = SessionStore(resolution: resolution, backends: [board], privateBackend: privateStore, pasteboardFallbackAllowed: { enabled })
        store.write(session(1)); XCTAssertNotNil(board.values["session"])
        enabled = false
        XCTAssertEqual(store.read(), session(1))
        XCTAssertNil(board.values["session"])
        store.write(session(2))
        XCTAssertNil(board.values["session"])
        XCTAssertEqual(store.diagnostics().pasteboardFallback, "off")
        XCTAssertNotNil(board.values["report.app"])
    }
    func testNewestPeerReportControlsNormalSharing() {
        let normal = Backend("App-group file"), board = Backend("Team pasteboard")
        let local = AppGroupResolution(candidatesTried: [], chosen: "same", container: nil)
        peer(normal, group: "other")
        peer(board, group: "same", stores: [.init(name: normal.name, readable: true, writable: true, hasSession: false)])
        let store = SessionStore(resolution: local, backends: [normal, board], privateBackend: Backend("private"))
        store.write(session(1))
        XCTAssertNil(board.values["session"])
    }
    func testReconcileRereadsEachStoreBeforeWritingOverRotatedTokens() throws {
        for name in ["Keychain", "App-group file", "Team pasteboard"] {
            let backend = Backend(name)
            peer(backend)
            backend.session(session(1))
            var reads = 0
            backend.onRead = { [weak backend] key in
                if key == "session" {
                    reads += 1
                    if reads == 2 { backend?.session(self.session(2)) }
                }
            }
            let store = SessionStore(resolution: resolution, backends: [backend], privateBackend: Backend("private"))
            _ = store.read()
            XCTAssertEqual(try JSONDecoder().decode(Session.self, from: XCTUnwrap(backend.values["session"])), session(2))
            XCTAssertEqual(backend.sessionWrites, 0)
            backend.onRead = nil
            XCTAssertEqual(store.read(), session(2))
        }
    }
    func testRefreshWritesNeverReplaceNewerOrEqualStoreCopies() throws {
        let backend = Backend("Team pasteboard")
        peer(backend)
        let current = session(20, expiry: 900)
        backend.session(current)
        let store = SessionStore(resolution: resolution, backends: [backend], privateBackend: Backend("private"))
        store.write(session(10, expiry: 1000))
        store.write(session(20, expiry: 500))
        var equal = current; equal.accessToken = "different-but-not-newer"
        store.write(equal)
        XCTAssertEqual(backend.sessionWrites, 0)
        XCTAssertEqual(try JSONDecoder().decode(Session.self, from: XCTUnwrap(backend.values["session"])), current)
        store.write(session(20, expiry: 1000))
        XCTAssertEqual(backend.sessionWrites, 1)
        XCTAssertEqual(store.read(), session(20, expiry: 1000))
    }
    func testPrivateFallbackDoesNotDowngradeNewerCopy() {
        let backend = Backend("Team pasteboard"), privateStore = Backend("private")
        backend.writable = false
        privateStore.session(session(20))
        let store = SessionStore(resolution: resolution, backends: [backend], privateBackend: privateStore)
        store.write(session(10))
        XCTAssertEqual(store.read(), session(20))
        XCTAssertEqual(privateStore.sessionWrites, 0)
    }
    func testMatchingPeerGroupStillRequiresOurStoreReadableAndWritable() {
        for name in ["Keychain", "App-group file"] {
            for unreadable in [true, false] {
                let normal = Backend(name), board = Backend("Team pasteboard")
                normal.readable = !unreadable; normal.writable = unreadable
                let local = AppGroupResolution(candidatesTried: [], chosen: "same", container: nil)
                peer(board, group: "same", keychain: "same", stores: [.init(name: name, readable: true, writable: true, hasSession: false)])
                let store = SessionStore(resolution: local, backends: [normal, board], privateBackend: Backend("private"))
                store.write(session(10))
                XCTAssertNotNil(board.values["session"])
                XCTAssertEqual(store.diagnostics().pasteboardFallback, "in use")
            }
        }
    }
    func testMissingOwnStoreCannotDisableFallbackFromPeerReportAlone() {
        let board = Backend("Team pasteboard")
        let local = AppGroupResolution(candidatesTried: [], chosen: "same", container: nil)
        peer(board, group: "same", keychain: "same", stores: ["Keychain", "App-group file"].map { .init(name: $0, readable: true, writable: true, hasSession: false) })
        let store = SessionStore(resolution: local, backends: [board], privateBackend: Backend("private"))
        store.write(session(10))
        XCTAssertNotNil(board.values["session"])
    }
    func testRecoveringLocalNormalStorePurgesFallbackImmediately() {
        let normal = Backend("App-group file"), board = Backend("Team pasteboard")
        normal.writable = false
        let local = AppGroupResolution(candidatesTried: [], chosen: "same", container: nil)
        peer(board, group: "same", stores: [.init(name: normal.name, readable: true, writable: true, hasSession: false)])
        let store = SessionStore(resolution: local, backends: [normal, board], privateBackend: Backend("private"))
        store.write(session(10)); XCTAssertNotNil(board.values["session"])
        normal.writable = true
        XCTAssertEqual(store.diagnostics().pasteboardFallback, "not needed")
        XCTAssertNil(board.values["session"])
        XCTAssertNotNil(normal.values["session"])
    }
    func testRefreshSurvivesClockRollbackAndReconcilesUnavailableStore() throws {
        let file = Backend("App-group file"), board = Backend("Team pasteboard"); peer(board)
        var before = session(100); before.refreshToken = "old"
        var rotated = session(20); rotated.refreshToken = "rotated"
        file.session(before); board.session(before); file.writable = false
        let store = SessionStore(resolution: resolution, backends: [file, board], privateBackend: Backend("private"))
        XCTAssertEqual(store.writeRefreshed(rotated, replacing: before).refreshToken, "rotated")
        file.writable = true
        XCTAssertEqual(store.read()?.refreshToken, "rotated")
        XCTAssertEqual(try JSONDecoder().decode(Session.self, from: XCTUnwrap(file.values["session"])).refreshToken, "rotated")
    }
    func testRefreshAdoptsConcurrentPeerRotationRegardlessOfClock() {
        let file = Backend("App-group file"), board = Backend("Team pasteboard"); peer(board)
        var before = session(100); before.refreshToken = "old"
        var peerSession = session(10); peerSession.refreshToken = "peer"
        var ours = session(20); ours.refreshToken = "ours"
        file.session(before); board.session(peerSession)
        let store = SessionStore(resolution: resolution, backends: [file, board], privateBackend: Backend("private"))
        XCTAssertEqual(store.writeRefreshed(ours, replacing: before).refreshToken, "peer")
        XCTAssertEqual(store.read()?.refreshToken, "peer")
    }
    func testStaleReconcileCannotOverwritePeerRotationAfterClockRollback() {
        let board = Backend("Team pasteboard"); peer(board)
        var before = session(100); before.refreshToken = "old"
        var rotated = session(20); rotated.refreshToken = "rotated"
        let peerSession = rotated.refreshed(replacing: before)
        board.session(before)
        var reads = 0
        board.onRead = { [weak board] key in
            if key == "session" { reads += 1; if reads == 2 { board?.session(peerSession) } }
        }
        let store = SessionStore(resolution: resolution, backends: [board], privateBackend: Backend("private"))
        _ = store.read(); board.onRead = nil
        XCTAssertEqual(store.read()?.refreshToken, "rotated")
    }
    func testRefreshChoosesOneNewestPeerRotationAcrossDivergentBackends() throws {
        let file = Backend("App-group file"), board = Backend("Team pasteboard"); peer(board)
        var before = session(100); before.refreshToken = "old"
        var older = session(500); older.refreshToken = "peer-older"
        older = older.refreshed(replacing: before)
        var newest = session(10); newest.refreshToken = "peer-newest"
        newest = newest.refreshed(replacing: older)
        var ours = session(20); ours.refreshToken = "ours"
        file.session(newest); board.session(older)
        let store = SessionStore(resolution: resolution, backends: [file, board], privateBackend: Backend("private"))
        XCTAssertEqual(store.writeRefreshed(ours, replacing: before), newest)
        for backend in [file, board] { XCTAssertEqual(try JSONDecoder().decode(Session.self, from: XCTUnwrap(backend.values["session"])), newest) }
        XCTAssertEqual(store.read(), newest)
    }
    func testUnavailablePasteboardOnMacDoesNotReportSharing() {
        #if !canImport(UIKit)
        let backend = NamedSessionPasteboard()
        XCTAssertFalse(backend.read("session").readable)
        XCTAssertFalse(backend.write(Data(), key: "session"))
        #endif
    }
}
