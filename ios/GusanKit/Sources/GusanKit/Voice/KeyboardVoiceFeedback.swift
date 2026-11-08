import Foundation

public enum KeyboardVoiceFeedback {
    public static func statusForDisplay(_ status: VoiceStatus?, timedOutRequestID: UUID?) -> VoiceStatus? {
        guard let status, timedOutRequestID == nil || status.requestID != timedOutRequestID else { return nil }
        return status
    }

    public static func displaysFailure(_ status: VoiceStatus?, pending: KeyboardVoiceState, now: Date = Date()) -> Bool {
        guard let status, status.phase == .failed else { return false }
        if let id = pending.requestID, status.requestID != id { return false }
        let written = status.writtenAt ?? status.heartbeat
        guard written <= now else { return false }
        return now.timeIntervalSince(written) < 120 || (pending.requestID != nil && written >= pending.startedAt)
    }
}
