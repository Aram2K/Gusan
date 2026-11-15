import SwiftUI

struct AppRootView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var agent: AgentCenter
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        TabView(selection: $model.selectedTab) {
            NavigationStack { HomeView() }.tabItem { Label("Home", systemImage: "house") }.tag(AppTab.home)
            NavigationStack { SessionsView() }
                .tabItem { Label("Sessions", systemImage: "list.bullet.rectangle") }
                .badge(agent.unseenCount)
                .tag(AppTab.sessions)
            NavigationStack { VaultView() }.tabItem { Label("Vault", systemImage: "lock") }.tag(AppTab.vault)
            NavigationStack { SettingsView() }.tabItem { Label("Settings", systemImage: "gearshape") }.tag(AppTab.settings)
        }
        .tint(Theme.accent)
        .sheet(isPresented: $model.showingSignIn, onDismiss: {
            model.cancelGoogle()
        }) { SignInSheet() }
        .task { await model.update() }
        .onChange(of: scenePhase) { phase in if phase == .active { Task { await model.update() } } }
    }
}
