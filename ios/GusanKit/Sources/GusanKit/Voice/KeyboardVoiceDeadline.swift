import Foundation

public struct KeyboardVoiceDeadline {
    public enum Expiry: Equatable { case acknowledgement, processing }
    private var acknowledged = false
    private var readyAliveUntil: Date?
    private var processingAt: Date?
    public init() {}

    public mutating func expiry(pending: KeyboardVoiceState, status: VoiceStatus?, now: Date = Date()) -> Expiry? {
        guard let id = pending.requestID, pending.insertedID != id else { return nil }
        if let status, status.requestID == id {
            switch status.phase {
            case .done, .failed: return nil
            case .ready:
                if status.heartbeat <= now {
                    readyAliveUntil = max(pending.startedAt, status.heartbeat.addingTimeInterval(3))
                }
            case .recording: acknowledged = true
            case .transcribing, .thinking:
                acknowledged = true
                if processingAt == nil { processingAt = status.writtenAt ?? now }
            case .off: break
            }
        }
        if let processingAt, now.timeIntervalSince(processingAt) >= 120 { return .processing }
        if !acknowledged && now.timeIntervalSince(readyAliveUntil ?? pending.startedAt) >= 10 { return .acknowledgement }
        return nil
    }
}
