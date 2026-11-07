import SwiftUI
import GusanKit

struct KeyboardSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var voice: VoiceSession
    @State private var action = KeyboardDefaultAction.smartAuto
    @State private var target = "en"
    @State private var keyClicks = true
    @State private var autoCapitalization = true
    @State private var doubleSpacePeriod = true
    @State private var haptics = true
    @State private var pasteboardFallback = true
    private let languages = [("en", "English"), ("hy", "Հայերեն"), ("ru", "Русский"), ("fr", "Français"), ("de", "Deutsch"), ("es", "Español"), ("it", "Italiano"), ("pt", "Português"), ("ar", "العربية")]
    var body: some View {
        Form {
            Section {
                Picker("Default action", selection: $action) { ForEach(KeyboardDefaultAction.allCases) { Text($0.label).tag($0) } }.frame(minHeight: 44)
                Picker("Translate to", selection: $target) { ForEach(languages, id: \.0) { Text($0.1).tag($0.0) } }.frame(minHeight: 44)
            } header: { Text("Gusan actions") } footer: { Text("Hold the Gusan button to run your default action.") }
            VoiceSettingsSection(voice: voice)
            Section {
                Toggle("Share sign-in via pasteboard fallback", isOn: $pasteboardFallback).frame(minHeight: 44)
            } footer: { Text("Only used if the keyboard can't see your sign-in after sideloading. Other apps signed with the same Apple ID could read it.") }
            Section("Typing") {
                Toggle("Key clicks", isOn: $keyClicks).frame(minHeight: 44)
                Toggle("Auto-capitalization", isOn: $autoCapitalization).frame(minHeight: 44)
                Toggle("Double-space for period", isOn: $doubleSpacePeriod).frame(minHeight: 44)
                Toggle("Haptic feedback", isOn: $haptics).frame(minHeight: 44)
            }
            Section {
                StatusRow(title: "Keyboard added", detail: model.keyboardAdded ? "Seen on this iPhone." : "Open Gusan once to check.", confirmed: model.keyboardAdded)
                Button("Set up keyboard") { model.onboardingStep = .addKeyboard; model.showingOnboarding = true }.frame(minHeight: 44)
                StatusRow(title: "Full Access", detail: model.fullAccess.map { $0 ? "Enabled at the last check." : "Turn on Allow Full Access in Settings." } ?? "Not checked yet.", confirmed: model.fullAccess == true)
                Button("Open Settings") { Theme.openSettings() }.frame(minHeight: 44)
                Text(model.keyboardCheck).font(.caption).foregroundStyle(.secondary)
            } header: { Text("Setup") }
            Section { NavigationLink { DiagnosticsView() } label: { Label("Diagnostics", systemImage: "stethoscope").font(.footnote).frame(minHeight: 44) } }
        }.navigationTitle("Keyboard")
            .onAppear { model.settings.refresh(); action = model.settings.keyboardAction; target = model.settings.translateTarget; keyClicks = model.settings.keyClicks; autoCapitalization = model.settings.autoCapitalization; doubleSpacePeriod = model.settings.doubleSpacePeriod; haptics = model.settings.haptics; model.refreshDiagnostics() }
            .onChange(of: action) { model.settings.keyboardAction = $0 }
            .onChange(of: target) { model.settings.translateTarget = $0 }
            .onChange(of: keyClicks) { model.settings.keyClicks = $0 }
            .onChange(of: autoCapitalization) { model.settings.autoCapitalization = $0 }
            .onChange(of: doubleSpacePeriod) { model.settings.doubleSpacePeriod = $0 }
            .onChange(of: haptics) { model.settings.haptics = $0 }
            .onAppear { pasteboardFallback = model.settings.pasteboardFallback }
            .onChange(of: pasteboardFallback) { model.settings.pasteboardFallback = $0; _ = model.store.read(); model.refreshDiagnostics() }
    }
}
