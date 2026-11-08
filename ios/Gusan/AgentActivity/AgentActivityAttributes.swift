import Foundation
import ActivityKit

@available(iOS 16.1, *)
struct AgentActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        var phase: String
        var latestStep: String
        var stepCount: Int
        var resultPreview: String
        var updatedAt: Date

        init(phase: String, latestStep: String, stepCount: Int, resultPreview: String, updatedAt: Date) {
            self.phase = String(phase.prefix(16))
            self.latestStep = Self.clipped(latestStep, bytes: 512)
            self.stepCount = max(0, stepCount)
            self.resultPreview = Self.clipped(resultPreview, bytes: 512)
            self.updatedAt = updatedAt
        }

        private static func clipped(_ text: String, bytes: Int) -> String {
            String(decoding: text.utf8.prefix(bytes), as: UTF8.self)
        }

        var isFinished: Bool { ["done", "failed", "stopped"].contains(phase) }
        var label: String {
            switch phase {
            case "confirm": return "Needs your confirmation"
            case "login": return "Needs a login"
            case "code": return "Needs a code"
            case "done": return "Done"
            case "failed": return "Failed"
            case "stopped": return "Stopped"
            default: return "Working"
            }
        }
    }

    let sessionID: UUID
    let taskID: UUID
    let mode: String
    let title: String

    var sessionURL: URL { URL(string: "gusan://session/\(sessionID.uuidString)")! }
}
