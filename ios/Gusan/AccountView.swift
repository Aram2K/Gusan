import SwiftUI

struct SignInCard: View {
    @EnvironmentObject private var model: AppModel
    @State private var email = ""
    @State private var password = ""
    @State private var emailExpanded = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    var body: some View {
        AppCard {
            Text("Your words, with a little help.").font(.title2).bold()
            Text("Sign in to use Gusan Cloud from your keyboard. AI actions require Gusan Pro.").font(.body).foregroundStyle(.secondary)
            Button { model.impact(); model.google() } label: {
                HStack(spacing: 12) {
                    if model.authBusy && model.usingGoogle { ProgressView().tint(.black) }
                    else { Image("GoogleG").resizable().scaledToFit().frame(width: 24, height: 24).accessibilityHidden(true) }
                    Text("Continue with Google").font(.headline).multilineTextAlignment(.center)
                }.frame(maxWidth: .infinity, minHeight: 52).padding(.horizontal, 12)
                    .contentShape(Rectangle())
            }.buttonStyle(.plain).foregroundStyle(.black).background(.white, in: RoundedRectangle(cornerRadius: 14, style: .continuous))
                .overlay { RoundedRectangle(cornerRadius: 14, style: .continuous).stroke(Color.gray.opacity(0.3)).allowsHitTesting(false) }
                .disabled(model.authBusy).accessibilityHint("Opens secure Google sign-in")
            Button("Use email instead") { withAnimation(reduceMotion ? .easeInOut(duration: 0.15) : .spring(response: 0.35, dampingFraction: 0.85)) { emailExpanded.toggle() } }
                .frame(minHeight: 44).disabled(model.authBusy).accessibilityHint("Shows email and password fields")
            if emailExpanded {
                VStack(spacing: 12) {
                    TextField("Email", text: $email).keyboardType(.emailAddress).textContentType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled().accessibilityLabel("Email").frame(minHeight: 44).disabled(model.authBusy)
                    SecureField("Password", text: $password).textContentType(.password).accessibilityLabel("Password").frame(minHeight: 44).disabled(model.authBusy)
                    PrimaryButton(title: "Sign in", busy: model.authBusy) { model.impact(); Task { await model.signIn(email: email, password: password); password = "" } }.disabled(email.trimmingCharacters(in: .whitespaces).isEmpty || password.isEmpty || model.authBusy)
                }.textFieldStyle(.roundedBorder).transition(.opacity)
            }
            if let error = model.authError { Label(error, systemImage: "exclamationmark.circle").font(.footnote).foregroundStyle(Theme.danger).accessibilityLabel("Sign-in error. " + error) }
            Link("Create an account", destination: Theme.website).frame(minHeight: 44).disabled(model.authBusy)
        }
    }
}

struct SignInSheet: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.dismiss) private var dismiss
    var body: some View {
        NavigationStack {
            ScreenContent {
                if model.session != nil {
                    AppCard {
                        Image(systemName: "checkmark.circle.fill").font(.largeTitle).foregroundStyle(Theme.success).accessibilityHidden(true)
                        Text(model.fromKeyboard ? "You're signed in, switch back to your app" : "You're signed in").font(.title2).bold()
                        Text(model.fromKeyboard ? "Use the ◀ back link at the top-left, or switch back to your app. Your Gusan keyboard is ready to reconnect." : "You're ready to set up your Gusan keyboard.").font(.body).foregroundStyle(.secondary)
                        PrimaryButton(title: "Done") { dismiss() }
                    }
                } else { SignInCard() }
            }.navigationTitle("Sign in").navigationBarTitleDisplayMode(.inline)
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() }.frame(minWidth: 44, minHeight: 44) } }
        }
    }
}

struct AccountView: View {
    @EnvironmentObject private var model: AppModel
    @State private var confirmingSignOut = false
    var body: some View {
        ScreenContent {
            if let session = model.session {
                AppCard {
                    Text(String((session.email ?? "G").prefix(1)).uppercased()).font(.largeTitle).bold().frame(minWidth: 72, minHeight: 72).background(Theme.accent.opacity(0.12), in: Circle()).accessibilityHidden(true)
                    Text(session.email ?? "Your Gusan account").font(.title2).bold().textSelection(.enabled)
                    if model.loadingPlan { ProgressView("Checking your plan…") }
                    else if let error = model.planError {
                        Text(error).foregroundStyle(Theme.danger)
                        Button("Try again") { Task { await model.update() } }.frame(minHeight: 44)
                    } else {
                        PlanBadge(pro: model.isPro)
                        Text(model.isPro ? "Gusan Cloud is included in your plan." : "Upgrade to Gusan Pro for AI actions in any app.").font(.body).foregroundStyle(.secondary)
                        Link("Manage plan", destination: Theme.website).frame(minHeight: 44)
                        if !model.isPro { Link("Explore Gusan Pro", destination: Theme.pricing).frame(minHeight: 44) }
                    }
                }
                AppCard {
                    Button("Sign out", role: .destructive) { confirmingSignOut = true }.frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).disabled(model.authBusy)
                    if model.authBusy { ProgressView("Signing out…") }
                }
            } else { SignInCard() }
        }.navigationTitle("Account")
            .confirmationDialog("Sign out of Gusan?", isPresented: $confirmingSignOut, titleVisibility: .visible) {
                Button("Sign out", role: .destructive) { Task { await model.signOut() } }
                Button("Cancel", role: .cancel) {}
            } message: { Text("Your keyboard will still type. Sign in again to use AI actions.") }
    }
}
