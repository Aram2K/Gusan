import SwiftUI
import AVFoundation
import GusanKit

fileprivate extension VoiceSession {
    var inAppError: String {
        switch error {
        case VoiceError.notSignedIn.localizedDescription: return "Sign in to use Gusan."
        case VoiceError.unauthorized.localizedDescription: return "Your sign-in has expired. Sign in again to continue."
        case VoiceError.notShared.localizedDescription: return "Gusan couldn't connect to the keyboard. Check Full Access in Settings."
        case VoiceError.fallbackOff.localizedDescription: return "Enable pasteboard fallback in Advanced settings to connect the keyboard."
        default: return error
        }
    }
}

struct VoiceListeningView: View {
    @ObservedObject var voice: VoiceSession
    let onSignIn: () -> Void
    private var title: String {
        if voice.starting { return "Starting…" }
        switch voice.status.phase {
        case .recording: return "Listening"
        case .transcribing: return "Transcribing…"
        case .thinking: return "Working…"
        case .done: return "Done"
        case .failed: return "Couldn't finish"
        case .ready, .off: return "Ready"
        }
    }
    var body: some View {
        NavigationStack {
            VStack(spacing: 20) {
                Spacer()
                if voice.starting { ProgressView().controlSize(.large) }
                else { VoiceLevelBars(level: voice.status.level, active: voice.status.phase == .recording).frame(height: 44).accessibilityHidden(true) }
                Text(title).font(.title2).bold()
                if voice.error.isEmpty {
                    Text("Swipe back to your app.").foregroundStyle(.secondary)
                } else {
                    Text(voice.inAppError).foregroundStyle(.secondary).multilineTextAlignment(.center)
                    if voice.error == VoiceError.notSignedIn.localizedDescription || voice.error == VoiceError.unauthorized.localizedDescription {
                        Button("Sign in", action: onSignIn).buttonStyle(.borderedProminent)
                    } else if voice.permission == .denied {
                        Button("Open Settings") { Theme.openSettings() }.buttonStyle(.bordered)
                    }
                }
                Spacer()
                if voice.alive {
                    Button("Stop listening", role: .destructive) { voice.stop() }
                        .buttonStyle(.bordered).controlSize(.large)
                }
            }.padding(24).frame(maxWidth: .infinity, maxHeight: .infinity).background(GrainBackground())
                .navigationTitle("Gusan").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { voice.closePresentation() } } }
        }
    }
}

struct VoiceSettingsSection: View {
    @ObservedObject var voice: VoiceSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var language = "auto"
    @State private var minutes = 2
    var body: some View {
        Section("Voice") {
            LabeledContent("Session", value: voice.status.phase.rawValue.capitalized)
            Button {
                if voice.alive { voice.stop() } else { voice.start() }
            } label: {
                HStack {
                    Text(voice.alive ? "Stop voice session" : "Start voice session")
                    if voice.starting { ProgressView() }
                }.frame(minHeight: 44).contentShape(Rectangle())
            }.disabled(voice.starting)
            if voice.permission == .denied { Button("Open microphone settings") { Theme.openSettings() } }
            if !voice.error.isEmpty { Label(voice.inAppError, systemImage: "exclamationmark.circle").foregroundStyle(.secondary) }
            Picker("Language I speak", selection: $language) { ForEach(VoiceSettings.languages, id: \.code) { Text($0.name).tag($0.code) } }.onChange(of: language) { voice.settings.language = $0 }
            Picker("Ready window", selection: $minutes) { ForEach(VoiceSettings.idleOptions, id: \.self) { Text($0 == 0 ? "Until I stop" : $0 == 60 ? "1 hour" : "\($0) min").tag($0) } }.onChange(of: minutes) { voice.settings.idleMinutes = $0 }
            Text("An open session reserves the mic and uses some battery. iOS may end a background session early.").font(.footnote).foregroundStyle(.secondary)
        }.onAppear { language = voice.settings.language; minutes = voice.settings.idleMinutes; voice.refreshPermission() }
            .onChange(of: scenePhase) { if $0 == .active { voice.refreshPermission() } }
    }
}

/// Seven bars that follow the microphone level while recording; idle bars rest at a low height.
struct VoiceLevelBars: View {
    let level: Float
    let active: Bool
    private let weights: [CGFloat] = [0.45, 0.7, 0.9, 1, 0.9, 0.7, 0.45]
    var body: some View {
        HStack(spacing: 5) {
            ForEach(weights.indices, id: \.self) { index in
                Capsule()
                    .fill(Theme.accent)
                    .frame(width: 6, height: height(for: index))
            }
        }
        .animation(.easeOut(duration: 0.18), value: level)
        .animation(.easeInOut(duration: 0.25), value: active)
    }
    private func height(for index: Int) -> CGFloat {
        let floor: CGFloat = 8
        guard active else { return floor }
        let strength = CGFloat(min(max(level, 0), 1))
        let wobble = 0.85 + 0.15 * sin(CGFloat(index) * 1.7 + strength * 6)
        return floor + (44 - floor) * strength * weights[index] * wobble
    }
}
