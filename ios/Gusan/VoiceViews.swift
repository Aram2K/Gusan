import SwiftUI
import AVFoundation
import GusanKit

fileprivate extension VoiceSession {
    var inAppError: String {
        switch error {
        case VoiceError.notSignedIn.localizedDescription: return "Sign in to start a voice session."
        case VoiceError.unauthorized.localizedDescription: return "Your sign-in has expired. Sign in again to continue."
        case VoiceError.notShared.localizedDescription: return "Voice can't connect to your keyboard. Check Keyboard settings, then try again."
        case VoiceError.fallbackOff.localizedDescription: return "Voice sharing is off. Enable pasteboard fallback in Keyboard settings to connect your keyboard."
        case VoiceError.sessionEnded.localizedDescription: return "This voice session has ended. Tap Record to start a new one."
        default: return error.hasPrefix("Audio failed:") ? "Your microphone couldn't start. Check microphone access in Settings, then try again." : error
        }
    }
}

struct VoiceReadyView: View {
    @ObservedObject var voice: VoiceSession
    let onSignIn: () -> Void
    @Environment(\.dismiss) private var dismiss
    @Environment(\.scenePhase) private var scenePhase
    private var statusText: String? {
        switch voice.status.phase {
        case .off: return nil
        case .ready: return "Ready"
        case .recording: return "Listening…"
        case .transcribing: return "Transcribing…"
        case .thinking: return "Thinking…"
        case .done: return "Done"
        case .failed: return "Couldn't finish"
        }
    }
    var body: some View {
        NavigationStack {
            ScreenContent {
                AppCard {
                    BrandMark(large: true)
                    Text("Voice dictation").font(.largeTitle).bold().accessibilityAddTraits(.isHeader)
                    if voice.keyboardActivation {
                        Text("Gusan needs to start the mic once — swipe back to your app.").font(.headline)
                    }
                    Text(voice.alive ? "Your voice session is ready. Switch back to your app, keep Gusan selected, and tap the mic to dictate." : "Speak naturally, right from your Gusan keyboard. Tap Record to prepare a voice session, then return to your app.")
                        .font(.body).foregroundStyle(.secondary)
                    Text("The microphone stays ready between dictations until you stop this session or the idle timeout ends it.").font(.footnote).foregroundStyle(.secondary)
                    if !voice.alive && voice.error.isEmpty {
                        PrimaryButton(title: "Record", icon: "mic.fill") { voice.start() }
                    }
                }
                if let statusText, voice.error.isEmpty {
                    AppCard {
                        Label(statusText, systemImage: voice.status.phase == .failed ? "exclamationmark.circle" : voice.alive ? "waveform" : "mic")
                            .font(.headline).foregroundStyle(voice.status.phase == .failed ? Color.secondary : Theme.accent)
                        if let end = voice.status.sessionEndsAt, voice.alive {
                            Text("Session until \(end.formatted(date: .omitted, time: .shortened))").font(.body).foregroundStyle(.secondary)
                        }
                    }
                }
                if !voice.error.isEmpty {
                    AppCard {
                        Label("Voice needs attention", systemImage: "exclamationmark.circle").font(.headline).foregroundStyle(.secondary)
                        Text(voice.inAppError).font(.body).foregroundStyle(.secondary)
                        if voice.error == VoiceError.notSignedIn.localizedDescription || voice.error == VoiceError.unauthorized.localizedDescription {
                            PrimaryButton(title: "Sign in", icon: "person.crop.circle") { onSignIn(); dismiss() }
                        } else if voice.error == VoiceError.fallbackOff.localizedDescription || voice.error == VoiceError.notShared.localizedDescription {
                            PrimaryButton(title: "Keyboard settings", icon: "keyboard") { dismiss(); UIApplication.shared.open(URL(string: "gusan://keyboard-settings")!) }
                        } else if voice.permission == .denied {
                            PrimaryButton(title: "Open Settings", icon: "gear") { Theme.openSettings() }
                        } else if !voice.alive {
                            PrimaryButton(title: "Record", icon: "mic.fill") { voice.start() }
                        }
                    }
                }
                if voice.alive {
                    AppCard {
                        Button { voice.stop() } label: {
                            Label("Stop voice session", systemImage: "stop.circle").frame(maxWidth: .infinity, minHeight: 44)
                        }.buttonStyle(.bordered).tint(Theme.accent)
                    }
                }
            }.navigationTitle("Voice").navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) {
                        Button { dismiss() } label: {
                            Image(systemName: "xmark").font(.system(size: 17, weight: .semibold)).frame(width: 44, height: 44)
                        }.accessibilityLabel("Close voice")
                    }
                }
                .onAppear { voice.refreshPermission() }
                .onChange(of: scenePhase) { phase in
                    if phase == .active { voice.refreshPermission() }
                }
        }
    }
}

struct VoiceSettingsSection: View {
    @ObservedObject var voice: VoiceSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var language = "auto"
    @State private var action = VoiceAction.transcribeOnly
    @State private var minutes = 5
    @State private var autoReturn = true
    var body: some View {
        Section("Voice") {
            LabeledContent("Session", value: voice.status.phase.rawValue.capitalized)
            Button(voice.alive ? "Stop voice session" : "Start voice session") { if voice.alive { voice.stop() } else { voice.start() } }
            LabeledContent("Microphone", value: voice.permission == .granted ? "Allowed" : voice.permission == .denied ? "Denied" : "Not requested")
            if voice.permission == .denied { Button("Open Settings") { UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!) } }
            if !voice.error.isEmpty { Label(voice.inAppError, systemImage: "exclamationmark.circle").foregroundStyle(.secondary) }
            Picker("Spoken language", selection: $language) { ForEach(VoiceSettings.languages, id: \.code) { Text($0.name).tag($0.code) } }.onChange(of: language) { voice.settings.language = $0 }
            Picker("Default voice action", selection: $action) { ForEach(VoiceAction.all, id: \.self) { Text($0.label).tag($0) } }.onChange(of: action) { voice.settings.defaultAction = $0 }
            Picker("Keep session alive", selection: $minutes) { ForEach(VoiceSettings.idleOptions, id: \.self) { Text("\($0) min").tag($0) } }.onChange(of: minutes) { voice.settings.idleMinutes = $0 }
            Toggle("Return to the previous app automatically (experimental)", isOn: $autoReturn).onChange(of: autoReturn) { voice.settings.autoReturn = $0 }
        }.onAppear { language = voice.settings.language; action = voice.settings.defaultAction; minutes = voice.settings.idleMinutes; autoReturn = voice.settings.autoReturn; voice.refreshPermission() }
            .onChange(of: scenePhase) { if $0 == .active { voice.refreshPermission() } }
    }
}
