import SwiftUI

struct HowToUseView: View {
    private let steps = [
        ("1. Select text, or just type", "selection.pin.in.out", "Select the text you want to change. With nothing selected, Gusan uses the text around your cursor."),
        ("2. Tap the Gusan button", "keyboard", "Look for the round Gusan logo above the keys. Tap it to open actions; hold it to run your default action."),
        ("3. Pick an action, or ask", "sparkles", "Choose Rephrase, Fix grammar, Email and more. Or tap Ask Gusan… and type an instruction, such as ‘make it more formal’ or ‘translate to Armenian’. Tap Run."),
        ("4. Your words, improved", "arrow.uturn.backward", "The result replaces your text. Undo stays in the Gusan bar while the replacement can still be verified, even after typing after it."),
        ("5. Speak with Gusan", "mic.fill", "Tap Gusan's mic above the keys, speak, then tap Stop. Smart voice inserts dictation or follows your instruction about the text. Select text to edit just that selection. Undo restores the original. Gusan opens once to activate the microphone — follow the on-screen guidance. Later taps record in place while the session is ready. Apple's mic below the keyboard is system dictation.")
    ]
    var body: some View {
        ScreenContent {
            ForEach(steps, id: \.0) { step in
                AppCard {
                    if step.1 == "keyboard" { BrandMark() }
                    else { Image(systemName: step.1).font(.largeTitle).foregroundStyle(Theme.accent).accessibilityHidden(true) }
                    Text(step.0).font(.title2).bold()
                    Text(step.2).font(.body).foregroundStyle(.secondary)
                    if step.1 == "arrow.uturn.backward" { Label("Replaced · Undo", systemImage: "checkmark.circle").font(.headline).padding(12).background(Theme.surface, in: RoundedRectangle(cornerRadius: 14)) }
                }
            }
            AppCard {
                Text("Need a hand?").font(.title2).bold()
                Text("‘Sign in to use Gusan’? Open Gusan and sign in. If you already have, open the keyboard again to reconnect.").font(.body)
                Text("AI actions need Full Access. Go to Settings › General › Keyboard › Keyboards › Gusan › Allow Full Access.").font(.body)
                Text("Use the globe key to switch keyboards. iOS uses Apple's keyboard in secure fields. In a long document, select the text first so Gusan gets the right context.").font(.body)
                Button("Open Settings") { Theme.openSettings() }.frame(minHeight: 44)
            }
        }.navigationTitle("How to use").navigationBarTitleDisplayMode(.inline)
    }
}
