import GusanKit
import SwiftUI

struct TaskRow: View {
    let task: AgentTask
    static let relative: RelativeDateTimeFormatter = { let f = RelativeDateTimeFormatter(); f.unitsStyle = .abbreviated; return f }()
    var body: some View {
        HStack(alignment: .top, spacing: 12) {
            StatusGlyph(status: task.status)
            VStack(alignment: .leading, spacing: 4) {
                Text(task.title).font(.body).foregroundStyle(.primary).lineLimit(2)
                Text(task.summaryLine).font(.subheadline).foregroundStyle(.secondary).lineLimit(2)
                HStack(spacing: 6) {
                    Text(task.mode == .do ? "Do-it task" : "Research")
                    Text("·")
                    Text(task.phase.label)
                    Text("·")
                    Text(Self.relative.localizedString(for: task.createdAt, relativeTo: Date()))
                }
                .font(.caption).foregroundStyle(.tertiary)
            }
            Spacer(minLength: 0)
            if task.needsAttention { Circle().fill(Theme.agent).frame(width: 10, height: 10).accessibilityLabel("New") }
        }
        .padding(.vertical, 4)
        .accessibilityElement(children: .combine)
        .accessibilityLabel("\(task.phase.label). \(task.mode.label). \(task.title). \(task.summaryLine)")
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
        case .awaitingConfirmation: return Theme.agent
        case .completed: return Theme.success
        case .failed, .timedOut: return Theme.danger
        case .cancelled: return .secondary
        default: return Theme.agent
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
                Label(task.phase.label, systemImage: "exclamationmark.triangle").font(.headline)
                Text(task.summaryLine).foregroundStyle(.secondary)
            }
            if agent.atActiveLimit {
                Text("You already have \(AgentTask.maxActive) tasks running; wait for one to finish before running this again.").font(.footnote).foregroundStyle(.secondary)
            }
            if agent.canRunAgain(task) {
                PrimaryButton(title: working ? "Starting…" : "Run again", tint: Theme.agent) {
                    Task { working = true; if let started = try? await agent.runAgain(task) { agent.openTaskID = started.id }; working = false }
                }
                .disabled(working || agent.atActiveLimit)
            } else {
                Text("The full instruction isn't stored on this device; start it again from Home.").font(.footnote).foregroundStyle(.secondary)
            }
        }
    }

    private func header(_ task: AgentTask) -> some View {
        AppCard {
            HStack(alignment: .top, spacing: 12) {
                StatusGlyph(status: task.status)
                VStack(alignment: .leading, spacing: 4) {
                    Text(task.title).font(.headline)
                    Text(task.phase.label).font(.subheadline).foregroundStyle(.secondary)
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
            Label(again ? "Confirm again" : (task.confirmation?.kindLabel ?? "Needs your OK"), systemImage: "hand.raised.fill").font(.headline).foregroundStyle(Theme.agent)
            if again {
                Text("Your first confirmation may not have reached the agent, so it is asking once more. Nothing was done twice.").font(.subheadline)
            }
            Text(task.confirmation?.summary ?? "The agent wants to continue.").font(.body)
            Text("Nothing is submitted, bought or sent until you confirm. The request expires after 15 minutes" + (task.deadlineAt.map { " (and no later than \($0.formatted(date: .omitted, time: .shortened)))" } ?? "") + ".")
                .font(.footnote).foregroundStyle(.secondary)
            HStack(spacing: 12) {
                Button(role: .destructive) { Task { working = true; await agent.cancel(task.id); working = false } } label: {
                    Text("Cancel task").frame(maxWidth: .infinity, minHeight: 44)
                }
                .buttonStyle(.bordered).tint(Theme.danger)
                .accessibilityHint("Stops the task; nothing is submitted")
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
                    HStack(spacing: 10) { RoundedRectangle(cornerRadius: 2).fill(Theme.agent).frame(width: 3); inline(text).foregroundStyle(.secondary) }
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
