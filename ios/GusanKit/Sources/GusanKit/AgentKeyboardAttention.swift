import Foundation

public struct AgentKeyboardAttention: Equatable {
    public let text: String
    public let url: URL
    public static func resolve(_ task: AgentTask) -> Self? {
        let text: String
        switch task.phase {
        case .needsLogin: text = "Needs your login"
        case .needsCode: text = "Needs a code"
        default: return nil
        }
        return Self(text: text, url: URL(string: "gusan://session/\(task.id.uuidString.lowercased())")!)
    }
}
