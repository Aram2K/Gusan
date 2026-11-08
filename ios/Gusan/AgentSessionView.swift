import GusanKit
import SwiftUI

struct AgentSessionView: View {
    let id: UUID
    @EnvironmentObject private var agent: AgentCenter
    @State private var message = ""
    @State private var sending = false
    @State private var loading = true
    @State private var error: String?

    private var tasks: [AgentTask] {
        agent.threads.first { $0.id == agent.sessionStore.sessionID(for: id) }?.tasks ?? []
    }
    private var latest: AgentTask? { tasks.last }
    private var canSend: Bool {
        guard let latest else { return false }
        return latest.status.isTerminal && !sending && !agent.atActiveLimit && !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && message.count <= 4_000
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    if loading { ProgressView("Loading session…").frame(maxWidth: .infinity) }
                    if tasks.isEmpty && !loading {
                        Text("This session is unavailable. Sign in and try again.").foregroundStyle(.secondary)
                        Button("Try again") { Task { await agent.loadSession(id) } }
                    }
                    ForEach(tasks) { task in
                        AgentSessionTurn(task: task).id(task.id)
                    }
                    if let error = error ?? agent.error { Text(error).foregroundStyle(Theme.danger) }
                }.padding(20).frame(maxWidth: 680).frame(maxWidth: .infinity)
            }
            .onChange(of: tasks.map(\.id)) { ids in
                if let last = ids.last { proxy.scrollTo(last, anchor: .bottom) }
            }
        }
        .background(Theme.surface)
        .navigationTitle(tasks.first?.title ?? "Agent session")
        .navigationBarTitleDisplayMode(.inline)
        .tint(Theme.agent)
        .safeAreaInset(edge: .bottom) { composer }
        .task(id: id) {
            loading = true; await agent.loadSession(id); loading = false
            markSeen()
        }
        .onChange(of: tasks) { _ in markSeen() }
        .refreshable { await agent.refresh(); await agent.loadSession(id) }
    }

    private var composer: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let latest, !latest.status.isTerminal {
                Text("Send a follow-up when Gusan finishes, or stop the task first.").font(.caption).foregroundStyle(.secondary)
            }
            if agent.atActiveLimit { Text("Wait for a running task to finish before sending.").font(.caption).foregroundStyle(.secondary) }
            if message.count > 4_000 { Text("Keep follow-ups under 4,000 characters.").font(.caption).foregroundStyle(Theme.danger) }
            HStack(alignment: .bottom) {
                TextField("Message the agent…", text: $message, axis: .vertical)
                    .lineLimit(1...5).textFieldStyle(.roundedBorder)
                Button {
                    guard canSend, let latest else { return }
                    let text = message.trimmingCharacters(in: .whitespacesAndNewlines)
                    sending = true; error = nil
                    Task {
                        defer { sending = false }
                        do { try await agent.continueTask(latest.id, message: text); message = "" }
                        catch { self.error = error.localizedDescription }
                    }
                } label: {
                    Group {
                        if sending { ProgressView() }
                        else { Image(systemName: "arrow.up.circle.fill").font(.title) }
                    }.frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
                }.disabled(!canSend).accessibilityLabel(sending ? "Sending" : "Send")
            }
        }.padding().background(.bar)
    }

    private func markSeen() {
        for task in tasks { agent.markSeen(task.id) }
        agent.endFinishedActivity(for: id)
    }
}

private struct AgentSessionTurn: View {
    let task: AgentTask
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var agent: AgentCenter
    @State private var working = false
    @State private var activeAction: String?
    @State private var error: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Spacer(minLength: 36)
                VStack(alignment: .leading, spacing: 6) {
                    Text(agent.message(for: task)).textSelection(.enabled)
                    Text(task.createdAt.formatted(date: .abbreviated, time: .shortened)).font(.caption).foregroundStyle(.secondary)
                }.padding(14).background(Theme.agent.opacity(0.12), in: RoundedRectangle(cornerRadius: 16))
            }
            if !task.steps.isEmpty {
                VStack(alignment: .leading, spacing: 8) {
                    ForEach(Array(task.steps.enumerated()), id: \.offset) { _, step in
                        Label(step.text, systemImage: "circle.fill").font(.footnote).foregroundStyle(.secondary)
                    }
                }
            }
            phaseContent
            if let error { Text(error).foregroundStyle(Theme.danger) }
        }.disabled(working)
    }

    @ViewBuilder private var phaseContent: some View {
        switch task.phase {
        case .needsConfirmation:
            AppCard {
                Text(agent.needsReconfirm(task) ? "Confirm again" : "Needs your OK").font(.headline).foregroundStyle(Theme.agent)
                Text(task.confirmation?.summary ?? "Gusan needs your OK to continue.")
                Text("Nothing is submitted, bought or sent until you confirm.").font(.footnote).foregroundStyle(.secondary)
                DisclosureGroup("Details") {
                    if let url = task.confirmation?.startURL { Text(url.absoluteString).textSelection(.enabled) }
                    ForEach(Array((task.confirmation?.steps ?? []).enumerated()), id: \.offset) { index, step in
                        Text("\(index + 1). \(step)").font(.subheadline)
                    }
                    if task.confirmation == nil { Text("Plan details are not available yet.").foregroundStyle(.secondary) }
                }
                HStack {
                    Button(role: .destructive) { perform("Cancel") { await agent.cancel(task.id) } } label: { actionLabel("Cancel") }.buttonStyle(.bordered)
                    Spacer()
                    Button { perform("Confirm") { await agent.confirm(task.id) } } label: { actionLabel("Confirm") }.buttonStyle(.borderedProminent)
                }
            }
        case .needsLogin, .needsCode:
            AppCard {
                Label(task.phase.label, systemImage: "key.fill").font(.headline).foregroundStyle(Theme.agent)
                Text("Gusan Cloud needs \(task.phase == .needsCode ? "a login code" : "a login") for \(task.confirmation?.startURL?.host ?? "this site") from Vault. The agent never sees your credentials.")
                HStack {
                    Button(role: .destructive) { perform("Stop") { await agent.stop(task.id) } } label: { actionLabel("Stop") }
                    Spacer()
                    Button("Open Vault") { model.selectedTab = .vault }.buttonStyle(.borderedProminent)
                }
            }
        case .queued, .working:
            HStack {
                ProgressView().tint(Theme.agent)
                Text(task.phase == .queued ? "Queued" : "Working").foregroundStyle(.secondary)
                Spacer()
                Button(role: .destructive) { perform("Stop") { await agent.stop(task.id) } } label: { actionLabel("Stop") }
            }
        case .completed:
            AppCard {
                HStack {
                    Label("Done", systemImage: "checkmark.circle").foregroundStyle(Theme.success)
                    Spacer()
                    if let result = task.result { CopyTextButton(text: result.plainText) }
                }
                if let result = task.result {
                    Text(result.plainText).textSelection(.enabled)
                    ForEach(result.images, id: \.self) { url in
                        Link(destination: url) {
                            AsyncImage(url: url) { phase in
                                switch phase {
                                case .success(let image):
                                    image.resizable().scaledToFit().frame(maxHeight: 180)
                                case .failure:
                                    Label("Open image", systemImage: "photo")
                                default:
                                    ProgressView().frame(maxWidth: .infinity, minHeight: 100)
                                }
                            }
                            .clipShape(RoundedRectangle(cornerRadius: 12))
                        }.accessibilityLabel("Open full-size result image")
                    }
                    ForEach(result.links, id: \.self) { link in
                        Link(link.title ?? link.url.host ?? link.url.absoluteString, destination: link.url)
                    }
                } else { Text("Gusan finished without a text result.").foregroundStyle(.secondary) }
            }
        case .failed, .timedOut:
            AppCard {
                Label(task.phase.label, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.danger)
                Text(task.summaryLine)
                if agent.canRunAgain(task) {
                    Button {
                        perform("Try again") { do { try await agent.runAgain(task) } catch { self.error = error.localizedDescription } }
                    } label: { actionLabel("Try again") }.disabled(agent.atActiveLimit)
                } else {
                    Text("The original input is no longer available. Start again from Home.").font(.footnote).foregroundStyle(.secondary)
                }
            }
        case .stopped, .cancelled:
            Label("Stopped", systemImage: "stop.circle").foregroundStyle(.secondary)
        }
    }

    private func actionLabel(_ title: String) -> some View {
        HStack {
            if activeAction == title { ProgressView() }
            Text(title)
        }.frame(minWidth: 44, minHeight: 44).contentShape(Rectangle())
    }

    private func perform(_ title: String, _ action: @escaping () async -> Void) {
        guard !working else { return }
        working = true; activeAction = title; error = nil
        Task { defer { working = false; activeAction = nil }; await action() }
    }
}
