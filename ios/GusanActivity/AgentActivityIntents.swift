import AppIntents
import Foundation
import UIKit

@available(iOS 17.0, *)
enum AgentResultIntentBridge {
    static var handler: (@MainActor (UUID) async throws -> String)?

    @MainActor static func result(for id: UUID) async throws -> String {
        guard let handler else { throw AgentResultIntentError.unavailable }
        return try await handler(id)
    }
}

@available(iOS 17.0, *)
enum AgentResultIntentError: Error, CustomLocalizedStringResourceConvertible {
    case unavailable
    var localizedStringResource: LocalizedStringResource { "Open Gusan to view this result." }
}

@available(iOS 17.0, *)
struct CopyAgentResultIntent: LiveActivityIntent {
    static var title: LocalizedStringResource = "Copy Gusan result"
    static var openAppWhenRun = false
    static var isDiscoverable = false
    static var authenticationPolicy: IntentAuthenticationPolicy = .requiresAuthentication

    @Parameter(title: "Task ID") var taskID: String

    init() {}
    init(taskID: String) { self.taskID = taskID }

    @MainActor func perform() async throws -> some IntentResult {
        guard let id = UUID(uuidString: taskID) else { throw AgentResultIntentError.unavailable }
        UIPasteboard.general.string = try await AgentResultIntentBridge.result(for: id)
        return .result()
    }
}
