import Foundation

public struct KeyboardVoiceState: Codable, Equatable {
    public var requestID: UUID?
    public var context: VoiceContext?
    public var insertedID: UUID?
    public var startedAt: Date
    public var action: VoiceAction?
    public init(requestID: UUID? = nil, context: VoiceContext? = nil, insertedID: UUID? = nil, startedAt: Date = Date(), action: VoiceAction? = nil) {
        self.requestID = requestID; self.context = context; self.insertedID = insertedID; self.startedAt = startedAt
        self.action = action
    }
    public func isStale(status: VoiceStatus, now: Date = Date()) -> Bool {
        now.timeIntervalSince(status.heartbeat) >= 600 || now.timeIntervalSince(startedAt) >= 600
    }
    public func canInsert(context current: VoiceContext) -> Bool {
        guard let context else { return false }
        if (context.documentID ?? "").isEmpty && (current.documentID ?? "").isEmpty { return context == current }
        guard let documentID = context.documentID, !documentID.isEmpty else { return false }
        return documentID == current.documentID && context == current
    }
}

public enum VoiceInsertionDestination: Equatable {
    case planned, cursor, clipboard
    public static func resolve(snapshot: VoiceContext, current: VoiceContext) -> Self {
        if (snapshot.documentID ?? "").isEmpty && (current.documentID ?? "").isEmpty {
            return snapshot == current ? .planned : .clipboard
        }
        guard let document = snapshot.documentID, !document.isEmpty, document == current.documentID else { return .clipboard }
        if snapshot == current { return .planned }
        return current.selected.isEmpty ? .cursor : .clipboard
    }
}
