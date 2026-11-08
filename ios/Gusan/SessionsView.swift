import SwiftUI
import GusanKit

struct SessionsView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var agent: AgentCenter
    @AppStorage("gusan.sessions.segment") private var segment = "smart"
    @State private var history: [SmartHistory.Entry] = []
    @State private var selectedID: UUID?

    var body: some View {
        List {
            Section {
                Picker("Sessions", selection: $segment) {
                    Text("Smart").tag("smart")
                    Text("Agent").tag("agent")
                }.pickerStyle(.segmented)
            }
            if segment == "smart" {
                Section {
                    if history.isEmpty { Text("No Smart results yet").foregroundStyle(.secondary) }
                    ForEach(history.reversed()) { entry in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(entry.result).lineLimit(2)
                            Text((entry.sourceApp.map { $0 + " · " } ?? "") + entry.date.formatted(date: .abbreviated, time: .shortened))
                                .font(.caption).foregroundStyle(.secondary)
                        }
                    }
                }
            } else {
                Section {
                    if agent.loading { ProgressView("Loading sessions…").tint(Theme.agent) }
                    if let error = agent.error { Text(error).foregroundStyle(Theme.danger) }
                    if agent.tasks.isEmpty && !agent.loading { Text("No agent sessions yet").foregroundStyle(.secondary) }
                    ForEach(agent.tasks) { task in
                        Button { selectedID = task.id } label: { TaskRow(task: task) }.buttonStyle(.plain)
                    }
                }
            }
        }
        .navigationTitle("Sessions")
        .tint(segment == "agent" ? Theme.agent : Theme.accent)
        .navigationDestination(isPresented: Binding(get: { selectedID != nil }, set: { if !$0 { selectedID = nil } })) {
            if let id = selectedID { TaskDetailView(id: id).tint(Theme.agent) }
        }
        .task { reloadHistory(); await agent.refresh(); openPendingSession() }
        .refreshable { reloadHistory(); await agent.refresh() }
        .onAppear { reloadHistory(); openPendingSession() }
        .onChange(of: model.openSessionID) { _ in openPendingSession() }
        .onChange(of: agent.openTaskID) { id in
            if let id { model.openSessionID = id; agent.openTaskID = nil }
        }
    }

    private func reloadHistory() { history = SmartHistory(group: model.store.resolution.chosen).entries }
    private func openPendingSession() {
        guard let id = model.openSessionID ?? agent.openTaskID else { return }
        segment = "agent"; selectedID = id
        model.openSessionID = nil; agent.openTaskID = nil
    }
}
