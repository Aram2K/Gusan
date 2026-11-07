import SwiftUI

struct AppRootView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    var body: some View {
        TabView(selection: $model.selectedTab) {
            NavigationStack { HomeView() }.tabItem { Label("Home", systemImage: "house") }.tag(AppTab.home)
            NavigationStack { KeyboardSettingsView() }.tabItem { Label("Keyboard", systemImage: "keyboard") }.tag(AppTab.keyboard)
            NavigationStack { AccountView() }.tabItem { Label("Account", systemImage: "person.crop.circle") }.tag(AppTab.account)
        }
        .fullScreenCover(isPresented: $model.showingOnboarding, onDismiss: {
            if model.pendingSignIn { model.pendingSignIn = false; model.showingSignIn = true }
        }) { OnboardingView() }
        .sheet(isPresented: $model.showingSignIn, onDismiss: {
            model.cancelGoogle()
            if model.pendingOnboarding { model.pendingOnboarding = false; model.showingOnboarding = true }
        }) { SignInSheet() }
        .task { await model.update() }
        .onChange(of: scenePhase) { phase in if phase == .active { Task { await model.update() } } }
    }
}
