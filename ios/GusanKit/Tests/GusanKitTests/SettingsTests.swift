import XCTest
@testable import GusanKit

final class SettingsTests: XCTestCase {
    private final class Shared: SessionBackend {
        let name = "Preferences"
        var data: Data?
        var writable = true
        func read(_ key: String) -> SessionBackendRead { .init(data: data, readable: true) }
        func write(_ data: Data, key: String) -> Bool { if writable { self.data = data }; return writable }
        func clear(_ key: String) { data = nil }
    }
    func testPreferencesSurviveSeparatePrivateStoresAndUnavailableSharing() {
        let appName = "Gusan.preferences.app." + UUID().uuidString, keyboardName = "Gusan.preferences.keyboard." + UUID().uuidString
        let appDefaults = UserDefaults(suiteName: appName)!, keyboardDefaults = UserDefaults(suiteName: keyboardName)!
        defer { appDefaults.removePersistentDomain(forName: appName); keyboardDefaults.removePersistentDomain(forName: keyboardName) }
        let shared = Shared(), app = SettingsStore(defaults: appDefaults, shared: shared), keyboard = SettingsStore(defaults: keyboardDefaults, shared: shared)
        XCTAssertTrue(keyboard.keyClicks)
        app.keyboardAction = .rephrase; app.keyClicks = false; app.autoCapitalization = false
        keyboard.refresh()
        XCTAssertEqual(keyboard.keyboardAction, .rephrase); XCTAssertFalse(keyboard.keyClicks); XCTAssertFalse(keyboard.autoCapitalization)
        keyboard.haptics = false
        app.refresh(); XCTAssertFalse(app.haptics); XCTAssertEqual(app.keyboardAction, .rephrase)
        shared.writable = false
        app.keyboardAction = .fixGrammar; app.refresh(); XCTAssertEqual(app.keyboardAction, .fixGrammar)
        shared.data = Data("corrupt".utf8); keyboard.refresh()
        XCTAssertEqual(keyboard.keyboardAction, .rephrase); XCTAssertFalse(keyboard.haptics)
    }
    func testSharedTypingPreferencesAndAllDefaultActions() {
        let group = "Gusan.settings.tests." + UUID().uuidString
        defer { UserDefaults.standard.removePersistentDomain(forName: group) }
        let app = SettingsStore(group: group), keyboard = SettingsStore(group: group)
        XCTAssertEqual(app.keyboardAction, .smartAuto)
        XCTAssertTrue(app.keyClicks); XCTAssertTrue(app.autoCapitalization); XCTAssertTrue(app.doubleSpacePeriod); XCTAssertTrue(app.haptics)
        for action in KeyboardDefaultAction.allCases { app.keyboardAction = action; XCTAssertEqual(keyboard.keyboardAction, action) }
        app.keyboardAction = .rephrase; XCTAssertEqual(keyboard.keyboardAction.action, .rephrase)
        app.keyboardAction = .fixGrammar; XCTAssertEqual(keyboard.keyboardAction.action, .fixGrammar)
        app.keyClicks = false; app.autoCapitalization = false; app.doubleSpacePeriod = false; app.haptics = false
        XCTAssertFalse(keyboard.keyClicks); XCTAssertFalse(keyboard.autoCapitalization); XCTAssertFalse(keyboard.doubleSpacePeriod); XCTAssertFalse(keyboard.haptics)
        app.defaultAction = .translate; XCTAssertEqual(keyboard.keyboardAction, .translate)
        UserDefaults(suiteName: group)?.set("bad", forKey: "defaultAction")
        XCTAssertEqual(keyboard.keyboardAction, .smartAuto)
        app.translateTarget = "unknown"; XCTAssertEqual(keyboard.translateTarget, "en")
    }
    func testLegacyHeartbeatDecodesWithoutFullAccess() throws {
        let data = Data(#"{"role":"keyboard","timestamp":0,"stores":[]}"#.utf8)
        let report = try JSONDecoder().decode(SessionReport.self, from: data)
        XCTAssertNil(report.fullAccess)
    }
    func testHeartbeatReportsFullAccessWithoutInferringItFromStorage() {
        let resolution = AppGroupResolution(candidatesTried: [], chosen: nil, container: nil)
        for allowed in [false, true] {
            let store = SessionStore(resolution: resolution, role: .keyboard, pasteboardAccessAllowed: { allowed })
            XCTAssertEqual(store.diagnostics().local.fullAccess, allowed)
        }
    }
}
