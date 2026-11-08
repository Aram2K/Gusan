import XCTest
@testable import GusanKit

final class SettingsTests: XCTestCase {
    func testStripModeAndKeyboardModeUseOneSharedChoice() {
        let group = "Gusan.mode.alias.tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: group)!
        defer { defaults.removePersistentDomain(forName: group) }
        defaults.set("agent", forKey: "stripMode")
        let app = SettingsStore(group: group), keyboard = SettingsStore(group: group)
        XCTAssertEqual(keyboard.keyboardMode, .agent)
        app.stripMode = .smart
        keyboard.refresh(); XCTAssertEqual(keyboard.keyboardMode, .smart)
        keyboard.keyboardMode = .agent
        app.refresh(); XCTAssertEqual(app.stripMode, .agent)
        XCTAssertEqual(defaults.string(forKey: "stripMode"), "agent")
        XCTAssertEqual(defaults.string(forKey: "keyboardMode"), "agent")
        defaults.set("invalid", forKey: "keyboardMode")
        XCTAssertEqual(app.stripMode, .smart)
    }
    func testKeyboardModeAndScreenAreSharedAndDefaultToSmartWithScreenOn() {
        let appName = "Gusan.mode.app." + UUID().uuidString
        let keyboardName = "Gusan.mode.keyboard." + UUID().uuidString
        let appDefaults = UserDefaults(suiteName: appName)!
        let keyboardDefaults = UserDefaults(suiteName: keyboardName)!
        defer { appDefaults.removePersistentDomain(forName: appName); keyboardDefaults.removePersistentDomain(forName: keyboardName) }
        let shared = Shared()
        let app = SettingsStore(defaults: appDefaults, shared: shared)
        let keyboard = SettingsStore(defaults: keyboardDefaults, shared: shared)
        XCTAssertEqual(keyboard.keyboardMode, .smart)
        XCTAssertTrue(keyboard.keyboardScreenEnabled)
        keyboard.keyboardMode = .agent
        keyboard.keyboardScreenEnabled = false
        app.refresh()
        XCTAssertEqual(app.keyboardMode, .agent)
        XCTAssertFalse(app.keyboardScreenEnabled)
        app.keyboardMode = .smart
        keyboard.refresh()
        XCTAssertEqual(keyboard.keyboardMode, .smart)
        keyboardDefaults.set("invalid", forKey: "keyboardMode")
        XCTAssertEqual(keyboard.keyboardMode, .smart)
    }
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
        XCTAssertTrue(keyboard.pasteboardFallback)
        app.pasteboardFallback = false
        keyboard.refresh(); XCTAssertFalse(keyboard.pasteboardFallback)
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
    func testStripModeDefaultsPersistsAndRejectsInvalidValues() {
        let group = "Gusan.stripMode.tests." + UUID().uuidString
        let defaults = UserDefaults(suiteName: group)!
        defer { defaults.removePersistentDomain(forName: group) }
        let app = SettingsStore(group: group), keyboard = SettingsStore(group: group)
        XCTAssertEqual(app.stripMode, .smart)
        app.stripMode = .agent
        XCTAssertEqual(defaults.string(forKey: "stripMode"), "agent")
        XCTAssertEqual(keyboard.stripMode, .agent)
        XCTAssertEqual(SettingsStore(group: group).stripMode, .agent)
        keyboard.stripMode = .smart
        app.refresh(); XCTAssertEqual(app.stripMode, .smart)
        defaults.set("unknown", forKey: "stripMode")
        XCTAssertEqual(app.stripMode, .smart)
        defaults.set(42, forKey: "stripMode")
        XCTAssertEqual(app.stripMode, .smart)
    }
    func testStripModeMirrorsBetweenSeparatePrivateStores() {
        let appName = "Gusan.stripMode.app." + UUID().uuidString, keyboardName = "Gusan.stripMode.keyboard." + UUID().uuidString
        let appDefaults = UserDefaults(suiteName: appName)!, keyboardDefaults = UserDefaults(suiteName: keyboardName)!
        defer { appDefaults.removePersistentDomain(forName: appName); keyboardDefaults.removePersistentDomain(forName: keyboardName) }
        let shared = Shared(), app = SettingsStore(defaults: appDefaults, shared: shared), keyboard = SettingsStore(defaults: keyboardDefaults, shared: shared)
        XCTAssertEqual(keyboard.stripMode, .smart)
        app.stripMode = .agent
        keyboard.refresh(); XCTAssertEqual(keyboard.stripMode, .agent)
        keyboard.keyClicks = false
        app.refresh(); XCTAssertEqual(app.stripMode, .agent)
        keyboard.stripMode = .smart
        app.refresh(); XCTAssertEqual(app.stripMode, .smart)
        shared.writable = false
        app.stripMode = .agent
        shared.data = Data("corrupt".utf8)
        app.refresh(); XCTAssertEqual(app.stripMode, .agent)
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
