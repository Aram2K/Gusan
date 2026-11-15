import ActivityKit
import AppIntents
import SwiftUI
import WidgetKit

private let gusanBlue = Color(red: 10 / 255, green: 132 / 255, blue: 1)

/// The Dynamic Island "glow": a tinted ring around the mic while Gusan listens.
/// ActivityKit has no continuous animation, so the ring is static and the bars move on each update.
struct RecordingActivityWidget: Widget {
    var body: some WidgetConfiguration {
        ActivityConfiguration(for: RecordingActivityAttributes.self) { context in
            RecordingLockScreenView(state: context.state)
                .activityBackgroundTint(Color.black.opacity(0.85))
                .activitySystemActionForegroundColor(.white)
                .widgetURL(URL(string: "gusan://voice-activity"))
        } dynamicIsland: { context in
            DynamicIsland {
                DynamicIslandExpandedRegion(.leading) {
                    GlowRing(phase: context.state.phase, size: 40).padding(.leading, 4)
                }
                DynamicIslandExpandedRegion(.trailing) {
                    ActivityControls(state: context.state).padding(.trailing, 4)
                }
                DynamicIslandExpandedRegion(.center) {
                    Text(title(context.state)).font(.headline).foregroundStyle(.white).lineLimit(1)
                }
                DynamicIslandExpandedRegion(.bottom) {
                    if context.state.phase == .listening {
                        Bars(levels: context.state.levels, count: 24).frame(height: 28).padding(.horizontal, 8)
                    } else if let text = context.state.text, !text.isEmpty {
                        Text(text).font(.subheadline).foregroundStyle(.white.opacity(0.85)).lineLimit(2).truncationMode(.head)
                    }
                }
            } compactLeading: {
                GlowRing(phase: context.state.phase, size: 22)
            } compactTrailing: {
                switch context.state.phase {
                case .listening: Bars(levels: context.state.levels, count: 7).frame(width: 28, height: 16)
                case .working: ProgressView().tint(gusanBlue)
                case .done: Image(systemName: context.state.copied ? "doc.on.clipboard" : "checkmark").foregroundStyle(.green)
                case .failed: Image(systemName: "exclamationmark.triangle.fill").foregroundStyle(.orange)
                case .ready: Image(systemName: "mic").foregroundStyle(gusanBlue)
                }
            } minimal: {
                GlowRing(phase: context.state.phase, size: 20)
            }
            .keylineTint(gusanBlue)
            .widgetURL(URL(string: "gusan://voice-activity"))
        }
    }
}

private func title(_ state: RecordingActivityAttributes.ContentState) -> String {
    switch state.phase {
    case .ready: return "Gusan is ready"
    case .listening: return "Listening…"
    case .working: return "Working…"
    case .done: return state.copied ? "Copied" : "Inserted"
    case .failed: return state.text ?? "Didn't work"
    }
}

private struct GlowRing: View {
    let phase: RecordingActivityAttributes.ContentState.Phase
    let size: CGFloat
    var body: some View {
        let active = phase == .listening || phase == .working
        ZStack {
            Circle().stroke(gusanBlue.opacity(active ? 1 : 0.5), lineWidth: max(2, size / 11))
                .shadow(color: gusanBlue.opacity(active ? 0.9 : 0), radius: size / 5)
            Image(systemName: phase == .done ? "checkmark" : "mic.fill")
                .font(.system(size: size * 0.45, weight: .semibold))
                .foregroundStyle(phase == .listening ? .white : gusanBlue)
        }
        .frame(width: size, height: size)
        .accessibilityLabel(phase == .listening ? "Gusan is listening" : "Gusan")
    }
}

private struct Bars: View {
    let levels: [Double]
    let count: Int
    var body: some View {
        GeometryReader { geometry in
            let values = Array((Array(repeating: 0.08, count: count) + levels).suffix(count))
            let width = geometry.size.width / CGFloat(count * 2)
            HStack(alignment: .center, spacing: width) {
                ForEach(values.indices, id: \.self) { index in
                    Capsule().fill(gusanBlue)
                        .frame(width: width, height: max(2, geometry.size.height * CGFloat(min(1, max(0.08, values[index])))))
                }
            }
            .frame(width: geometry.size.width, height: geometry.size.height)
        }
        .accessibilityHidden(true)
    }
}

/// X and mic while listening (same icons as the keyboard strip); Stop ends the ready window.
private struct ActivityControls: View {
    let state: RecordingActivityAttributes.ContentState
    var body: some View {
        HStack(spacing: 12) {
            switch state.phase {
            case .listening:
                Button(intent: CancelListeningIntent()) { Image(systemName: "xmark").frame(width: 32, height: 32) }
                    .buttonStyle(.plain).foregroundStyle(.white).background(Circle().fill(.white.opacity(0.2)))
                    .accessibilityLabel("Cancel")
                Button(intent: StopListeningIntent()) { Image(systemName: "mic.fill").frame(width: 32, height: 32) }
                    .buttonStyle(.plain).foregroundStyle(.white).background(Circle().fill(gusanBlue))
                    .accessibilityLabel("Finish and send")
            case .working:
                Button(intent: CancelListeningIntent()) { Image(systemName: "xmark").frame(width: 32, height: 32) }
                    .buttonStyle(.plain).foregroundStyle(.white).background(Circle().fill(.white.opacity(0.2)))
                    .accessibilityLabel("Cancel")
            case .ready, .done, .failed:
                Button(intent: StartListeningIntent()) { Image(systemName: "mic.fill").frame(width: 32, height: 32) }
                    .buttonStyle(.plain).foregroundStyle(.white).background(Circle().fill(gusanBlue))
                    .accessibilityLabel("Start listening")
                Button(intent: EndReadyIntent()) { Image(systemName: "stop.fill").frame(width: 32, height: 32) }
                    .buttonStyle(.plain).foregroundStyle(.white).background(Circle().fill(.white.opacity(0.2)))
                    .accessibilityLabel("Stop Gusan")
            }
        }
    }
}

private struct RecordingLockScreenView: View {
    let state: RecordingActivityAttributes.ContentState
    var body: some View {
        HStack(spacing: 12) {
            GlowRing(phase: state.phase, size: 36)
            VStack(alignment: .leading, spacing: 4) {
                Text(title(state)).font(.headline).foregroundStyle(.white).lineLimit(1)
                if state.phase == .listening {
                    Bars(levels: state.levels, count: 24).frame(height: 20)
                } else if let text = state.text, !text.isEmpty {
                    Text(text).font(.subheadline).foregroundStyle(.white.opacity(0.8)).lineLimit(2).truncationMode(.head)
                } else if state.phase == .ready {
                    Text("Tap the mic in the Gusan keyboard, or here.").font(.subheadline).foregroundStyle(.white.opacity(0.8))
                }
            }
            Spacer(minLength: 0)
            ActivityControls(state: state)
        }
        .padding(14)
    }
}
