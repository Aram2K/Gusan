import SwiftUI
import AVFoundation
import GusanKit

struct VoiceReadyView: View {
    @ObservedObject var voice: VoiceSession
    let onSignIn: () -> Void
    @Environment(\.dismiss) private var dismiss
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
            VStack(spacing: 24) {
                Image(systemName: "mic.fill").font(.system(size: 64)).foregroundStyle(.tint)
                Text(voice.alive ? "Recording ready — swipe back" : "Voice dictation").font(.title2).bold()
                if voice.alive {
                    Text("Tap ‹ at the top-left, or swipe right along the bottom edge, to go back to your app").multilineTextAlignment(.center)
                } else {
                    Text("Tap Record to prepare a voice session.").multilineTextAlignment(.center)
                    Button("Record") { voice.start() }.frame(minHeight: 44)
                }
                if let statusText { Text(statusText).foregroundStyle(.secondary) }
                if let end = voice.status.sessionEndsAt, voice.alive { Text("Session until \(end.formatted(date: .omitted, time: .shortened))") }
                if !voice.error.isEmpty { Text(voice.error).foregroundStyle(.red) }
                if voice.error == VoiceError.notSignedIn.localizedDescription {
                    Button("Sign in") { onSignIn(); dismiss() }.frame(minHeight: 44)
                }
                if voice.error == VoiceError.fallbackOff.localizedDescription {
                    Button("Keyboard settings") { dismiss(); UIApplication.shared.open(URL(string: "gusan://keyboard-settings")!) }.frame(minHeight: 44)
                }
                if voice.alive { Button("Stop voice session", role: .destructive) { voice.stop() }.frame(minHeight: 44) }
            }.padding().toolbar { Button("Close") { dismiss() } }
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
            if !voice.error.isEmpty { Text(voice.error).foregroundStyle(.red) }
            Picker("Spoken language", selection: $language) { ForEach(VoiceSettings.languages, id: \.code) { Text($0.name).tag($0.code) } }.onChange(of: language) { voice.settings.language = $0 }
            Picker("Default voice action", selection: $action) { ForEach(VoiceAction.all, id: \.self) { Text($0.label).tag($0) } }.onChange(of: action) { voice.settings.defaultAction = $0 }
            Picker("Keep session alive", selection: $minutes) { ForEach(VoiceSettings.idleOptions, id: \.self) { Text("\($0) min").tag($0) } }.onChange(of: minutes) { voice.settings.idleMinutes = $0 }
            Toggle("Return to the previous app automatically (experimental)", isOn: $autoReturn).onChange(of: autoReturn) { voice.settings.autoReturn = $0 }
        }.onAppear { language = voice.settings.language; action = voice.settings.defaultAction; minutes = voice.settings.idleMinutes; autoReturn = voice.settings.autoReturn; voice.refreshPermission() }
            .onChange(of: scenePhase) { if $0 == .active { voice.refreshPermission() } }
    }
}
