import SwiftUI
import WidgetKit

@main
struct GusanActivityBundle: WidgetBundle {
    var body: some Widget {
        RecordingActivityWidget()
        AgentActivityWidget()
        if #available(iOS 18.0, *) { GusanVoiceControl() }
    }
}

/// Control Center / Lock Screen / Action button control: start Gusan from anywhere.
@available(iOS 18.0, *)
struct GusanVoiceControl: ControlWidget {
    var body: some ControlWidgetConfiguration {
        StaticControlConfiguration(kind: "com.aram2k.gusan.voice-control") {
            ControlWidgetButton(action: StartListeningIntent()) {
                Label("Gusan", systemImage: "mic.fill")
            }
        }
        .displayName("Start Gusan")
        .description("Listens and writes what you say where your cursor is.")
    }
}
