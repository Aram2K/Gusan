import SwiftUI

struct OnboardingView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    private var step: OnboardingStep { model.onboardingStep }
    private var title: String {
        switch step { case .welcome: return "Welcome"; case .signIn: return "Sign in"; case .addKeyboard: return "Add Gusan"; case .mainKeyboard: return "Make it yours"; case .tryIt: return "Try it" }
    }
    var body: some View {
        NavigationStack {
            ScreenContent {
                Text("Step \(step.rawValue + 1) of \(OnboardingStep.allCases.count)").font(.caption).foregroundStyle(.secondary).accessibilityLabel("Setup, step \(step.rawValue + 1) of 5")
                ProgressView(value: Double(step.rawValue + 1), total: 5).accessibilityHidden(true)
                Group {
                    switch step {
                    case .welcome: welcome
                    case .signIn:
                        if model.session == nil { SignInCard() }
                        else { AppCard { Label("You're signed in", systemImage: "checkmark.circle.fill").font(.title2); Text(model.session?.email ?? "").font(.body); PrimaryButton(title: "Continue") { advance() } } }
                    case .addKeyboard: addKeyboard
                    case .mainKeyboard: mainKeyboard
                    case .tryIt:
                        PlaygroundCard()
                        AppCard { Text("Try an action, or keep typing normally. Undo stays in the Gusan bar after a replacement.").font(.body); PrimaryButton(title: "Done", icon: "checkmark") { model.impact(); model.finishOnboarding() } }
                    }
                }.id(step).transition(reduceMotion ? .opacity : .opacity.combined(with: .move(edge: .trailing)))
            }.navigationTitle(title).navigationBarTitleDisplayMode(.inline)
                .animation(reduceMotion ? .easeInOut(duration: 0.15) : .spring(response: 0.35, dampingFraction: 0.85), value: step)
                .toolbar {
                    ToolbarItem(placement: .navigationBarLeading) { if step != .welcome { Button("Back") { model.onboardingStep = OnboardingStep(rawValue: step.rawValue - 1) ?? .welcome }.frame(minWidth: 44, minHeight: 44) } }
                    ToolbarItem(placement: .navigationBarTrailing) { Button("Skip for now") { model.cancelGoogle(); model.finishOnboarding() }.frame(minHeight: 44).accessibilityHint("Finish setup later from Home") }
                }
                .onChange(of: model.session) { session in if session != nil, step == .signIn { advance() } }
        }.interactiveDismissDisabled()
    }
    private var welcome: some View {
        VStack(alignment: .leading, spacing: 24) {
            VStack(alignment: .leading, spacing: 20) {
                BrandMark(large: true)
                Text("Gusan, the AI keyboard").font(.largeTitle).bold()
                Text("Rewrite, translate, summarize and more, in any app.").font(.title2)
            }.foregroundStyle(.white).frame(maxWidth: .infinity, alignment: .leading).padding(24).background(Theme.hero, in: RoundedRectangle(cornerRadius: 16, style: .continuous))
            AppCard {
                Label("Your keyboard, with Gusan built in", systemImage: "keyboard").font(.headline)
                Text("Keep typing. When you need a little help, Gusan is one tap away.").font(.body).foregroundStyle(.secondary)
                PrimaryButton(title: "Get started", icon: "arrow.right") { advance() }
            }
        }
    }
    private var addKeyboard: some View {
        VStack(spacing: 12) {
            AppCard {
                Text("A little setup. A lot of possibility.").font(.title2).bold()
                numbered("1", icon: "gearshape", text: "Open Settings")
                numbered("2", icon: "keyboard", text: "Keyboards › Add New Keyboard › Gusan")
                numbered("3", icon: "network", text: "Turn on Allow Full Access")
                PrimaryButton(title: "Open Gusan Settings", icon: "arrow.up.forward.app") { model.impact(); Theme.openSettings() }
                Text("Or go to Settings › General › Keyboard › Keyboards › Add New Keyboard › Gusan.").font(.footnote).foregroundStyle(.secondary)
                StatusRow(title: model.keyboardAdded ? "Keyboard added ✓" : "Not added yet", detail: model.keyboardCheck, confirmed: model.keyboardAdded)
            }
            AppCard {
                Label("Full Access, explained", systemImage: "hand.raised").font(.headline)
                Text("Gusan uses nearby text on your iPhone to show its context preview. Only when you tap an AI action does it send the shown text and your instruction to Gusan Cloud.").font(.body)
                Text("Full Access lets the keyboard connect to Gusan Cloud and see your sign-in. Gusan doesn't send text while you type.").font(.footnote).foregroundStyle(.secondary)
            }
            PrimaryButton(title: "Continue", icon: "arrow.right") { advance() }
        }
    }
    private var mainKeyboard: some View {
        VStack(spacing: 12) {
            AppCard {
                Image(systemName: "arrow.up.to.line").font(.largeTitle).foregroundStyle(Theme.accent).accessibilityHidden(true)
                Text("Make Gusan your main keyboard").font(.title2).bold()
                Text("Settings › General › Keyboard › Keyboards › Edit").font(.headline)
                Text("Drag Gusan to the top of the list, then tap Done.").font(.body)
                PrimaryButton(title: "Open Gusan Settings", icon: "arrow.up.forward.app") { Theme.openSettings() }
                Text("You can always switch keyboards with the globe key.").font(.body).foregroundStyle(.secondary)
            }
            AppCard {
                Label("Speak with Gusan", systemImage: "mic").font(.headline)
                Text("Tap Gusan's mic above the keys, speak, then tap Stop. Smart voice applies your words or edits the selected text; Undo restores the original. Gusan opens once to activate the microphone — follow the on-screen guidance. Later taps record in place while the voice session is ready. Apple's mic below the keyboard is system dictation.").font(.body)
            }
            PrimaryButton(title: "Continue", icon: "arrow.right") { advance() }
        }
    }
    private func numbered(_ number: String, icon: String, text: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Text(number).font(.headline).foregroundStyle(Theme.accent).frame(minWidth: 24)
            Label(text, systemImage: icon).font(.body)
        }.padding(.vertical, 8).accessibilityElement(children: .combine)
    }
    private func advance() {
        model.impact()
        if step == .mainKeyboard { UserDefaults.standard.set(true, forKey: "mainKeyboardConfirmed") }
        model.onboardingStep = OnboardingStep(rawValue: step.rawValue + 1) ?? .tryIt
    }
}
