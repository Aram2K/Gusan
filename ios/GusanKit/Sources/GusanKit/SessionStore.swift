import Foundation
import Security

public struct Session: Codable, Equatable {
    public var accessToken: String
    public var expiresAt: Date
    public var refreshToken: String
    public var userID: String?
    public var email: String?
    public var updatedAt: Date
    public var refreshLineage: String?
    public var refreshGeneration: Int?
    public init(accessToken: String, expiresAt: Date, refreshToken: String, userID: String? = nil, email: String? = nil, updatedAt: Date = Date()) {
        self.accessToken = accessToken; self.expiresAt = expiresAt; self.refreshToken = refreshToken
        self.userID = userID; self.email = email; self.updatedAt = updatedAt
    }
    var lineage: String { refreshLineage ?? SHA256.digest(Array(refreshToken.utf8)).map { String(format: "%02x", $0) }.joined() }
    var generation: Int { refreshGeneration ?? 0 }
    func refreshed(replacing previous: Session) -> Session {
        var result = self
        result.refreshLineage = previous.lineage
        result.refreshGeneration = previous.generation + 1
        return result
    }
}

public protocol SessionStorage: AnyObject {
    func read() -> Session?
    func write(_ session: Session)
    func writeRefreshed(_ session: Session, replacing previous: Session) -> Session
    func clear()
}

public extension SessionStorage {
    func writeRefreshed(_ session: Session, replacing previous: Session) -> Session {
        if let current = read(), current.refreshToken != previous.refreshToken { return current }
        let rotated = session.refreshed(replacing: previous)
        write(rotated)
        return read() ?? rotated
    }
}

public struct AppGroupResolution {
    public let candidatesTried: [String]
    public let chosen: String?
    public let container: URL?
}

public enum AppGroupResolver {
    public static func provisionGroups(_ data: Data) -> [String] {
        guard let start = data.range(of: Data("<?xml".utf8)),
              let end = data.range(of: Data("</plist>".utf8), in: start.lowerBound..<data.endIndex),
              let plist = try? PropertyListSerialization.propertyList(from: data[start.lowerBound..<end.upperBound], format: nil) as? [String: Any],
              let entitlements = plist["Entitlements"] as? [String: Any],
              let groups = entitlements["com.apple.security.application-groups"] as? [String] else { return [] }
        return groups
    }
    public static func resolve(bundle: Bundle = .main, container: (String) -> URL? = { FileManager.default.containerURL(forSecurityApplicationGroupIdentifier: $0) }) -> AppGroupResolution {
        let data = bundle.url(forResource: "embedded", withExtension: "mobileprovision").flatMap { try? Data(contentsOf: $0) }
        return resolve(provision: data, fallback: bundle.object(forInfoDictionaryKey: "GusanAppGroup") as? String, container: container)
    }
    public static func resolve(provision: Data?, fallback: String?, container: (String) -> URL?) -> AppGroupResolution {
        let groups = provision.map(provisionGroups) ?? []
        let preferred = groups.filter { $0.localizedCaseInsensitiveContains("gusan") }
        var candidates = preferred + groups.filter { !preferred.contains($0) }
        if let fallback, !candidates.contains(fallback) { candidates.append(fallback) }
        var tried: [String] = []
        for group in candidates {
            tried.append(group)
            if let url = container(group) { return AppGroupResolution(candidatesTried: tried, chosen: group, container: url) }
        }
        return AppGroupResolution(candidatesTried: tried, chosen: nil, container: nil)
    }
}

public struct StoreHealth: Codable, Equatable {
    public let name: String
    public let readable: Bool
    public let writable: Bool
    public let hasSession: Bool
    public var summary: String { "\(name): read \(readable ? "yes" : "no"), write \(writable ? "yes" : "no"), session \(hasSession ? "yes" : "no")" }
}

public enum SessionRole: String, Codable { case app, keyboard }

public struct SessionReport: Codable {
    public let role: SessionRole
    public let timestamp: Date
    public let groupID: String?
    public let keychainGroupID: String?
    public let stores: [StoreHealth]
    public let fullAccess: Bool?
    public var voiceChannel: String? = nil
}

public struct SessionDiagnostics {
    public let local: SessionReport
    public let peer: SessionReport?
    public let pasteboardFallback: String
    public let localVoiceChannel: String
    public var voiceChannel: String {
        guard local.role == .app else { return localVoiceChannel }
        guard let peer else { return "Unavailable (keyboard has not reported)" }
        if peer.fullAccess == false { return "Unavailable (keyboard reports Full Access off)" }
        if localVoiceChannel.hasPrefix("Unavailable") { return localVoiceChannel }
        if let voice = peer.voiceChannel, voice.hasPrefix("Unavailable") { return voice.replacingOccurrences(of: "Unavailable (", with: "Unavailable (keyboard: ", options: .anchored) }
        if localVoiceChannel == "App Group" {
            guard local.groupID != nil, local.groupID == peer.groupID,
                  peer.stores.contains(where: { $0.name == "App-group file" && $0.readable && $0.writable }) else { return "Unavailable (keyboard cannot use the App Group)" }
        } else if localVoiceChannel == "Pasteboard fallback" {
            guard peer.stores.contains(where: { $0.name == "Team pasteboard" && $0.readable && $0.writable }) else { return "Unavailable (keyboard cannot read/write the pasteboard)" }
        }
        if let voice = peer.voiceChannel, voice != localVoiceChannel { return "Unavailable (app and keyboard voice channels differ)" }
        return localVoiceChannel
    }
    public func encodedReport() throws -> Data {
        struct Report: Encodable {
            let app: SessionReport
            let keyboard: SessionReport?
            let voiceChannel: String
            let localVoiceChannel: String
        }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]; encoder.dateEncodingStrategy = .iso8601
        return try encoder.encode(Report(app: local, keyboard: peer, voiceChannel: voiceChannel, localVoiceChannel: localVoiceChannel))
    }
    public var groupID: String? { local.groupID }
    public var keychainOK: Bool { local.stores.contains { $0.name == "Keychain" && $0.readable && $0.writable } }
    public var fileOK: Bool { local.stores.contains { $0.name == "App-group file" && $0.readable && $0.writable } }
    public var pasteboardOK: Bool { local.stores.contains { $0.name == "Team pasteboard" && $0.readable && $0.writable } }
    public var isShared: Bool { local.stores.contains { $0.readable && $0.writable } }
    public var sharingStatus: String { isShared ? "shared" : "not shared" }
    public var summary: String { (local.stores.map(\.summary) + ["Voice: " + voiceChannel]).joined(separator: "\n") }
}

public final class SessionStore: SessionStorage {
    public let resolution: AppGroupResolution
    public let role: SessionRole
    private let backends: [SessionBackend]
    private let privateBackend: SessionBackend
    private let keychainGroup: String?
    private let fullAccess: () -> Bool?
    private let fallbackAllowed: () -> Bool
    private let voicePasteboard: SessionBackend
    private var voiceCheckedAt = Date.distantPast
    private var cachedVoiceRoute = VoiceChannel.Route.unavailable
    private let lock = NSLock()
    public init(resolution: AppGroupResolution = AppGroupResolver.resolve(), role: SessionRole = .app, pasteboardAccessAllowed: @escaping () -> Bool = { true }) {
        self.role = role
        fullAccess = { role == .keyboard ? pasteboardAccessAllowed() : nil }
        let pasteboard = NamedSessionPasteboard(accessAllowed: pasteboardAccessAllowed)
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--gusan-end-storage-test") {
            pasteboard.clear("debug-mode")
            let isolatedStore = NamedSessionPasteboard(namespace: "com.aram2k.gusan.debug.session", accessAllowed: pasteboardAccessAllowed)
            for key in ["session", "report.app", "report.keyboard"] { isolatedStore.clear(key) }
            let voice = NamedSessionPasteboard(namespace: "com.aram2k.gusan.debug.voice", accessAllowed: pasteboardAccessAllowed)
            for key in ["command", "status", "keyboard", "launch", "transcript", "receipt", "probe"] { voice.clear(key) }
        }
        if ProcessInfo.processInfo.arguments.contains("--gusan-pasteboard-only") {
            _ = pasteboard.write(Data([1]), key: "debug-mode")
        }
        let isolated = pasteboard.read("debug-mode").data == Data([1])
        #else
        let isolated = false
        #endif
        self.resolution = isolated ? AppGroupResolution(candidatesTried: [], chosen: nil, container: nil) : resolution
        voicePasteboard = NamedSessionPasteboard(namespace: isolated ? "com.aram2k.gusan.debug.voice" : "com.aram2k.gusan.voice", lifetime: 600, accessAllowed: pasteboardAccessAllowed)
        keychainGroup = isolated ? nil : resolution.chosen
        backends = [KeychainSessionBackend(group: keychainGroup, enabled: keychainGroup != nil), FileSessionBackend(container: self.resolution.container), NamedSessionPasteboard(namespace: isolated ? "com.aram2k.gusan.debug.session" : "com.aram2k.gusan.session", accessAllowed: pasteboardAccessAllowed)]
        #if DEBUG
        let directory = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first?.appendingPathComponent("GusanStorageTest", isDirectory: true)
        if let directory { try? FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true) }
        let debugPrivate = FileSessionBackend(container: directory)
        if !isolated { debugPrivate.clear("session") }
        privateBackend = isolated ? debugPrivate : KeychainSessionBackend(group: nil, enabled: true)
        #else
        privateBackend = KeychainSessionBackend(group: nil, enabled: true)
        #endif
        let settings = SettingsStore(group: self.resolution.chosen, pasteboardAccessAllowed: pasteboardAccessAllowed)
        fallbackAllowed = { settings.refresh(); return settings.pasteboardFallback }
    }
    init(resolution: AppGroupResolution, backends: [SessionBackend], privateBackend: SessionBackend, role: SessionRole = .app, pasteboardFallbackAllowed: @escaping () -> Bool = { true }, voicePasteboard: SessionBackend? = nil) {
        self.resolution = resolution; self.backends = backends; self.privateBackend = privateBackend; self.role = role
        keychainGroup = resolution.chosen
        fullAccess = { nil }
        fallbackAllowed = pasteboardFallbackAllowed
        self.voicePasteboard = voicePasteboard ?? NamedSessionPasteboard(namespace: "com.aram2k.gusan.voice", lifetime: 600)
    }
    public func makeVoiceChannel() -> VoiceChannel {
        VoiceChannel(container: resolution.container, group: "com.aram2k.gusan", pasteboard: voicePasteboard, route: { self.voiceRoute() })
    }
    private func voiceRoute() -> VoiceChannel.Route {
        synchronized {
            guard role != .keyboard || fullAccess() != false else { return .fullAccessRequired }
            let allowed = fallbackAllowed()
            if Date().timeIntervalSince(voiceCheckedAt) < 1 {
                if cachedVoiceRoute == .pasteboard && !allowed { return .disabled }
                if cachedVoiceRoute != .disabled || !allowed { return cachedVoiceRoute }
            }
            let stores = backends.map(health)
            let peer = peerReport()
            let localFile = resolution.container != nil && stores.contains { $0.name == "App-group file" && $0.readable && $0.writable }
            let sharedFile = peer == nil || (resolution.chosen != nil && resolution.chosen == peer?.groupID && peer!.stores.contains { $0.name == "App-group file" && $0.readable && $0.writable })
            cachedVoiceRoute = localFile && sharedFile ? .file : (allowed ? .pasteboard : .disabled)
            voiceCheckedAt = Date()
            let selected = cachedVoiceRoute
            let voice = VoiceChannel(container: resolution.container, group: "com.aram2k.gusan", pasteboard: voicePasteboard, route: { selected }).diagnosticsDescription
            let report = SessionReport(role: role, timestamp: Date(), groupID: resolution.chosen, keychainGroupID: keychainGroup, stores: stores, fullAccess: fullAccess(), voiceChannel: voice)
            if let data = try? JSONEncoder().encode(report) { for backend in backends { _ = backend.write(data, key: "report." + role.rawValue) } }
            return cachedVoiceRoute
        }
    }
    private func synchronized<T>(_ body: () -> T) -> T {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.synchronized(body) } }
        #endif
        lock.lock(); defer { lock.unlock() }; return body()
    }
    private func decode(_ data: Data?) -> Session? { data.flatMap { try? JSONDecoder().decode(Session.self, from: $0) } }
    public func read() -> Session? {
        synchronized { reconcile() }
    }
    static func sharedBackendAvailable(group: String?, keychainOK: Bool, fileOK: Bool) -> Bool {
        group != nil && (keychainOK || fileOK)
    }
    private func peerReport() -> SessionReport? {
        let key = "report." + (role == .app ? "keyboard" : "app")
        return backends.compactMap { $0.read(key).data.flatMap { try? JSONDecoder().decode(SessionReport.self, from: $0) } }.max { $0.timestamp < $1.timestamp }
    }
    private func normalSharing(_ peer: SessionReport?) -> Bool {
        guard let peer else { return false }
        return peer.stores.contains { store in
            guard store.readable && store.writable else { return false }
            let matchingGroup: Bool
            switch store.name {
            case "Keychain": matchingGroup = keychainGroup != nil && keychainGroup == peer.keychainGroupID
            case "App-group file": matchingGroup = resolution.chosen != nil && resolution.chosen == peer.groupID
            default: return false
            }
            guard matchingGroup, let backend = backends.first(where: { $0.name == store.name }) else { return false }
            let local = health(backend)
            return local.readable && local.writable
        }
    }
    private func health(_ backend: SessionBackend) -> StoreHealth {
        let value = backend.read("session")
        let writable = backend.write(Data([0]), key: "probe." + role.rawValue)
        backend.clear("probe." + role.rawValue)
        return StoreHealth(name: backend.name, readable: value.readable, writable: writable, hasSession: decode(value.data) != nil)
    }
    private func usePasteboard() -> Bool {
        let peer = peerReport()
        return fallbackAllowed() && peer != nil && !normalSharing(peer)
    }
    private func reconcile() -> Session? {
        let sessions = backends.compactMap { decode($0.read("session").data) }
        var newest = sessions.max { newer($1, than: $0) }
        if let privateCopy = decode(privateBackend.read("session").data), newest == nil || (privateCopy.lineage == newest!.lineage && privateCopy.generation > newest!.generation) { newest = privateCopy }
        if let newest, let data = try? JSONEncoder().encode(newest) {
            if writeShared(data) { privateBackend.clear("session") }
            else { _ = persist(data, to: privateBackend) }
        } else if !usePasteboard() {
            for backend in backends where backend.name == "Team pasteboard" { backend.clear("session") }
        }
        return newest
    }
    private func writeShared(_ data: Data) -> Bool {
        let allowed = usePasteboard()
        var saved = false
        for backend in backends {
            if backend.name == "Team pasteboard" && !allowed { backend.clear("session") }
            else if persist(data, to: backend) { saved = true }
        }
        return saved
    }
    private func persist(_ data: Data, to backend: SessionBackend) -> Bool {
        guard let incoming = decode(data) else { return false }
        if let current = decode(backend.read("session").data), !newer(incoming, than: current) { return true }
        return backend.write(data, key: "session")
    }
    private func newer(_ candidate: Session, than current: Session) -> Bool {
        if candidate.lineage == current.lineage || candidate.refreshToken == current.refreshToken,
           candidate.generation != current.generation { return candidate.generation > current.generation }
        return candidate.updatedAt == current.updatedAt ? candidate.expiresAt > current.expiresAt : candidate.updatedAt > current.updatedAt
    }
    public func write(_ session: Session) {
        synchronized {
            guard let data = try? JSONEncoder().encode(session) else { return }
            if writeShared(data) { privateBackend.clear("session") }
            else { _ = persist(data, to: privateBackend) }
        }
    }
    public func writeRefreshed(_ session: Session, replacing previous: Session) -> Session {
        synchronized {
            let allowed = usePasteboard()
            let targets = backends.filter { $0.name != "Team pasteboard" || allowed }
            if !allowed { backends.filter { $0.name == "Team pasteboard" }.forEach { $0.clear("session") } }
            var winner = session.refreshed(replacing: previous)
            var copies = targets.compactMap { decode($0.read("session").data) }
            if let privateCopy = decode(privateBackend.read("session").data), copies.isEmpty || (privateCopy.lineage == previous.lineage && privateCopy.generation >= previous.generation) { copies.append(privateCopy) }
            let peers = copies.filter {
                $0.refreshToken != previous.refreshToken && $0.refreshToken != session.refreshToken
                    && !($0.lineage == previous.lineage && $0.generation < previous.generation)
            }
            if let peer = peers.max(by: { $0.generation == $1.generation ? newer($1, than: $0) : $0.generation < $1.generation }) {
                winner = peer
                if peer.refreshLineage == nil && peer.userID == previous.userID {
                    winner.refreshLineage = previous.lineage; winner.refreshGeneration = previous.generation + 1
                }
            }
            func save(_ backend: SessionBackend) -> Bool {
                if let current = decode(backend.read("session").data) {
                    if current == winner { return true }
                    if current.generation > winner.generation || (current.generation == winner.generation && newer(current, than: winner)) { return false }
                }
                guard let data = try? JSONEncoder().encode(winner) else { return false }
                return backend.write(data, key: "session")
            }
            var saved = false
            for backend in targets { if save(backend) { saved = true } }
            if saved { privateBackend.clear("session") }
            else { _ = save(privateBackend) }
            if !usePasteboard() { backends.filter { $0.name == "Team pasteboard" }.forEach { $0.clear("session") } }
            return winner
        }
    }
    public func clear() {
        synchronized {
            for backend in backends { backend.clear("session") }
            privateBackend.clear("session")
            makeVoiceChannel().clear()
        }
    }
    public func diagnostics() -> SessionDiagnostics {
        let voice = makeVoiceChannel().diagnosticsDescription
        return synchronized {
            _ = reconcile()
            let reportKey = "report." + role.rawValue
            let stores = backends.map(health)
            let report = SessionReport(role: role, timestamp: Date(), groupID: resolution.chosen, keychainGroupID: keychainGroup, stores: stores, fullAccess: fullAccess(), voiceChannel: voice)
            if let data = try? JSONEncoder().encode(report) { for backend in backends { _ = backend.write(data, key: reportKey) } }
            let peer = peerReport()
            let fallback = !fallbackAllowed() ? "off" : (usePasteboard() && stores.contains { $0.name == "Team pasteboard" && $0.hasSession } ? "in use" : "not needed")
            return SessionDiagnostics(local: report, peer: peer, pasteboardFallback: fallback, localVoiceChannel: voice)
        }
    }
}

public final class SettingsStore {
    private let defaults: UserDefaults
    private let shared: SessionBackend
    private let lock = NSLock()
    private var cached: [String: Any] = [:]
    private var lastRead = Date.distantPast
    private let keys = ["defaultAction", "translateTarget", "keyClicks", "autoCapitalization", "doubleSpacePeriod", "haptics", "pasteboardFallback", "privacyMode", "stripMode"]
    public init(group: String?, pasteboardAccessAllowed: @escaping () -> Bool = { true }) {
        defaults = group.flatMap(UserDefaults.init(suiteName:)) ?? .standard
        shared = NamedSessionPasteboard(namespace: "com.aram2k.gusan.settings", accessAllowed: pasteboardAccessAllowed)
    }
    init(defaults: UserDefaults, shared: SessionBackend) { self.defaults = defaults; self.shared = shared }
    private func synchronized<T>(_ body: () -> T) -> T {
        #if canImport(UIKit)
        if !Thread.isMainThread { return DispatchQueue.main.sync { self.synchronized(body) } }
        #endif
        lock.lock(); defer { lock.unlock() }; return body()
    }
    private func loadShared(force: Bool = false) {
        guard force || Date().timeIntervalSince(lastRead) > 0.5 else { return }
        lastRead = Date()
        cached = shared.read("session").data.flatMap { try? JSONSerialization.jsonObject(with: $0) as? [String: Any] } ?? [:]
        if let timestamp = cached["updatedAt"] as? Double, timestamp > defaults.double(forKey: "settingsUpdatedAt") {
            for key in keys { if let value = cached[key] { defaults.set(value, forKey: key) } }
            defaults.set(timestamp, forKey: "settingsUpdatedAt")
        }
    }
    public func refresh() { synchronized { loadShared(force: true) } }
    private func preference(_ key: String) -> Any? {
        synchronized {
            loadShared()
            if (cached["updatedAt"] as? Double ?? 0) > defaults.double(forKey: "settingsUpdatedAt") { return cached[key] ?? defaults.object(forKey: key) }
            return defaults.object(forKey: key)
        }
    }
    private func setPreference(_ value: Any, key: String) {
        synchronized {
            loadShared(force: true)
            var values = Dictionary(uniqueKeysWithValues: keys.compactMap { key in defaults.object(forKey: key).map { (key, $0) } })
            if (cached["updatedAt"] as? Double ?? 0) > defaults.double(forKey: "settingsUpdatedAt") {
                for key in keys { if let value = cached[key] { values[key] = value } }
            }
            values[key] = value
            let timestamp = Date().timeIntervalSince1970
            for (key, value) in values { defaults.set(value, forKey: key) }
            defaults.set(timestamp, forKey: "settingsUpdatedAt")
            values["updatedAt"] = timestamp; cached = values
            if let data = try? JSONSerialization.data(withJSONObject: values) { _ = shared.write(data, key: "session") }
        }
    }
    public var stripMode: StripMode {
        get { (preference("stripMode") as? String).flatMap(StripMode.init(rawValue:)) ?? .smart }
        set { setPreference(newValue.rawValue, key: "stripMode") }
    }
    public var defaultAction: SmartActionMode {
        get { (preference("defaultAction") as? String).flatMap(SmartActionMode.init(rawValue:)) ?? .smartAuto }
        set { setPreference(newValue.rawValue, key: "defaultAction") }
    }
    public var translateTarget: String {
        get { Prompts.normalizeTarget(preference("translateTarget") as? String ?? "en") }
        set { setPreference(Prompts.normalizeTarget(newValue), key: "translateTarget") }
    }
    public var keyboardAction: KeyboardDefaultAction {
        get { (preference("defaultAction") as? String).flatMap(KeyboardDefaultAction.init(rawValue:)) ?? .smartAuto }
        set { setPreference(newValue.rawValue, key: "defaultAction") }
    }
    public var keyClicks: Bool {
        get { preference("keyClicks") as? Bool ?? true }
        set { setPreference(newValue, key: "keyClicks") }
    }
    public var autoCapitalization: Bool {
        get { preference("autoCapitalization") as? Bool ?? true }
        set { setPreference(newValue, key: "autoCapitalization") }
    }
    public var doubleSpacePeriod: Bool {
        get { preference("doubleSpacePeriod") as? Bool ?? true }
        set { setPreference(newValue, key: "doubleSpacePeriod") }
    }
    public var haptics: Bool {
        get { preference("haptics") as? Bool ?? true }
        set { setPreference(newValue, key: "haptics") }
    }
    public var pasteboardFallback: Bool {
        get { preference("pasteboardFallback") as? Bool ?? true }
        set { setPreference(newValue, key: "pasteboardFallback") }
    }
    /// Compatibility API for older keyboard clients; cloud actions are always available.
    public var privacyMode: Bool {
        get { false }
        set {}
    }
}

public enum StripMode: String, CaseIterable {
    case smart, agent
}

public enum KeyboardDefaultAction: String, CaseIterable, Identifiable {
    case smartAuto = "smart_auto", rephrase, fixGrammar = "fix_grammar", writeEmail = "write_email", makeTodoList = "make_todo_list", translate, summarize
    public var id: String { rawValue }
    public var action: SmartAction {
        switch self {
        case .rephrase: return .rephrase
        case .fixGrammar: return .fixGrammar
        default: return .mode(SmartActionMode(rawValue: rawValue) ?? .smartAuto)
        }
    }
    public var label: String {
        switch self {
        case .smartAuto: return "Smart"
        case .rephrase: return "Rephrase"
        case .fixGrammar: return "Fix grammar"
        case .writeEmail: return "Email"
        case .makeTodoList: return "To-do"
        case .translate: return "Translate"
        case .summarize: return "Summarize"
        }
    }
}
