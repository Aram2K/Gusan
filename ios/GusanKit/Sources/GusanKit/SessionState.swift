import Foundation

public struct SessionState: Equatable {
    public let signedIn: Bool
    public let shareable: Bool
    public let actionError: VoiceError?
    public let voiceError: VoiceError?
    public var canRunActions: Bool { actionError == nil }
    public var voiceReady: Bool { voiceError == nil }

    public init(signedIn: Bool, fullAccess: Bool, shareable: Bool, voiceError: VoiceError? = nil) {
        self.signedIn = signedIn
        self.shareable = shareable
        actionError = !fullAccess ? .fullAccessRequired : (!signedIn ? .notSignedIn : (!shareable ? .notShared : nil))
        self.voiceError = actionError ?? voiceError
    }
}

public extension SessionStore {
    func sessionState(fullAccess: Bool) -> SessionState {
        let session = read()
        var signedIn = session != nil
        #if DEBUG
        if session?.accessToken == "gusan-debug-no-network" { signedIn = false }
        #endif
        let channel = makeVoiceChannel()
        return SessionState(signedIn: signedIn, fullAccess: fullAccess, shareable: diagnostics().isShared,
                            voiceError: channel.available ? nil : channel.unavailableError)
    }
}
