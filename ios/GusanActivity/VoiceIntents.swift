import AppIntents

/// What the system triggers (Control Center, Action button, Back Tap, Siri, the Live Activity) ask the app to do.
enum VoiceIntentCommand { case start, stop, toggle, cancel, endReady }

/// Set by the app at launch. Nil inside GusanActivity, which never runs these intents itself:
/// LiveActivityIntent and AudioRecordingIntent run in the app process.
enum VoiceIntentBridge {
    static var handler: (@MainActor (VoiceIntentCommand) async throws -> Void)?
    @MainActor static func run(_ command: VoiceIntentCommand) async throws {
        guard let handler else { throw VoiceIntentError.appUnavailable }
        try await handler(command)
    }
}

enum VoiceIntentError: Error, CustomLocalizedStringResourceConvertible {
    case appUnavailable
    var localizedStringResource: LocalizedStringResource { "Open Gusan once, then try again." }
}

/// Starts listening (or finishes the current take) without opening Gusan.
/// iOS 18+: an AudioRecordingIntent, so the app may record from the background while its Live Activity shows.
struct StartListeningIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Start Gusan"
    static var description = IntentDescription("Starts listening with Gusan. Tap again to finish and send.")
    static var openAppWhenRun = false
    static var isDiscoverable = true
    init() {}
    @MainActor func perform() async throws -> some IntentResult {
        try await VoiceIntentBridge.run(.toggle)
        return .result()
    }
}

@available(iOS 18.0, *)
extension StartListeningIntent: AudioRecordingIntent {}

#if compiler(>=6.2)
@available(iOS 26.0, *)
extension StartListeningIntent {
    static var supportedModes: IntentModes { .background }
}
#endif

/// The Live Activity's mic button: finish the take and send it.
struct StopListeningIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Finish and send"
    static var openAppWhenRun = false
    static var isDiscoverable = false
    init() {}
    @MainActor func perform() async throws -> some IntentResult {
        try await VoiceIntentBridge.run(.stop)
        return .result()
    }
}

/// The Live Activity's X button: drop the take.
struct CancelListeningIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Cancel Gusan"
    static var openAppWhenRun = false
    static var isDiscoverable = false
    init() {}
    @MainActor func perform() async throws -> some IntentResult {
        try await VoiceIntentBridge.run(.cancel)
        return .result()
    }
}

/// Ends the ready window (and the orange microphone dot) now.
struct EndReadyIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Stop Gusan"
    static var openAppWhenRun = false
    static var isDiscoverable = false
    init() {}
    @MainActor func perform() async throws -> some IntentResult {
        try await VoiceIntentBridge.run(.endReady)
        return .result()
    }
}
