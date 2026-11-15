import ActivityKit
import SwiftUI
import WidgetKit

@available(iOS 16.1, *)
struct AgentActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: AgentActivityAttributes.self) { context in
            AgentActivityCard(attributes: context.attributes, state: context.state)
                .activityBackgroundTint(.black)
                .activitySystemActionForegroundColor(.white)
                .widgetURL(context.attributes.sessionURL)
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    Label("Gusan · Agent", systemImage: "sparkles").font(.caption).foregroundStyle(.white)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    AgentActivityRing(state: context.state).frame(width: 24, height: 24)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    AgentActivityCard(attributes: context.attributes, state: context.state, expanded: true)
                }
            } compactLeading: {
                Image(systemName: "sparkles").foregroundStyle(AgentActivityStyle.indigo)
            } compactTrailing: {
                AgentActivityRing(state: context.state).frame(width: 20, height: 20)
            } minimal: {
                AgentActivityRing(state: context.state).frame(width: 20, height: 20)
            }
            .widgetURL(context.attributes.sessionURL)
            .keylineTint(AgentActivityStyle.indigo)
        }
    }
}

private enum AgentActivityStyle {
    static let indigo = Color(red: 94 / 255, green: 92 / 255, blue: 230 / 255)
}

@available(iOS 16.1, *)
private struct AgentActivityCard: View {
    let attributes: AgentActivityAttributes
    let state: AgentActivityAttributes.ContentState
    var expanded = false

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            if !expanded {
                HStack {
                    Label("Gusan · Agent", systemImage: "sparkles")
                    Spacer()
                    Text(state.label)
                }.font(.caption).foregroundStyle(.white.opacity(0.75))
            }
            Text(attributes.title).font(.headline).lineLimit(2).privacySensitive()
            if state.phase == "done" {
                Text("Result ready · unlock to view").font(.subheadline).foregroundStyle(.white.opacity(0.75))
                if expanded && !state.resultPreview.isEmpty {
                    Text(state.resultPreview).font(.subheadline).lineLimit(3).privacySensitive()
                }
            } else if state.phase == "working" {
                Text(state.latestStep.isEmpty ? "Working…" : state.latestStep).font(.subheadline).lineLimit(2).privacySensitive()
                ProgressView(value: Double(state.stepCount), total: Double(max(state.stepCount + 1, 5)))
                    .tint(AgentActivityStyle.indigo)
                Text("\(state.stepCount) steps").font(.caption).foregroundStyle(.white.opacity(0.65))
            } else {
                Text(state.label).font(.subheadline).foregroundStyle(.white.opacity(0.8))
            }
            HStack(spacing: 12) {
                if state.phase == "done" {
                    if #available(iOS 17.0, *) {
                        Button(intent: CopyAgentResultIntent(taskID: attributes.taskID.uuidString)) {
                            Label("Copy", systemImage: "doc.on.doc")
                        }.buttonStyle(.borderedProminent).tint(AgentActivityStyle.indigo)
                    }
                }
                Link(destination: attributes.sessionURL) { Label("Open", systemImage: "arrow.up.forward.app") }
                    .buttonStyle(.bordered)
                Spacer(minLength: 0)
            }.font(.subheadline)
        }.padding(expanded ? 4 : 16).foregroundStyle(.white)
    }
}

@available(iOS 16.1, *)
private struct AgentActivityRing: View {
    let state: AgentActivityAttributes.ContentState
    var body: some View {
        ZStack {
            Circle().stroke(.white.opacity(0.25), lineWidth: 2)
            if state.isFinished {
                Image(systemName: state.phase == "done" ? "checkmark" : "xmark")
                    .font(.caption2.bold()).foregroundStyle(.white)
            } else if state.phase != "working" {
                Image(systemName: "exclamationmark").font(.caption2.bold()).foregroundStyle(AgentActivityStyle.indigo)
            } else {
                Circle().trim(from: 0, to: CGFloat(state.stepCount + 1) / CGFloat(max(state.stepCount + 2, 5)))
                    .stroke(AgentActivityStyle.indigo, style: StrokeStyle(lineWidth: 2, lineCap: .round))
                    .rotationEffect(.degrees(-90))
            }
        }.accessibilityLabel(state.label)
    }
}
