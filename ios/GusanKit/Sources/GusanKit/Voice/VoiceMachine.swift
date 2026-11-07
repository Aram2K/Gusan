import Foundation

public enum VoicePlan: Equatable {
    case insert(String), smart(SmartActionMode, String), instruction(instruction: String, text: String)
    public static func plan(transcript: String, context: VoiceContext?, action: VoiceAction) -> VoicePlan {
        if let selected = context?.selected, !selected.isEmpty { return .instruction(instruction: transcript, text: selected) }
        if case .smart(let mode) = action { return .smart(mode, transcript) }
        return .insert(transcript)
    }
}

public struct VoiceMachine {
    public enum Event {
        case sessionStarted(Date), command(VoiceCommand, Date), transcribed(UUID, String), finished(UUID, String, Bool), failed(UUID, String), acknowledged(UUID), tick(Date), sessionStopped
    }
    public enum Effect: Equatable {
        case beginCapture, finishCapture(UUID), discardCapture, cancelWork, runAction(UUID, String, VoiceContext?), deliver(String), stopSession, publish
    }
    public private(set) var phase: VoicePhase = .off
    public private(set) var requestID: UUID?
    public private(set) var context: VoiceContext?
    public private(set) var lastActivity = Date.distantPast
    public private(set) var recordingStarted: Date?
    public private(set) var result: String?
    public private(set) var message: String?
    public private(set) var replacesSelection = false
    public var idleTimeout: TimeInterval
    public var maxRecording: TimeInterval = 300
    public var action: VoiceAction
    public init(idleTimeout: TimeInterval = 300, action: VoiceAction = .transcribeOnly) { self.idleTimeout = idleTimeout; self.action = action }
    public mutating func handle(_ event: Event) -> [Effect] {
        var effects: [Effect] = []
        switch event {
        case .sessionStarted(let now):
            guard phase == .off else { return [] }
            phase = .ready; lastActivity = now; requestID = nil; result = nil; message = nil
        case .command(let command, let now):
            guard now.timeIntervalSince(command.sentAt) <= 15 else { return [] }
            switch command.kind {
            case .endSession: phase = .off; effects = [.stopSession]
            case .start:
                guard [.ready, .done, .failed].contains(phase) else { return [] }
                phase = .recording; requestID = command.requestID; context = command.context; recordingStarted = now; lastActivity = now; result = nil; message = nil; replacesSelection = false
                effects = [.beginCapture]
            case .stop:
                guard phase == .recording, requestID == command.requestID else { return [] }
                phase = .transcribing; lastActivity = now; effects = [.finishCapture(command.requestID)]
            case .cancel:
                guard requestID == command.requestID else { return [] }
                if phase == .recording { effects = [.discardCapture] }
                else if phase == .transcribing || phase == .thinking { effects = [.cancelWork] }
                else { return [] }
                phase = .ready; requestID = nil; context = nil; result = nil; message = nil; lastActivity = now
            }
        case .transcribed(let id, let text):
            guard requestID == id, phase == .transcribing else { return [] }
            if case .insert(let result) = VoicePlan.plan(transcript: text, context: context, action: action) {
                phase = .done; self.result = result; replacesSelection = false; effects = [.deliver(result)]
            } else { phase = .thinking; effects = [.runAction(id, text, context)] }
        case .finished(let id, let result, let replaces):
            guard requestID == id, phase == .thinking else { return [] }
            phase = .done; self.result = result; replacesSelection = replaces; effects = [.deliver(result)]
        case .failed(let id, let message):
            guard requestID == id, [.recording, .transcribing, .thinking].contains(phase) else { return [] }
            phase = .failed; self.message = message; effects = [.discardCapture, .cancelWork]
        case .tick(let now):
            if [.recording, .transcribing, .thinking].contains(phase) { lastActivity = now }
            if phase == .recording, let start = recordingStarted, now.timeIntervalSince(start) >= maxRecording, let id = requestID {
                phase = .transcribing; lastActivity = now; effects = [.finishCapture(id)]
            } else if [.ready, .done, .failed].contains(phase), now.timeIntervalSince(lastActivity) >= idleTimeout { phase = .off; effects = [.stopSession] }
            else { return [] }
        case .acknowledged(let id):
            guard requestID == id, phase == .done || phase == .failed else { return [] }
            phase = .ready; requestID = nil; context = nil; result = nil; message = nil; replacesSelection = false
        case .sessionStopped:
            phase = .off; requestID = nil; recordingStarted = nil; context = nil; result = nil; message = nil; effects = [.discardCapture, .cancelWork]
        }
        return effects + [.publish]
    }
}

public enum KeyboardVoice {
    public enum TapResult: Equatable { case error(VoiceError), openApp, start, stop, cancel }
    public static func tap(status: VoiceStatus?, now: Date = Date(), hasFullAccess: Bool, shared: Bool, signedIn: Bool, fallbackOff: Bool = false) -> TapResult {
        guard hasFullAccess else { return .error(.fullAccessRequired) }
        guard shared else { return .error(fallbackOff ? .fallbackOff : .notShared) }
        guard signedIn else { return .error(.notSignedIn) }
        guard let status, status.isAlive(now: now) else { return .openApp }
        switch status.phase {
        case .ready, .done, .failed: return .start
        case .recording: return .stop
        case .transcribing, .thinking: return .cancel
        case .off: return .openApp
        }
    }
}
