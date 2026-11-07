import SwiftUI

@main
struct GusanApp: App {
    @StateObject private var model = AppModel()
    @StateObject private var voice = VoiceSession()
    var body: some Scene {
        WindowGroup {
            AppRootView().environmentObject(model).environmentObject(voice).tint(Theme.accent)
                .onOpenURL { if $0.host == "voice" { voice.handleURL($0) } else { model.handleURL($0) } }
                .sheet(isPresented: $voice.presented) { VoiceReadyView(voice: voice) }
        }
    }
}
