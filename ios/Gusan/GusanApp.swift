import SwiftUI

@main
struct GusanApp: App {
    @StateObject private var model = AppModel()
    @StateObject private var voice = VoiceSession()
    @StateObject private var screen = ScreenMonitor()
    @State private var voiceSignInRequested = false
    var body: some Scene {
        WindowGroup {
            AppRootView().environmentObject(model).environmentObject(voice).environmentObject(screen).tint(Theme.accent)
                .onOpenURL { if $0.host == "voice" { voice.handleURL($0) } else { model.handleURL($0) } }
                .sheet(isPresented: $voice.presented, onDismiss: {
                    if voiceSignInRequested {
                        voiceSignInRequested = false
                        model.handleURL(URL(string: "gusan://signin")!)
                    }
                }) { VoiceReadyView(voice: voice, onSignIn: { voiceSignInRequested = true }).tint(Theme.accent) }
                .onChange(of: model.session) { if $0 == nil { voice.stop() } }
        }
    }
}
