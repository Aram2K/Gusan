import SwiftUI

@main
struct GusanApp: App {
    @StateObject private var model: AppModel
    @StateObject private var voice = VoiceSession()
    @StateObject private var agent: AgentCenter
    @Environment(\.scenePhase) private var scenePhase
    init() {
        Theme.configureTabBar()
        let model = AppModel()
        let agent = AgentCenter(model: model)
        agent.configureLiveActivities()
        _model = StateObject(wrappedValue: model); _agent = StateObject(wrappedValue: agent)
        AgentCenter.registerBackgroundRefresh(agent)
    }
    @StateObject private var screen = ScreenMonitor()
    @State private var voiceSignInRequested = false
    var body: some Scene {
        WindowGroup {
            AppRootView().environmentObject(model).environmentObject(voice).environmentObject(agent).environmentObject(screen).tint(Theme.accent)
                .onAppear { voice.agentHandoff = { intent, context in try await agent.start(intent, context: context) } }
                .onChange(of: scenePhase) { agent.setActive($0 == .active) }
                #if DEBUG
                .overlay(alignment: .bottom) { AgentCardPreview() }
                #endif
                .onOpenURL { if $0.host == "voice" { voice.handleURL($0) } else { model.handleURL($0) } }
                .sheet(isPresented: $voice.presented, onDismiss: {
                    if voiceSignInRequested {
                        voiceSignInRequested = false
                        model.handleURL(URL(string: "gusan://signin")!)
                    }
                }) { VoiceReadyView(voice: voice, onSignIn: { voiceSignInRequested = true }).tint(Theme.accent) }
                .onChange(of: model.session) { if $0 == nil { voice.stop() }; agent.sessionChanged() }
        }
    }
}
