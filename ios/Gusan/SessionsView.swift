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
                        NavigationLink { SmartHistoryDetailView(entry: entry) } label: {
                            VStack(alignment: .leading, spacing: 4) {
                                Text(entry.instruction).lineLimit(1)
                                Text(entry.result).lineLimit(1).foregroundStyle(.secondary)
                                Text((entry.sourceApp.map { $0 + " · " } ?? "") + entry.date.formatted(date: .abbreviated, time: .shortened))
                                    .font(.caption).foregroundStyle(.secondary)
                            }
                        }
                        .swipeActions(edge: .trailing) {
                            Button(role: .destructive) {
                                history.removeAll { $0.id == entry.id }
                                SmartHistory(group: model.store.resolution.chosen).remove(entry.id)
                            } label: { Label("Delete", systemImage: "trash") }
                        }
                    }
                }
            } else {
                Section {
                    Button("Clear finished", role: .destructive) {
                        let ids = Set(agent.tasks.filter { $0.status.isTerminal }.map(\.id))
                        Task { await agent.deleteTasks(ids) }
                    }.disabled(!agent.tasks.contains { $0.status.isTerminal })
                    if agent.loading { ProgressView("Loading sessions…").tint(Theme.agent) }
                    if let error = agent.error { Text(error).foregroundStyle(Theme.danger) }
                    if agent.tasks.isEmpty && !agent.loading { Text("No agent sessions yet").foregroundStyle(.secondary) }
                    ForEach(agent.threads) { thread in
                        if let latest = thread.tasks.last {
                            Button { selectedID = thread.id } label: {
                                TaskRow(task: latest, attention: thread.needsAttention, title: thread.tasks.first?.title)
                            }.buttonStyle(.plain)
                                .swipeActions(edge: .trailing) {
                                    Button(role: .destructive) {
                                        let ids = Set(thread.tasks.map(\.id))
                                        Task { await agent.deleteTasks(ids) }
                                    } label: { Label("Delete", systemImage: "trash") }
                                }
                        }
                    }
                }
            }
        }
        .navigationTitle("Sessions")
        .tint(segment == "agent" ? Theme.agent : Theme.accent)
        .navigationDestination(isPresented: Binding(get: { selectedID != nil }, set: { if !$0 { selectedID = nil } })) {
            if let id = selectedID { AgentSessionView(id: id).id(id).tint(Theme.agent) }
        }
        .task { reloadHistory(); await agent.refresh(); openPendingSession() }
        .refreshable { reloadHistory(); await agent.refresh() }
        .onAppear { reloadHistory(); openPendingSession() }
        .onChange(of: model.openSessionID) { _ in openPendingSession() }
        .onChange(of: model.session) { _ in reloadHistory(); selectedID = nil }
    }

    private func reloadHistory() { history = SmartHistory(group: model.store.resolution.chosen).entries }
    private func openPendingSession() {
        guard let id = model.openSessionID else { return }
        segment = "agent"; selectedID = agent.sessionStore.sessionID(for: id)
        model.openSessionID = nil
    }
}

private struct SmartHistoryDetailView: View {
    let entry: SmartHistory.Entry
    var body: some View {
        List {
            Section("Instruction") { Text(entry.instruction).textSelection(.enabled) }
            Section("Result") {
                Text(entry.result).textSelection(.enabled)
                CopyTextButton(text: entry.result)
            }
            Section {
                Text(entry.date.formatted(date: .abbreviated, time: .shortened))
                if let source = entry.sourceApp { Text(source) }
            }.foregroundStyle(.secondary)
        }.navigationTitle("Smart result").navigationBarTitleDisplayMode(.inline)
    }
}
