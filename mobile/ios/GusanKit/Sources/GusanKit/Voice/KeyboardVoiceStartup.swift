import Foundation

public enum KeyboardVoiceStartup {
    public static func validatedStart(_ command: VoiceCommand?, within window: TimeInterval, now: Date = Date()) -> VoiceCommand? {
        guard let command, command.kind == .start,
              (0...window).contains(now.timeIntervalSince(command.sentAt)) else { return nil }
        return command
    }
    public static func afterPermission(_ command: VoiceCommand, now: Date = Date()) -> VoiceCommand {
        var command = command
        command.sentAt = now
        return command
    }
}
