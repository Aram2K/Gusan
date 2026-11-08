import SwiftUI
import ReplayKit

struct ScreenBroadcastSheet: View {
    @Environment(\.dismiss) private var dismiss
    @State private var trigger = 0
    var body: some View {
        VStack(spacing: 20) {
            BroadcastPicker(trigger: trigger)
                .frame(width: 1, height: 1)
                .opacity(0.02)
                .accessibilityHidden(true)
            Button { trigger += 1 } label: {
                Label("Start Broadcast", systemImage: "record.circle")
                    .font(.headline)
                    .frame(maxWidth: .infinity)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Text("Pick Gusan, tap Start Broadcast, then swipe back to your app.")
                .font(.subheadline)
                .foregroundStyle(.secondary)
                .multilineTextAlignment(.center)
            Button("Close") { dismiss() }
        }
        .padding()
        .presentationDetents([.medium])
        .task {
            try? await Task.sleep(nanoseconds: 500_000_000)
            trigger += 1
        }
    }
}

/// The system picker's own button is a small, often invisible glyph; the sheet drives it programmatically.
private struct BroadcastPicker: UIViewRepresentable {
    var trigger: Int
    func makeCoordinator() -> Coordinator { Coordinator() }
    func makeUIView(context: Context) -> RPSystemBroadcastPickerView {
        let picker = RPSystemBroadcastPickerView(frame: CGRect(x: 0, y: 0, width: 44, height: 44))
        picker.preferredExtension = (Bundle.main.bundleIdentifier ?? "com.aram2k.gusan") + ".screen"
        picker.showsMicrophoneButton = false
        return picker
    }
    func updateUIView(_ picker: RPSystemBroadcastPickerView, context: Context) {
        guard trigger > 0, trigger != context.coordinator.fired else { return }
        context.coordinator.fired = trigger
        DispatchQueue.main.async {
            picker.subviews.lazy.compactMap { $0 as? UIButton }.first?.sendActions(for: .touchUpInside)
        }
    }
    final class Coordinator { var fired = 0 }
}
