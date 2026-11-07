import GusanKit
import SwiftUI

/// Results/Activity: every agent task with its status, full results and confirm/cancel.
struct ActivityView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var agent: AgentCenter
    @State private var composing = false
    @State private var selection: UUID?

    var body: some View {
        Group {
            if agent.privacyMode {
                ScreenContent { privacyCard }
            } else if !agent.signedIn {
                ScreenContent {
                    AppCard {
                        Label("Sign in to use agent tasks", systemImage: "person.crop.circle.badge.exclamationmark").font(.headline)
                        Text("Agent tasks run on Gusan Cloud with your account and need Gusan Pro.").foregroundStyle(.secondary)
                        PrimaryButton(title: "Sign in") { model.handleURL(URL(string: "gusan://signin")!) }
                    }
                }
            } else if agent.tasks.isEmpty, let error = agent.error, !agent.loading {
                ScreenContent {
                    AppCard {
                        Label("Couldn't load your tasks", systemImage: "exclamationmark.triangle").font(.headline).foregroundStyle(Theme.danger)
                        Text(error).foregroundStyle(.secondary)
                        PrimaryButton(title: "Try again") { Task { await agent.refresh() } }
                    }
                }
            } else if agent.tasks.isEmpty && !agent.loading {
                ScreenContent {
                    emptyState
                    notificationsCard
                }
            } else {
                list
            }
        }
        .navigationTitle("Activity")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { composing = true } label: { Label("New task", systemImage: "plus") }
                    .disabled(!agent.signedIn || agent.privacyMode)
            }
        }
        .refreshable { await agent.refresh() }
        .sheet(isPresented: $composing) { NavigationStack { NewTaskView() } }
        .navigationDestination(isPresented: Binding(get: { selection != nil }, set: { if !$0 { selection = nil } })) {
            if let selection { TaskDetailView(id: selection) }
        }
        .task { await agent.refresh() }
        .onChange(of: agent.openTaskID) { if let id = $0 { selection = id; agent.openTaskID = nil } }
        .onAppear {
            if let id = agent.openTaskID ?? model.pendingTaskID { selection = id; agent.openTaskID = nil; model.pendingTaskID = nil }
        }
    }

    private var list: some View {
        List {
            if let error = agent.error {
                Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.danger) }
            }
            if agent.notificationStatus != .authorized {
                Section { notificationsCard.listRowInsets(EdgeInsets()).listRowBackground(Color.clear) }
            }
            let live = agent.tasks.filter { !$0.status.isTerminal }
            let done = agent.tasks.filter { $0.status.isTerminal }
            if !live.isEmpty {
                Section("In progress") { ForEach(live) { row($0) } }
            }
            if !done.isEmpty {
                Section("Results") { ForEach(done) { row($0) } }
            }
            Section {
                Text(agent.isDemo ? "Demo data: tasks here are simulated locally." : "Gusan checks for updates while it's open and when iOS lets it refresh in the background. Sideloaded builds can't receive push, so a finished task may show up a little later.")
                    .font(.footnote).foregroundStyle(.secondary)
                Text("Limits: \(AgentTask.maxActive) tasks at a time, \(AgentTask.maxPerDay) per day. A do-it task waits up to 15 minutes for your OK, then stops.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            Section {
                privacyToggle
            } footer: {
                Text("In Privacy Mode nothing is sent to agents and agent actions are hidden on the keyboard.")
            }
        }
        .listStyle(.insetGrouped)
    }

    private var privacyToggle: some View {
        Toggle("Privacy Mode", isOn: Binding(get: { agent.privacyMode }, set: { agent.setPrivacyMode($0) }))
            .frame(minHeight: 44)
            .accessibilityHint("Turns agent tasks off entirely")
    }

    private var privacyCard: some View {
        AppCard {
            Label("Privacy Mode is on", systemImage: "hand.raised.fill").font(.headline)
            Text("Agent tasks are off: nothing is sent to Gusan Cloud agents, and the keyboard treats every request as a text edit. Turn Privacy Mode off to use research and do-it tasks again.")
                .foregroundStyle(.secondary)
            privacyToggle
        }
    }

    private func row(_ task: AgentTask) -> some View {
        Button { selection = task.id } label: { TaskRow(task: task) }
            .buttonStyle(.plain)
            .swipeActions(edge: .trailing) {
                if !task.status.isTerminal {
                    Button(role: .destructive) { Task { await agent.cancel(task.id) } } label: { Label("Cancel", systemImage: "xmark") }
                }
            }
    }

    private var emptyState: some View {
        AppCard {
            Label("No agent tasks yet", systemImage: "sparkles").font(.headline)
            Text("Ask from the keyboard (\"find the cheapest flights to Rome\", \"book a table for two on Friday\") or start one here. Research tasks only read the web; do-it tasks stop and ask before anything is submitted, bought or sent.")
                .foregroundStyle(.secondary)
            PrimaryButton(title: "New task") { composing = true }
            privacyToggle
        }
    }

    private var notificationsCard: some View {
        AppCard {
            Label("Notifications", systemImage: "bell.badge").font(.headline)
            switch agent.notificationStatus {
            case .denied:
                Text("Notifications are off. Turn them on in Settings to hear when a task finishes or needs your OK.").foregroundStyle(.secondary)
                Button("Open Settings") { if let url = URL(string: UIApplication.openSettingsURLString) { UIApplication.shared.open(url) } }
                    .frame(minHeight: 44)
            case .authorized, .provisional, .ephemeral:
                Text("You'll get a local notification when a task finishes or needs your OK, as soon as Gusan next checks: while it's open, or when iOS runs its background refresh. There's no push on sideloaded builds.")
                    .foregroundStyle(.secondary)
            default:
                Text("Gusan can notify you when a task finishes or needs your OK. These are local notifications: they arrive when Gusan next checks, while it's open or during iOS background refresh (no push on sideloaded builds).")
                    .foregroundStyle(.secondary)
                PrimaryButton(title: "Allow notifications") { Task { await agent.requestNotifications() } }
            }
        }
    }
}

struct TaskRow: View {
    let task: AgentTask
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            StatusGlyph(status: task.status)
            VStack(alignment: .leading, spacing: 4) {
                Text(task.title).font(.body).foregroundStyle(.primary).lineLimit(2)
                Text(task.summaryLine).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                HStack(spacing: 6) {
                    Text(task.mode.label)
                    Text("·")
                    Text(task.status.label)
                    Text("·")
                    Text(task.createdAt, style: .relative) + Text(" ago")
                }
                .font(.caption).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
            if task.needsAttention { Circle().fill(Theme.accent).frame(width: 10, height: 10).accessibilityLabel("New") }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.status.label). \(task.mode.label). \(task.title). \(task.summaryLine)")
    }
}

struct StatusGlyph: View {
    let status: AgentStatus
    var body: some View {
        ZStack {
            Circle().fill(color.opacity(0.15)).frame(width: 36, height: 36)
            if status == .running || status == .queued { ProgressView().tint(color) } else { Image(systemName: symbol).foregroundStyle(color).font(.body.weight(.semibold)) }
        }
        .accessibilityHidden(true)
    }
    private var symbol: String {
        switch status {
        case .awaitingConfirmation: return "hand.raised.fill"
        case .completed: return "checkmark"
        case .failed, .timedOut: return "exclamationmark.triangle.fill"
        case .cancelled: return "xmark"
        default: return "hourglass"
        }
    }
    private var color: Color {
        switch status {
        case .awaitingConfirmation: return .orange
        case .completed: return Theme.success
        case .failed, .timedOut: return Theme.danger
        case .cancelled: return .secondary
        default: return Theme.accent
        }
    }
}

struct TaskDetailView: View {
    let id: UUID
    @EnvironmentObject private var agent: AgentCenter
    @State private var working = false
    @State private var copied = false
    @State private var loadingTask = false

    private var task: AgentTask? { agent.task(id) }

    var body: some View {
        ScreenContent {
            if let task {
                header(task)
                if task.confirmationExpired || task.status == .timedOut || task.status == .failed { runAgain(task) }
                if task.status == .awaitingConfirmation { confirmation(task) }
                if let result = task.result { resultCard(result) }
                if !task.steps.isEmpty { steps(task) }
                if !task.status.isTerminal && task.status != .awaitingConfirmation {
                    Button(role: .destructive) { Task { working = true; await agent.cancel(task.id); working = false } } label: {
                        Label("Cancel task", systemImage: "xmark.circle").frame(maxWidth: .infinity, minHeight: 44)
                    }
                    .buttonStyle(.bordered).disabled(working)
                }
            } else if loadingTask {
                ProgressView().frame(maxWidth: .infinity, minHeight: 120)
            } else {
                AppCard { Text("This task is no longer available.").foregroundStyle(.secondary) }
            }
        }
        .navigationTitle(task?.mode.label ?? "Task")
        .navigationBarTitleDisplayMode(.inline)
        .task { if task == nil { loadingTask = true; await agent.load(id); loadingTask = false }; agent.markSeen(id) }
        .onChange(of: task?.status) { _ in agent.markSeen(id) }
    }

    private func runAgain(_ task: AgentTask) -> some View {
        AppCard {
            if task.confirmationExpired {
                Label("Confirmation expired", systemImage: "clock.badge.xmark").font(.headline)
                Text("The agent waited 15 minutes for your OK and then stopped. Nothing was submitted, bought or sent.").foregroundStyle(.secondary)
            } else {
                Label(task.status.label, systemImage: "exclamationmark.triangle").font(.headline)
                Text(task.summaryLine).foregroundStyle(.secondary)
            }
            if agent.atActiveLimit {
                Text("You already have \(AgentTask.maxActive) tasks running; wait for one to finish before running this again.").font(.footnote).foregroundStyle(.secondary)
            }
            if agent.canRunAgain(task) {
                PrimaryButton(title: working ? "Starting…" : "Run again") {
                    Task { working = true; if let started = try? await agent.runAgain(task) { agent.openTaskID = started.id }; working = false }
                }
                .disabled(working || agent.atActiveLimit || agent.privacyMode)
            } else {
                Text("The full instruction isn't stored on this device; start it again from New task.").font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private func header(_ task: AgentTask) -> some View {
        AppCard {
            HStack(alignment: .top, spacing: 12) {
                StatusGlyph(status: task.status)
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.title).font(.headline)
                    Text(task.status.label).font(.subheadline).foregroundStyle(.secondary)
                    if task.status == .running || task.status == .queued, let step = task.latestStep {
                        Text(step).font(.footnote).foregroundStyle(.secondary)
                    }
                    if task.status.isTerminal, task.result == nil { Text(task.summaryLine).font(.footnote).foregroundStyle(.secondary) }
                }
            }
            .accessibilityElement(children: .combine)
        }
    }

    private func confirmation(_ task: AgentTask) -> some View {
        let again = agent.needsReconfirm(task)
        return AppCard {
            Label(again ? "Confirm again" : (task.confirmation?.kindLabel ?? "Needs your OK"), systemImage: "hand.raised.fill").font(.headline).foregroundStyle(.orange)
            if again {
                Text("Your first confirmation may not have reached the agent, so it is asking once more. Nothing was done twice.").font(.subheadline)
            }
            Text(task.confirmation?.summary ?? "The agent wants to continue.").font(.body)
            Text("Nothing is submitted, bought or sent until you confirm. The request expires after 15 minutes" + (task.deadlineAt.map { " (and no later than \($0.formatted(date: .omitted, time: .shortened)))" } ?? "") + ".")
                .font(.footnote).foregroundStyle(.secondary)
            HStack(spacing: 12) {
                Button(role: .destructive) { Task { working = true; await agent.cancel(task.id); working = false } } label: {
                    Text("Cancel").frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.bordered)
                Button { Task { working = true; await agent.confirm(task.id); working = false } } label: {
                    Text(again ? "Confirm again" : "Confirm").frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.borderedProminent)
                .accessibilityHint("Lets the agent take exactly the action described")
            }
            .disabled(working)
        }
    }

    private func resultCard(_ result: AgentResult) -> some View {
        AppCard {
            HStack {
                Label("Result", systemImage: "doc.text").font(.headline)
                Spacer()
                Button {
                    UIPasteboard.general.string = result.plainText; copied = true
                    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { copied = false }
                } label: { Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc") }
                    .frame(minHeight: 44)
            }
            MarkdownView(markdown: result.markdown)
            if !result.images.isEmpty {
                ForEach(result.images, id: \.self) { url in
                    AsyncImage(url: url) { phase in
                        switch phase {
                        case .success(let image): image.resizable().scaledToFit().clipShape(RoundedRectangle(cornerRadius: 12, style: .continuous))
                        case .failure: Label("Image couldn't load", systemImage: "photo").foregroundStyle(.secondary)
                        default: ProgressView().frame(maxWidth: .infinity, minHeight: 80)
                        }
                    }
                    .accessibilityLabel("Result image")
                }
            }
            if !result.links.isEmpty {
                Divider()
                Text("Sources").font(.subheadline.weight(.semibold))
                ForEach(result.links, id: \.self) { link in
                    Link(destination: link.url) {
                        HStack {
                            Image(systemName: "link")
                            VStack(alignment: .leading) {
                                Text(link.title?.isEmpty == false ? link.title! : link.url.host ?? link.url.absoluteString).lineLimit(2)
                                Text(link.url.host ?? "").font(.caption).foregroundStyle(.secondary)
                            }
                            Spacer()
                        }
                        .frame(minHeight: 44)
                    }
                }
            }
        }
    }

    private func steps(_ task: AgentTask) -> some View {
        AppCard {
            Text("Steps").font(.headline)
            ForEach(Array(task.steps.suffix(12).enumerated()), id: \.offset) { _, step in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: "circle.fill").font(.system(size: 6)).foregroundStyle(.tertiary).padding(.top, 7).accessibilityHidden(true)
                    Text(step.text).font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
    }
}

/// Renders markdown block by block so headings, lists and code keep their shape.
struct MarkdownView: View {
    let markdown: String
    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            ForEach(Array(MarkdownBlocks.parse(markdown).enumerated()), id: \.offset) { _, block in
                switch block {
                case .heading(let level, let text): inline(text).font(level <= 1 ? .title3.weight(.semibold) : .headline)
                case .paragraph(let text): inline(text)
                case .bullets(let items):
                    ForEach(Array(items.enumerated()), id: \.offset) { _, item in
                        HStack(alignment: .firstTextBaseline, spacing: 8) { Text("•").accessibilityHidden(true); inline(item) }
                    }
                case .numbered(let items):
                    ForEach(Array(items.enumerated()), id: \.offset) { index, item in
                        HStack(alignment: .firstTextBaseline, spacing: 8) { Text("\(index + 1).").monospacedDigit(); inline(item) }
                    }
                case .code(let text):
                    Text(text).font(.footnote.monospaced()).padding(10).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color(.tertiarySystemFill), in: RoundedRectangle(cornerRadius: 10, style: .continuous))
                case .quote(let text):
                    HStack(spacing: 10) { RoundedRectangle(cornerRadius: 2).fill(Theme.accent).frame(width: 3); inline(text).foregroundStyle(.secondary) }
                case .rule: Divider()
                }
            }
        }
        .textSelection(.enabled)
        .frame(maxWidth: .infinity, alignment: .leading)
    }
    private func inline(_ text: String) -> Text {
        if let attributed = try? AttributedString(markdown: text, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)) { return Text(attributed) }
        return Text(text)
    }
}

struct NewTaskView: View {
    @EnvironmentObject private var agent: AgentCenter
    @Environment(\.dismiss) private var dismiss
    @State private var instruction = ""
    @State private var mode: AgentMode = .research
    @State private var error: String?
    @State private var starting = false
    @FocusState private var focused: Bool

    var body: some View {
        Form {
            Section {
                TextField("What should the agent do?", text: $instruction, axis: .vertical).lineLimit(3...8).focused($focused)
            } footer: {
                Text(mode == .research ? "Read-only: the agent browses and reports back, never submits anything." : "The agent can fill in forms, but stops and asks you before anything is submitted, bought or sent.")
            }
            Section("Mode") {
                Picker("Mode", selection: $mode) {
                    ForEach(AgentMode.allCases, id: \.self) { Text($0.label).tag($0) }
                }
                .pickerStyle(.segmented)
            }
            Section {
                if agent.atActiveLimit {
                    Label("\(agent.activeCount) of \(AgentTask.maxActive) tasks running. Wait for one to finish, or cancel it, before starting another.", systemImage: "hourglass").foregroundStyle(.secondary)
                } else {
                    Text("\(agent.activeCount) of \(AgentTask.maxActive) tasks running · up to \(AgentTask.maxPerDay) per day").font(.footnote).foregroundStyle(.secondary)
                }
            }
            if let error { Section { Text(error).foregroundStyle(Theme.danger) } }
        }
        .navigationTitle("New task")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
            ToolbarItem(placement: .confirmationAction) {
                Button(starting ? "Starting…" : "Start") { start() }.disabled(starting || agent.atActiveLimit || instruction.trimmingCharacters(in: .whitespacesAndNewlines).count < 3)
            }
        }
        .onAppear { focused = true }
        .onChange(of: instruction) { if let detected = AgentIntent.detect($0) { mode = detected.mode } }
    }

    private func start() {
        starting = true; error = nil
        Task {
            do { try await agent.start(AgentIntent(mode: mode, instruction: instruction.trimmingCharacters(in: .whitespacesAndNewlines))); dismiss() }
            catch { self.error = (error as? AgentError)?.errorDescription ?? error.localizedDescription }
            starting = false
        }
    }
}
