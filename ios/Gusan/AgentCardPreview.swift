#if DEBUG
import GusanKit
import SwiftUI

/// Simulator-only: `--gusan-card-preview progress|confirm|result|error` renders the keyboard's
/// agent card at keyboard height so it can be screenshotted without enabling the extension.
struct AgentCardPreview: View {
    static var requested: String? {
        let args = ProcessInfo.processInfo.arguments
        guard let index = args.firstIndex(of: "--gusan-card-preview"), args.indices.contains(index + 1) else { return nil }
        return args[index + 1]
    }

    var body: some View {
        if let state = Self.requested {
            CardHost(state: state).frame(height: AgentCardView.keyboardHeight).ignoresSafeArea(edges: .bottom)
                .accessibilityLabel("Agent card preview")
        }
    }

    struct CardHost: UIViewRepresentable {
        let state: String
        func makeUIView(context: Context) -> AgentCardView {
            let card = AgentCardView()
            card.apply(KeyboardTheme(appearance: nil, traits: UITraitCollection.current))
            let demo = StubAgentService.demoTasks()
            switch state {
            case "confirm": card.render(.task(demo[1]))
            case "proposal": card.render(.proposal(AgentIntent(mode: .do, instruction: "Book a table for two at Osteria Francescana on Friday at 19:30"), sendsContext: false))
            case "result": card.render(.task(demo[2]))
            case "failed": card.render(.task(demo[3]))
            case "error": card.canRetry = true; card.render(.failed(message: AgentError.tooManyActive.errorDescription ?? "", retryable: false))
            case "expired": card.canRetry = true; card.render(.task(AgentTask(mode: .do, status: .cancelled, title: demo[1].title, errorCode: "confirmation_expired", createdAt: Date())))
            case "reconfirm": card.reconfirm = true; card.render(.task(demo[1]))
            default:
                var running = demo[0]; running.steps = [AgentStep(text: "Comparing prices on three airline sites")]
                card.render(.task(running))
            }
            return card
        }
        func updateUIView(_ uiView: AgentCardView, context: Context) {}
    }
}
#endif
