import SwiftUI
import ReplayKit

struct ScreenBroadcastSheet: View {
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        VStack(spacing: 24) {
            BroadcastPicker().frame(width: 60, height: 60)
            Text("Tap Start Broadcast, then swipe back to your app")
                .multilineTextAlignment(.center)
            Button("Close") { dismiss() }
        }
        .padding()
        .presentationDetents([.medium])
    }
}

private struct BroadcastPicker: UIViewRepresentable {
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: .zero)
        picker.preferredExtension = (Bundle.main.bundleIdentifier ?? "com.aram2k.gusan") + ".screen"
        picker.showsMicrophoneButton = false
        return picker
    }
    func updateUIView(_ uiView: RPSystemBroadcastPickerView, context: Context) {}
}
