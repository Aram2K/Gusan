import SwiftUI
import GusanKit

struct HomeView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var agent: AgentCenter
    @EnvironmentObject private var screen: ScreenMonitor
    @EnvironmentObject private var voice: VoiceSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var mode: StripMode = .smart
    @AppStorage("gusan.setup.dismissed") private var setupDismissed = false
    @State private var instruction = ""
    @State private var starting = false
    @State private var error: String?
    @State private var lastResult: SmartHistory.Entry?
    @State private var showScreen = false
    private var notificationsAllowed: Bool {
        [.authorized, .provisional, .ephemeral].contains(agent.notificationStatus)
    }
    private var setupComplete: Bool {
        model.session != nil && model.keyboardAdded && model.fullAccess == true && notificationsAllowed
    }

    var body: some View {
        List {
            Section {
                Picker("Mode", selection: $mode) {
                    Text("Smart").tag(StripMode.smart)
                    Text("Agent").tag(StripMode.agent)
                }.pickerStyle(.segmented)
            } header: { Text("Mode") } footer: {
                Text("The strip opens in this mode. You can also switch from the chip in the strip.")
            }
            if !setupDismissed && !setupComplete { setupChecklist }
            if mode == .agent { agentSections } else { smartSections }
            Section {
                NavigationLink { KeyboardSetupView() } label: {
                    HStack {
                        Text("Keyboard · Full Access · Mic")
                        Spacer()
                        Text(model.ready && voice.permission == .granted ? "On" : "Set up")
                            .foregroundStyle(model.ready && voice.permission == .granted ? Theme.success : Theme.accent)
                    }
                }
            }
            Section {
                NavigationLink { ScreenView() } label: {
                    Label {
                        VStack(alignment: .leading, spacing: 4) {
                            Text("Gusan Screen")
                            Text(screen.isLive ? "On · sees the screen when you speak" : "Sees the screen when you speak")
                                .font(.subheadline).foregroundStyle(.secondary)
                        }
                    } icon: { Image(systemName: "iphone").foregroundStyle(Theme.accent) }
                }
            } footer: {
                Text("Screen sharing requires the iOS broadcast sheet. Deselect the screen icon in the strip for a private moment.")
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Gusan")
        .toolbar { ToolbarItem(placement: .navigationBarTrailing) { BrandMark() } }
        .navigationDestination(isPresented: $showScreen) { ScreenView() }
        .onAppear {
            model.settings.refresh(); mode = model.settings.stripMode
            model.refreshDiagnostics(); voice.refreshPermission()
            lastResult = SmartHistory(group: model.store.resolution.chosen).entries.last
            if model.openScreenPage { showScreen = true; model.openScreenPage = false }
        }
        .onChange(of: scenePhase) {
            if $0 == .active { model.settings.refresh(); mode = model.settings.stripMode }
        }
        .onChange(of: mode) {
            if model.settings.stripMode != $0 { model.settings.stripMode = $0 }
        }
        .onChange(of: model.session) { _ in lastResult = SmartHistory(group: model.store.resolution.chosen).entries.last }
        .onChange(of: model.openScreenPage) { if $0 { showScreen = true; model.openScreenPage = false } }
        .task { await agent.refreshNotificationStatus() }
    }

    private var setupChecklist: some View {
        Section {
            checklistRow("Sign in", complete: model.session != nil) { model.showingSignIn = true }
            NavigationLink { KeyboardSetupView() } label: {
                checklistLabel("Add keyboard", complete: model.keyboardAdded)
            }
            checklistRow("Allow Full Access", complete: model.fullAccess == true) { Theme.openSettings() }
            checklistRow("Allow notifications", complete: notificationsAllowed) {
                if agent.notificationStatus == .denied { Theme.openSettings() }
                else { Task { await agent.requestNotifications() } }
            }
            Button("Dismiss checklist") { setupDismissed = true }.font(.footnote)
        } header: { Text("Get ready") } footer: {
            Text("Open the Gusan keyboard once to check keyboard setup and Full Access.")
        }
    }

    private func checklistRow(_ title: String, complete: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) { checklistLabel(title, complete: complete) }.disabled(complete)
    }
    private func checklistLabel(_ title: String, complete: Bool) -> some View {
        Label(title, systemImage: complete ? "checkmark.circle.fill" : "circle")
            .foregroundStyle(complete ? Theme.success : Theme.accent).frame(minHeight: 36)
    }

    @ViewBuilder private var smartSections: some View {
        Section {
            Label("Speak from your keyboard", systemImage: "mic.fill")
            Text("Open Gusan with the globe key, tap the mic and say what you need. Select text to edit it; the result goes back where you’re typing.")
                .foregroundStyle(.secondary)
        }
        if let lastResult {
            Section("Last result") {
                Button {
                    UserDefaults.standard.set("smart", forKey: "gusan.sessions.segment")
                    model.selectedTab = .sessions
                } label: {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(lastResult.result).lineLimit(2).foregroundStyle(.primary)
                        Text(lastResult.date.formatted(date: .abbreviated, time: .shortened))
                            .font(.caption).foregroundStyle(.secondary)
                    }
                }
            }
        }
    }

    @ViewBuilder private var agentSections: some View {
        Section {
            TextField("What should Gusan do?", text: $instruction, axis: .vertical).lineLimit(2...5)
            Button {
                let text = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
                Task {
                    starting = true; error = nil
                    defer { starting = false }
                    do {
                        let intent = AgentIntent.detect(text) ?? AgentIntent(mode: .research, instruction: text)
                        let task = try await agent.start(intent)
                        instruction = ""; model.openSessionID = task.id; model.selectedTab = .sessions
                    } catch { self.error = error.localizedDescription }
                }
            } label: {
                HStack { if starting { ProgressView().tint(Theme.agent) }; Text(starting ? "Starting…" : "Start session") }
            }.disabled(starting || !agent.signedIn || agent.atActiveLimit || instruction.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
            if !agent.signedIn { Button("Sign in to start") { model.showingSignIn = true } }
            if let error { Text(error).foregroundStyle(Theme.danger) }
        } header: { Text("Agent") } footer: {
            Text("Gusan researches the web. It asks before submitting, buying or sending anything.")
        }
        .tint(Theme.agent)
        Section("Recent agent sessions") {
            if agent.tasks.isEmpty { Text("No agent sessions yet").foregroundStyle(.secondary) }
            ForEach(Array(agent.tasks.prefix(3))) { task in
                Button { model.openSessionID = task.id; model.selectedTab = .sessions } label: { TaskRow(task: task) }
                    .buttonStyle(.plain)
            }
        }
    }
}
