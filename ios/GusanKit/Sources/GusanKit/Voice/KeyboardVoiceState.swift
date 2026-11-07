import Foundation

public struct KeyboardVoiceState: Codable, Equatable {
    public var requestID: UUID?
    public var context: VoiceContext?
    public var insertedID: UUID?
    public var startedAt: Date
    public init(requestID: UUID? = nil, context: VoiceContext? = nil, insertedID: UUID? = nil, startedAt: Date = Date()) {
        self.requestID = requestID; self.context = context; self.insertedID = insertedID; self.startedAt = startedAt
    }
    public func isStale(status: VoiceStatus, now: Date = Date()) -> Bool {
        now.timeIntervalSince(status.heartbeat) >= 600 || now.timeIntervalSince(startedAt) >= 600
    }
    public func canInsert(context current: VoiceContext) -> Bool {
        guard let context, let documentID = context.documentID, !documentID.isEmpty else { return false }
        return documentID == current.documentID && context == current
    }
}
