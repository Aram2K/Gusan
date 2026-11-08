import SwiftUI
import GusanKit

struct HomeView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var agent: AgentCenter
    @EnvironmentObject private var voice: VoiceSession
    @Environment(\.scenePhase) private var scenePhase
    @State private var mode: StripMode = .smart
    @AppStorage("gusan.setup.dismissed") private var setupDismissed = false
    @AppStorage("gusan.setup.completed") private var setupCompleted = false
    @State private var instruction = ""
    @State private var starting = false
    @State private var error: String?
    @State private var lastResult: SmartHistory.Entry?
    private var setupComplete: Bool {
        !model.restoringSession && model.session != nil && model.keyboardAdded && model.fullAccess == true && voice.permission == .granted
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
            if !model.restoringSession && !setupDismissed && !setupCompleted && !setupComplete { setupChecklist }
            if mode == .agent { agentSections } else { smartSections }
            Section {
                NavigationLink { KeyboardSetupView() } label: {
                    HStack {
                        Text("Keyboard")
                        Spacer()
                        Text(model.keyboardAdded && model.fullAccess == true ? "On" : "Settings")
                            .foregroundStyle(model.keyboardAdded && model.fullAccess == true ? Theme.success : Theme.accent)
                    }
                }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Gusan")
        .toolbar { ToolbarItem(placement: .navigationBarTrailing) { BrandMark() } }
        .onAppear {
            model.settings.refresh(); mode = model.settings.stripMode
            model.refreshDiagnostics(); voice.refreshPermission()
            lastResult = SmartHistory(group: model.store.resolution.chosen).entries.last
            if setupComplete { setupCompleted = true }
        }
        .onChange(of: scenePhase) {
            if $0 == .active { model.settings.refresh(); mode = model.settings.stripMode }
        }
        .onChange(of: mode) {
            if model.settings.stripMode != $0 { model.settings.stripMode = $0 }
        }
        .onChange(of: model.session) { _ in lastResult = SmartHistory(group: model.store.resolution.chosen).entries.last }
        .onChange(of: setupComplete) { if $0 { setupCompleted = true } }
        .onChange(of: scenePhase) { if $0 == .active { voice.refreshPermission(); model.refreshDiagnostics() } }
        .task { await agent.refreshNotificationStatus() }
    }

    private var setupChecklist: some View {
        Section {
            if model.session == nil {
                checklistRow("Sign in", complete: false) { model.showingSignIn = true }
            }
            if voice.permission != .granted {
                Button {
                    if voice.permission == .denied { Theme.openSettings() }
                    else { voice.requestMicrophonePermission() }
                } label: {
                    HStack {
                        checklistLabel("Allow microphone", complete: false)
                        if voice.requestingMicrophone { ProgressView() }
                    }.contentShape(Rectangle())
                }.disabled(voice.requestingMicrophone)
            }
            if !model.keyboardAdded {
                NavigationLink { KeyboardSetupView() } label: { checklistLabel("Add keyboard", complete: false) }
            }
            if model.fullAccess != true { checklistRow("Allow Full Access", complete: false) { Theme.openSettings() } }
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
            .foregroundStyle(complete ? Theme.success : Theme.accent)
            .frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
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
                guard !starting else { return }
                let text = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
                starting = true; error = nil
                Task {
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
