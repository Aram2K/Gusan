import SwiftUI
import AuthenticationServices
import GusanKit

enum AppTab: Hashable { case home, keyboard, account }
enum OnboardingStep: Int, CaseIterable { case welcome, signIn, addKeyboard, mainKeyboard, tryIt }

@MainActor
final class AppModel: NSObject, ObservableObject, ASWebAuthenticationPresentationContextProviding {
    let store = SessionStore()
    lazy var auth = AuthClient(store: store)
    lazy var settings = SettingsStore(group: store.resolution.chosen)
    @Published var session: Session?
    @Published var isPro = false
    @Published var loadingPlan = false
    @Published var planError: String?
    @Published var authError: String?
    @Published var authBusy = false
    @Published var usingGoogle = false
    @Published var diagnostics: SessionDiagnostics?
    @Published var selectedTab: AppTab = .home
    @Published var showingOnboarding = false
    @Published var onboardingStep: OnboardingStep = .welcome
    @Published var showingSignIn = false
    @Published var fromKeyboard = false
    private var webSession: ASWebAuthenticationSession?
    private var pkce: PKCE?
    private var updating = false
    var pendingSignIn = false
    var pendingOnboarding = false

    override init() {
        super.init()
        #if DEBUG
        let args = ProcessInfo.processInfo.arguments
        if args.contains("--gusan-pasteboard-only"), args.contains("--gusan-debug-session") {
            store.write(Session(accessToken: "gusan-debug-no-network", expiresAt: .distantFuture, refreshToken: "gusan-debug-no-network", userID: "debug", email: "alex@example.invalid"))
        }
        #endif
        session = store.read(); diagnostics = store.diagnostics()
        loadingPlan = session != nil
        showingOnboarding = !UserDefaults.standard.bool(forKey: "onboardingComplete")
        #if DEBUG
        if args.contains("--gusan-skip-onboarding") { showingOnboarding = false }
        if let index = args.firstIndex(of: "--gusan-onboarding-step"), args.indices.contains(index + 1), let number = Int(args[index + 1]), let step = OnboardingStep(rawValue: number) {
            onboardingStep = step; showingOnboarding = true
        }
        #endif
    }
    var keyboardAdded: Bool { diagnostics?.peer != nil }
    var fullAccess: Bool? { diagnostics?.peer?.fullAccess }
    var keyboardHasSession: Bool { diagnostics?.peer?.stores.contains { $0.hasSession && $0.readable } == true }
    var ready: Bool { keyboardAdded && fullAccess == true && keyboardHasSession && session != nil }
    var mainKeyboardConfirmed: Bool { UserDefaults.standard.bool(forKey: "mainKeyboardConfirmed") }
    var setupFinished: Bool { ready && mainKeyboardConfirmed }
    var keyboardCheck: String { diagnostics?.peer.map { "Last checked " + $0.timestamp.formatted(date: .omitted, time: .shortened) } ?? "Open the Gusan keyboard to check setup." }
    var missingStep: OnboardingStep { session == nil ? .signIn : (!keyboardAdded || fullAccess != true ? .addKeyboard : (mainKeyboardConfirmed ? .tryIt : .mainKeyboard)) }
    func refreshDiagnostics() { diagnostics = store.diagnostics() }
    func update() async {
        guard !updating else { return }; updating = true
        defer { updating = false; loadingPlan = false; refreshDiagnostics() }
        session = store.read(); planError = nil
        guard session != nil else { isPro = false; return }
        #if DEBUG
        if session?.accessToken == "gusan-debug-no-network" { isPro = ProcessInfo.processInfo.arguments.contains("--gusan-debug-pro"); return }
        #endif
        loadingPlan = true
        do {
            session = try await auth.refresh()
            if let session { isPro = try await auth.entitlement(accessToken: session.accessToken).isPro }
        } catch GusanError.unauthorized { session = store.read(); isPro = false }
        catch { planError = friendly(error) }
    }
    func signIn(email: String, password: String) async {
        usingGoogle = false; authBusy = true; authError = nil
        defer { authBusy = false }
        do { session = try await auth.signIn(email: email.trimmingCharacters(in: .whitespacesAndNewlines), password: password); feedback(.success); await update() }
        catch { authError = friendly(error); feedback(.error) }
    }
    func signOut() async {
        cancelGoogle(); authBusy = true
        #if DEBUG
        if session?.accessToken == "gusan-debug-no-network" { store.clear() }
        else { await auth.signOut() }
        #else
        await auth.signOut()
        #endif
        session = store.read(); isPro = false; planError = nil; authError = nil; authBusy = false; refreshDiagnostics()
    }
    func google() {
        guard !authBusy else { return }
        usingGoogle = true; authError = nil; authBusy = true
        do {
            let proof = try PKCE.generate(); pkce = proof
            let flow = ASWebAuthenticationSession(url: AuthClient.authorizeURL(pkce: proof), callbackURLScheme: "gusan") { [weak self] url, error in
                Task { @MainActor in
                    guard let self else { return }
                    self.webSession = nil
                    if let url { self.handleURL(url) }
                    else {
                        self.pkce = nil; self.authBusy = false
                        if (error as? ASWebAuthenticationSessionError)?.code != .canceledLogin { self.authError = "Couldn't sign in with Google. Try again, or use email." }
                    }
                }
            }
            flow.presentationContextProvider = self; webSession = flow
            if !flow.start() { webSession = nil; pkce = nil; authBusy = false; authError = "Couldn't open Google sign-in. Try again, or use email." }
        } catch { authBusy = false; pkce = nil; authError = "Couldn't start sign-in. Please try again." }
    }
    func cancelGoogle() { webSession?.cancel(); webSession = nil; pkce = nil; authBusy = false }
    func handleURL(_ url: URL) {
        guard url.scheme == "gusan" else { return }
        switch url.host {
        case "signin":
            session = store.read(); refreshDiagnostics()
            cancelGoogle(); authError = nil; fromKeyboard = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.contains { $0.name == "from" && $0.value == "keyboard" } == true
            if showingOnboarding { pendingSignIn = true; showingOnboarding = false }
            else { showingSignIn = true }
        case "onboarding":
            cancelGoogle(); onboardingStep = .welcome
            if showingSignIn { pendingOnboarding = true; showingSignIn = false }
            else { showingOnboarding = true }
        case "auth-callback":
            guard let proof = pkce else { return }
            pkce = nil
            let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            guard let code = items.first(where: { $0.name == "code" })?.value else {
                authBusy = false; authError = "Google sign-in didn't finish. Please try again, or use email."; return
            }
            Task {
                defer { authBusy = false }
                do { session = try await auth.exchange(code: code, pkce: proof); feedback(.success); await update() }
                catch { authError = friendly(error); feedback(.error) }
            }
        default: break
        }
    }
    func finishOnboarding() { UserDefaults.standard.set(true, forKey: "onboardingComplete"); showingOnboarding = false }
    func resumeSetup() { onboardingStep = missingStep; showingOnboarding = true }
    func impact() { if settings.haptics { UIImpactFeedbackGenerator(style: .light).impactOccurred() } }
    private func feedback(_ type: UINotificationFeedbackGenerator.FeedbackType) { if settings.haptics { UINotificationFeedbackGenerator().notificationOccurred(type) } }
    private func friendly(_ error: Error) -> String {
        switch error as? GusanError {
        case .network: return "You're offline. Check your connection and try again."
        case .timeout: return "That took too long. Please try again."
        case .signInFailed(let message): return message
        default: return "Something went wrong. Please try again."
        }
    }
    func presentationAnchor(for session: ASWebAuthenticationSession) -> ASPresentationAnchor {
        UIApplication.shared.connectedScenes.compactMap { $0 as? UIWindowScene }.flatMap(\.windows).first(where: \.isKeyWindow) ?? ASPresentationAnchor()
    }
}
