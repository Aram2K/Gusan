import SwiftUI
import GusanKit

struct SettingsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var voice: VoiceSession
    @EnvironmentObject private var agent: AgentCenter
    @State private var keyClicks = true
    @State private var haptics = true

    var body: some View {
        Form {
            Section {
                NavigationLink { AccountView() } label: {
                    Label {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(model.session?.email ?? "Sign in to Gusan")
                            Text(model.session == nil ? "Connect your keyboard" : model.isPro ? "Gusan Pro" : "Free")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                    } icon: { Image(systemName: "person.crop.circle") }
                }
            }
            VoiceSettingsSection(voice: voice)
            Section {
                Toggle("Keyboard sounds", isOn: $keyClicks)
                Toggle("In-app haptics", isOn: $haptics)
            } footer: { Text("Keyboard sounds follow iOS sound settings. Haptic feedback applies inside the Gusan app.") }
            Section {
                LabeledContent("Notifications", value: notificationsLabel)
                Button(agent.notificationStatus == .denied ? "Open notification settings" : "Allow notifications") {
                    if agent.notificationStatus == .denied { Theme.openSettings() }
                    else { agent.requestNotifications() }
                }.disabled(agent.requestingNotifications || [.authorized, .provisional, .ephemeral].contains(agent.notificationStatus))
                if agent.requestingNotifications { ProgressView("Requesting permission…") }
            } footer: { Text("Session finished or needs you. On sideloaded builds, notifications arrive when Gusan next checks for updates.") }
            Section {
                NavigationLink { KeyboardSetupView() } label: { Label("Keyboard setup", systemImage: "keyboard") }
                NavigationLink { AdvancedSettingsView() } label: { Label("Advanced", systemImage: "wrench.and.screwdriver") }
            }
        }
        .navigationTitle("Settings")
        .onAppear { model.settings.refresh(); keyClicks = model.settings.keyClicks; haptics = model.settings.haptics }
        .onChange(of: keyClicks) { model.settings.keyClicks = $0 }
        .onChange(of: haptics) { model.settings.haptics = $0 }
        .task { await agent.refreshNotificationStatus() }
    }
    private var notificationsLabel: String {
        switch agent.notificationStatus {
        case .authorized, .provisional, .ephemeral: return "On"
        case .denied: return "Off"
        default: return "Not requested"
        }
    }
}

struct KeyboardSetupView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var voice: VoiceSession
    var body: some View {
        List {
            Section("Add Gusan") {
                Text("1. Open Settings → General → Keyboard → Keyboards.")
                Text("2. Tap Add New Keyboard, then choose Gusan.")
                Text("3. Tap Gusan and turn on Allow Full Access.")
                Button("Open iPhone Settings") { Theme.openSettings() }
            }
            Section("Check setup") {
                StatusRow(title: "Keyboard added", detail: model.keyboardCheck, confirmed: model.keyboardAdded)
                StatusRow(title: "Full Access", detail: model.fullAccess == true ? "On at the last check" : "Open the Gusan keyboard to check", confirmed: model.fullAccess == true)
                Button("Check again") { model.refreshDiagnostics() }
            }
        }
        .navigationTitle("Keyboard setup").navigationBarTitleDisplayMode(.inline)
        .onAppear { model.refreshDiagnostics(); voice.refreshPermission() }
    }
}

struct AdvancedSettingsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var autoCapitalization = true
    @State private var doubleSpacePeriod = true
    @State private var pasteboardFallback = true
    var body: some View {
        Form {
            Section("Typing") {
                Toggle("Auto-capitalization", isOn: $autoCapitalization)
                Toggle("Double-space for period", isOn: $doubleSpacePeriod)
            }
            Section {
                Toggle("Share sign-in via pasteboard fallback", isOn: $pasteboardFallback)
            } footer: { Text("Used only if sign-in or voice can't be shared after sideloading. Other apps signed with the same Apple ID could read it.") }
            ScreenSettingsSection()
            Section { NavigationLink { DiagnosticsView() } label: { Label("Diagnostics", systemImage: "stethoscope") } }
        }
        .navigationTitle("Advanced").navigationBarTitleDisplayMode(.inline)
        .onAppear {
            autoCapitalization = model.settings.autoCapitalization
            doubleSpacePeriod = model.settings.doubleSpacePeriod
            pasteboardFallback = model.settings.pasteboardFallback
        }
        .onChange(of: autoCapitalization) { model.settings.autoCapitalization = $0 }
        .onChange(of: doubleSpacePeriod) { model.settings.doubleSpacePeriod = $0 }
        .onChange(of: pasteboardFallback) {
            model.settings.pasteboardFallback = $0
            if !$0 { model.store.makeVoiceChannel().clearPasteboard() }
            _ = model.store.read(); model.refreshDiagnostics()
        }
    }
}
