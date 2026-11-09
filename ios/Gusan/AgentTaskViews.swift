import GusanKit
import SwiftUI

struct TaskRow: View {
    let task: AgentTask
    var attention: Bool? = nil
    var title: String? = nil

    var body: some View {
        HStack(spacing: 12) {
            Image(systemName: symbol).foregroundStyle(Theme.agent).frame(width: 24)
            VStack(alignment: .leading, spacing: 5) {
                Text(title ?? task.title).foregroundStyle(.primary).lineLimit(1)
                HStack {
                    Text(phaseLabel)
                    Text("·")
                    if task.elapsedIsRunning {
                        Text(task.createdAt, style: .timer).monospacedDigit()
                    } else {
                        Text(Duration.seconds(task.elapsed()).formatted(.time(pattern: .minuteSecond))).monospacedDigit()
                    }
                }.font(.caption).foregroundStyle(Theme.secondaryText)
            }
            Spacer(minLength: 0)
            if attention ?? task.needsAttention {
                Circle().fill(Theme.agent).frame(width: 8, height: 8).accessibilityLabel("Needs attention")
            }
            Image(systemName: "chevron.right").font(.caption).foregroundStyle(.tertiary)
        }.frame(maxWidth: .infinity, minHeight: 44).padding(.vertical, 5)
            .contentShape(Rectangle()).accessibilityElement(children: .combine)
    }

    private var phaseLabel: String {
        switch task.phase {
        case .queued, .working: return "Working"
        case .needsConfirmation: return "Needs you"
        case .needsLogin: return "Needs a login"
        case .needsCode: return "Needs a code"
        case .completed: return "Done"
        case .failed, .timedOut: return "Failed"
        case .cancelled, .stopped: return "Stopped"
        }
    }
    private var symbol: String {
        switch task.phase {
        case .queued, .working: return "hourglass"
        case .needsConfirmation: return "hand.raised"
        case .needsLogin, .needsCode: return "key"
        case .completed: return "checkmark.circle"
        case .failed, .timedOut: return "exclamationmark.triangle"
        case .cancelled, .stopped: return "stop.circle"
        }
    }
}

struct CopyTextButton: View {
    let text: String
    @State private var copied = false
    var body: some View {
        Button {
            UIPasteboard.general.string = text
            copied = true
        } label: {
            Label(copied ? "Copied" : "Copy", systemImage: copied ? "checkmark" : "doc.on.doc")
        }
        .task(id: copied) {
            guard copied else { return }
            do { try await Task.sleep(nanoseconds: 2_000_000_000); copied = false } catch {}
        }
    }
}
