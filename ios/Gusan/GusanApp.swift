import SwiftUI

@main
struct GusanApp: App {
    @StateObject private var model = AppModel()
    var body: some Scene {
        WindowGroup {
            AppRootView().environmentObject(model).tint(Theme.accent)
                .onOpenURL { model.handleURL($0) }
        }
    }
}
