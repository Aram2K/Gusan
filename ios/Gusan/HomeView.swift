import SwiftUI

struct PlaygroundCard: View {
    @EnvironmentObject private var model: AppModel
    @State private var text = "Thanks for all your help with the launch. The team did a great job!"
    @FocusState private var focused: Bool
    @State private var focusedAt = Date.distantFuture
    private let timer = Timer.publish(every: 3, on: .main, in: .common).autoconnect()
    var body: some View {
        AppCard {
            Text("Try Gusan").font(.title2).bold()
            Text("Select text → tap the Gusan button → pick an action, or type an instruction.").font(.subheadline).foregroundStyle(.secondary)
            TextEditor(text: $text).font(.body).frame(minHeight: 120).focused($focused)
                .onAppear { if ProcessInfo.processInfo.arguments.contains("--gusan-focus-playground") { focused = true } }
                .padding(8).background(Theme.surface, in: RoundedRectangle(cornerRadius: 14)).scrollContentBackground(.hidden)
                .accessibilityLabel("Try the Gusan keyboard").accessibilityHint("Select some text, then open Gusan actions in the keyboard")
            Text(model.diagnostics?.peer.map { $0.timestamp >= focusedAt } == true ? "Gusan is connected." : "Use the globe key to switch to Gusan.").font(.footnote).foregroundStyle(.secondary)
            if focused { Button("Done typing") { focused = false }.frame(minHeight: 44) }
        }
        .onChange(of: focused) { value in if value { focusedAt = Date() } }
        .onReceive(timer) { _ in if focused { model.refreshDiagnostics() } }
    }
}

struct HomeView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var screen: ScreenMonitor
    private let examples: [(String, String, String, String)] = [
        ("Ask Gusan", "sparkles", "Make this warmer: Thanks for the update.", "Thanks so much for keeping me in the loop!"),
        ("Rephrase", "arrow.triangle.2.circlepath", "We need to do a check of the plan.", "Let's review the plan."),
        ("Fix grammar", "checkmark.seal", "The team have finish the work.", "The team has finished the work."),
        ("Email", "envelope", "Thank the team for the launch.", "Hi team, thank you for a fantastic launch…"),
        ("To-do", "checklist", "Book a room and send the agenda.", "☐ Book a room\n☐ Send the agenda"),
        ("Translate", "character.bubble", "Thank you!", "Շնորհակալություն։"),
        ("Summarize", "text.alignleft", "A long update with decisions and next steps…", "The key points, clearly and concisely.")
    ]
    var body: some View {
        ScreenContent {
            if screen.isLive { ScreenCard() }
            AppCard {
                HStack(alignment: .top, spacing: 12) { BrandMark(); Text("Welcome to Gusan").font(.title2).bold() }
                Text(model.session?.email ?? "Sign in to use Gusan Cloud.").font(.subheadline).foregroundStyle(.secondary)
                if model.session == nil {
                    Button("Sign in") { model.impact(); model.handleURL(URL(string: "gusan://signin")!) }.frame(minHeight: 44)
                }
                if model.loadingPlan { ProgressView("Checking your plan…") }
                else if model.session != nil, model.planError == nil { PlanBadge(pro: model.isPro) }
                if let error = model.planError {
                    Text(error).font(.footnote).foregroundStyle(Theme.danger)
                    Button("Try again") { Task { await model.update() } }.frame(minHeight: 44)
                }
                Divider()
                StatusRow(title: "Keyboard added", detail: model.keyboardAdded ? "Gusan has been used on this iPhone." : "Not checked yet. Add Gusan in Settings.", confirmed: model.keyboardAdded)
                StatusRow(title: "Full Access", detail: model.fullAccess.map { $0 ? "Enabled at the last keyboard check." : "Turn on Full Access for AI actions." } ?? "Open Gusan to check Full Access.", confirmed: model.fullAccess == true)
                StatusRow(title: "Ready to write", detail: model.ready ? "Your keyboard can see your sign-in." : (model.session == nil ? "Sign in and finish setup to connect Gusan." : "Finish keyboard setup to connect Gusan."), confirmed: model.ready)
                Text(model.keyboardCheck).font(.caption).foregroundStyle(.secondary)
                if !model.setupFinished { PrimaryButton(title: "Finish setup", icon: "arrow.right") { model.impact(); model.resumeSetup() } }
            }
            PlaygroundCard()
            if !screen.isLive { ScreenCard() }
            AppCard {
                NavigationLink { HowToUseView() } label: { Label("How to use Gusan", systemImage: "questionmark.circle").frame(minHeight: 44) }
                Button("Review keyboard setup") { model.resumeSetup() }.frame(minHeight: 44)
                Button("Keyboard preferences") { model.selectedTab = .keyboard }.frame(minHeight: 44)
            }
            Text("What you can do").font(.title2).bold().padding(.top, 12)
            ForEach(examples, id: \.0) { example in
                AppCard {
                    Label(example.0, systemImage: example.1).font(.headline)
                    Text("Before").font(.caption).foregroundStyle(.secondary)
                    Text(example.2).font(.body).foregroundStyle(.secondary)
                    Image(systemName: "arrow.down").foregroundStyle(Theme.accent).accessibilityLabel("Becomes")
                    Text(example.3).font(.body)
                }
            }
        }.navigationTitle("Home")
    }
}
