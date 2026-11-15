import SwiftUI

struct MakeGusanKeyboardSection: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    var compact = false

    var body: some View {
        Section {
            statusRow(
                title: model.keyboardAdded ? "Keyboard added" : "Keyboard not yet confirmed",
                detail: model.keyboardCheck,
                confirmed: model.keyboardAdded
            )
            statusRow(
                title: model.fullAccess == true ? "Full Access on at last check" : "Full Access not confirmed",
                detail: model.fullAccess == true ? "On at the last check" : "Open the Gusan keyboard to check",
                confirmed: model.fullAccess == true
            )
            VStack(alignment: .leading, spacing: 8) {
                Text("iOS shows the first keyboard in your list where supported.")
                Text("1. Settings › General › Keyboard › Keyboards › Edit, then drag Gusan to the top")
                Text("2. Optional: remove other keyboards")
            }
            .font(compact ? .footnote : .body)
            Button("Open Settings") { Theme.openSettings() }
        } header: {
            Text("Make Gusan your keyboard")
        } footer: {
            Text("Password and PIN fields always use Apple's keyboard.").foregroundStyle(Theme.secondaryText)
        }
        .onAppear { model.refreshDiagnostics() }
        .onChange(of: scenePhase) { if $0 == .active { model.refreshDiagnostics() } }
    }

    private func statusRow(title: String, detail: String, confirmed: Bool) -> some View {
        Label {
            VStack(alignment: .leading, spacing: 4) {
                Text(title).font(.subheadline)
                if !compact { Text(detail).font(.footnote).foregroundStyle(Theme.secondaryText) }
            }
        } icon: {
            Image(systemName: confirmed ? "checkmark.circle.fill" : "exclamationmark.triangle")
                .accessibilityHidden(true)
        }
        .accessibilityElement(children: .ignore)
        .accessibilityLabel("\(title). \(detail)")
    }
}
