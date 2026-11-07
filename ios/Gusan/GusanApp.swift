import SwiftUI
import AuthenticationServices
import GusanKit

@main
struct GusanApp: App {
    @StateObject private var account = AccountModel()
    var body: some Scene {
        WindowGroup {
            ContentView(account: account)
                .onOpenURL { account.handleURL($0) }
        }
    }
}

@MainActor
final class AccountModel: NSObject, ObservableObject, ASWebAuthenticationPresentationContextProviding {
    let store = SessionStore()
    lazy var auth = AuthClient(store: store)
    @Published var session: Session?
    @Published var tier = "Checking plan…"
    @Published var error = ""
    @Published var busy = false
    @Published var diagnostics: SessionDiagnostics?
    @Published var signInRequest = UUID()
    private var webSession: ASWebAuthenticationSession?
    private var pkce: PKCE?
    override init() {
        super.init()
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--gusan-pasteboard-only"), ProcessInfo.processInfo.arguments.contains("--gusan-debug-session") {
            store.write(Session(accessToken: "gusan-debug-no-network", expiresAt: .distantFuture, refreshToken: "gusan-debug-no-network", userID: "debug", email: "pasteboard@example.invalid"))
        }
        #endif
        session = store.read()
        diagnostics = store.diagnostics()
    }
    func update() async {
        session = store.read()
        #if DEBUG
        if session?.accessToken == "gusan-debug-no-network" {
            tier = "DEBUG · fake session"; diagnostics = store.diagnostics(); return
        }
        #endif
        if session != nil {
            do {
                session = try await auth.refresh()
                if let session {
                    let entitlement = try await auth.entitlement(accessToken: session.accessToken)
                    tier = entitlement.isPro ? "Gusan Pro" : "Free"
                }
            } catch GusanError.unauthorized {
                session = store.read(); tier = ""
            } catch { self.error = error.localizedDescription; tier = "Plan unavailable" }
        }
        diagnostics = store.diagnostics()
    }
    func signIn(email: String, password: String) async {
        busy = true; error = ""
        defer { busy = false }
        do { session = try await auth.signIn(email: email, password: password); await update() }
        catch { self.error = error.localizedDescription }
    }
    func signOut() async {
        busy = true
        await auth.signOut()
        session = store.read(); diagnostics = store.diagnostics(); busy = false
    }
    func google() {
        do {
            let proof = try PKCE.generate()
            pkce = proof; busy = true; error = ""
            webSession = ASWebAuthenticationSession(url: AuthClient.authorizeURL(pkce: proof), callbackURLScheme: "gusan") { [weak self] url, error in
                Task { @MainActor in
                    guard let self else { return }
                    if let url { self.handleURL(url) }
                    else {
                        self.busy = false; self.pkce = nil
                        if (error as? ASWebAuthenticationSessionError)?.code != .canceledLogin { self.error = error?.localizedDescription ?? "Google sign-in failed." }
                    }
                }
            }
            webSession?.presentationContextProvider = self
            if webSession?.start() != true { busy = false; error = "Could not open Google sign-in." }
        } catch { self.error = error.localizedDescription }
    }
    func handleURL(_ url: URL) {
        guard url.scheme == "gusan" else { return }
        switch url.host {
        case "signin":
            signInRequest = UUID()
        case "auth-callback":
            guard let proof = pkce else { return }
            pkce = nil
            let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            guard let code = items.first(where: { $0.name == "code" })?.value else {
                busy = false
                error = items.first(where: { $0.name == "error_description" })?.value ?? "Google sign-in returned no code."
                return
            }
            Task {
                defer { busy = false }
                do { session = try await auth.exchange(code: code, pkce: proof); await update() }
                catch { self.error = error.localizedDescription }
            }
        default: break
        }
    }
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows).first(where: \.isKeyWindow) ?? ASPresentationAnchor()
    }
}

struct ContentView: View {
    @ObservedObject var account: AccountModel
    @Environment(\.scenePhase) private var scenePhase
    @State private var email = ""
    @State private var password = ""
    @State private var sample = "Write an email to the team thanking them for a great launch."
    @State private var action: SmartActionMode = .smartAuto
    @State private var target = "en"
    private var settings: SettingsStore { SettingsStore(group: account.store.resolution.chosen) }
    var body: some View {
        NavigationStack {
            ScrollViewReader { reader in
            Form {
                Section("Account") {
                    if let session = account.session {
                        Label(session.email ?? "Signed in", systemImage: "person.crop.circle")
                        Text(account.tier).foregroundStyle(.secondary)
                        Button("Sign out", role: .destructive) { Task { await account.signOut() } }
                    } else {
                        TextField("Email", text: $email).textContentType(.emailAddress).keyboardType(.emailAddress).textInputAutocapitalization(.never).autocorrectionDisabled()
                        SecureField("Password", text: $password).textContentType(.password)
                        Button("Sign in") { Task { await account.signIn(email: email, password: password); password = "" } }
                            .disabled(email.isEmpty || password.isEmpty)
                        Button("Continue with Google") { account.google() }
                        Link("Create an account at gusan.aibuben.xyz", destination: URL(string: "https://gusan.aibuben.xyz")!)
                        Text("Google sign-in requires gusan://auth-callback in the Supabase redirect allow-list.").font(.footnote).foregroundStyle(.secondary)
                    }
                    if account.busy { ProgressView() }
                    if !account.error.isEmpty { Text(account.error).font(.footnote).foregroundStyle(.red) }
                }.disabled(account.busy).id("account")
                Section("Keyboard setup") {
                    Text("1. Open Settings › General › Keyboard › Keyboards.\n2. Tap Add New Keyboard… and choose Gusan.\n3. Tap Gusan and turn on Allow Full Access.")
                    Text("Full Access lets Gusan send your selected or surrounding text to Gusan Cloud only when you run an action.").font(.footnote).foregroundStyle(.secondary)
                    Button("Open Settings") { UIApplication.shared.open(URL(string: UIApplication.openSettingsURLString)!) }
                    Text("Try it").font(.headline)
                    TextEditor(text: $sample).frame(minHeight: 100).accessibilityLabel("Try the Gusan keyboard")
                }
                Section("Keyboard settings") {
                    Picker("Default action", selection: $action) {
                        ForEach(SmartActionMode.allCases, id: \.self) { Text($0.label).tag($0) }
                    }.onChange(of: action) { settings.defaultAction = $0 }
                    Picker("Translate to", selection: $target) {
                        ForEach(["en", "hy", "ru", "fr", "de", "es", "it", "pt", "ar"], id: \.self) { Text(Prompts.translateTargets[$0]!).tag($0) }
                    }.onChange(of: target) { settings.translateTarget = $0 }
                }
                Section("Sharing diagnostics") {
                    LabeledContent("App group", value: account.store.resolution.chosen ?? "None")
                    Text("Candidates: " + account.store.resolution.candidatesTried.joined(separator: ", ")).font(.caption)
                    LabeledContent("Shared keychain", value: account.diagnostics?.keychainOK == true ? "OK" : "Unavailable")
                    LabeledContent("Shared file", value: account.diagnostics?.fileOK == true ? "OK" : "Unavailable")
                    LabeledContent("Team pasteboard", value: account.diagnostics?.pasteboardOK == true ? "OK" : "Unavailable")
                    LabeledContent("Session storage", value: account.diagnostics?.sharingStatus ?? "not shared")
                    if let diagnostics = account.diagnostics {
                        Text(diagnostics.summary).font(.caption)
                        Text("Checked " + diagnostics.local.timestamp.formatted()).font(.caption)
                        Text("Keychain group: " + (diagnostics.local.keychainGroupID ?? "None")).font(.caption)
                        if let peer = diagnostics.peer {
                            Text("Keyboard sees: " + peer.stores.map(\.summary).joined(separator: "; ")).font(.caption)
                            Text("Keyboard checked \(peer.timestamp.formatted()) · group \(peer.groupID ?? "None") · keychain \(peer.keychainGroupID ?? "None")").font(.caption)
                        } else { Text("Keyboard: no report yet").font(.caption) }
                    }
                }.font(.footnote)
            }
            .navigationTitle("Gusan")
            .task { action = settings.defaultAction; target = settings.translateTarget; await account.update() }
            .onChange(of: scenePhase) { if $0 == .active { Task { await account.update() } } }
            .onChange(of: account.signInRequest) { _ in
                UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
                reader.scrollTo("account", anchor: .top)
            }
            }
        }
    }
}
