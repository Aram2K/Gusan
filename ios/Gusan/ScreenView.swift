import SwiftUI
import ReplayKit
import GusanKit

/// Watches the Gusan Screen heartbeat so the app can show whether a broadcast is live.
@MainActor
final class ScreenMonitor: ObservableObject {
    @Published private(set) var status: ScreenStatus?
    @Published var idleMinutes = ScreenIdleTimer.defaultMinutes
    @Published var explained = false
    let settings: ScreenSettings
    let channel: ScreenChannel
    private var observer: DarwinObserver?
    private var timer: Timer?
    init(store: SessionStore = SessionStore()) {
        settings = ScreenSettings(group: store.resolution.chosen)
        channel = ScreenChannel(container: store.resolution.container, pasteboardAllowed: { [settings = SettingsStore(group: store.resolution.chosen)] in settings.pasteboardFallback })
        idleMinutes = settings.idleMinutes; explained = settings.explained
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--gusan-debug-screen-live") {
            status = ScreenStatus(startedAt: Date().addingTimeInterval(-90), idleDeadline: Date().addingTimeInterval(510), width: 590, height: 1280, requests: 2)
        }
        #endif
    }
    var isLive: Bool { status?.isLive() == true }
    var preferredExtension: String { (Bundle.main.bundleIdentifier ?? "com.aram2k.gusan") + ".screen" }
    func start() {
        refresh()
        observer = channel.observe(.status) { [weak self] in self?.refresh() }
        timer?.invalidate()
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in Task { @MainActor in self?.refresh() } }
    }
    func stop() { observer = nil; timer?.invalidate(); timer = nil }
    func refresh() {
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--gusan-debug-screen-live") { return }
        #endif
        status = channel.readStatus()
    }
    func acknowledge() { settings.explained = true; explained = true }
    func setIdle(_ minutes: Int) { settings.idleMinutes = minutes; idleMinutes = settings.idleMinutes }
}

/// The system broadcast picker, stretched under a Gusan-styled label so it reads as one button.
struct BroadcastPickerButton: UIViewRepresentable {
    let preferredExtension: String
    func makeUIView(context: Context) -> BroadcastPickerHost { BroadcastPickerHost(preferredExtension: preferredExtension) }
    func updateUIView(_ uiView: BroadcastPickerHost, context: Context) {}
}

final class BroadcastPickerHost: UIView {
    private let picker: RPSystemBroadcastPickerView
    init(preferredExtension: String) {
        picker = RPSystemBroadcastPickerView(frame: .zero)
        picker.preferredExtension = preferredExtension
        picker.showsMicrophoneButton = false
        super.init(frame: .zero)
        addSubview(picker)
        isAccessibilityElement = true
        accessibilityLabel = "Start Gusan Screen"
        accessibilityHint = "Opens the system broadcast sheet. Tap Start Broadcast there."
        accessibilityTraits = .button
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layoutSubviews() {
        super.layoutSubviews()
        picker.frame = bounds
        for case let button as UIButton in picker.subviews {
            button.frame = bounds
            button.setImage(nil, for: .normal); button.setImage(nil, for: .highlighted)
            button.imageView?.isHidden = true
        }
    }
}

struct ScreenView: View {
    @EnvironmentObject private var model: AppModel
    @EnvironmentObject private var screen: ScreenMonitor
    var body: some View {
        ScreenContent {
            AppCard {
                HStack(alignment: .top, spacing: 12) {
                    Image(systemName: "eye").font(.title).foregroundStyle(Theme.brandBlue).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Gusan Screen").font(.title2).bold()
                        Text("Let Gusan see what's on your screen, so you can say \"reply to this\" or \"what does this say?\" from the keyboard.").font(.subheadline).foregroundStyle(.secondary)
                    }
                }
            }
            AppCard {
                Text("What is shared").font(.headline)
                bullet("eye.trianglebadge.exclamationmark", "iOS shows the red recording indicator the whole time Gusan Screen runs. Tap it, or use Control Center, to stop.")
                bullet("lock.shield", "Nothing leaves your iPhone on its own. Gusan Screen keeps only the latest picture in memory, downscaled, and never saves or logs it.")
                bullet("paperplane", "Only when you ask something with the keyboard's Screen chip, that one picture is sent to Gusan Cloud together with your instruction and the text in the field. The answer comes back into your text, with Undo.")
                bullet("eye.slash", "Passwords and other secure fields are blanked by iOS and never used.")
                bullet("timer", "It stops by itself after \(screen.idleMinutes) minutes without a question.")
            }
            if !screen.explained {
                PrimaryButton(title: "I understand, continue", icon: "checkmark") { model.impact(); screen.acknowledge() }
            } else {
                AppCard {
                    HStack(spacing: 12) {
                        Circle().fill(screen.isLive ? Theme.danger : Color.secondary).frame(width: 10, height: 10).accessibilityHidden(true)
                        VStack(alignment: .leading, spacing: 4) {
                            Text(screen.isLive ? "Live" : "Not running").font(.headline)
                            Text(statusDetail).font(.footnote).foregroundStyle(.secondary)
                        }
                    }.accessibilityElement(children: .combine)
                    if screen.isLive {
                        Text("Switch to any app, open the Gusan keyboard and tap the Screen chip. To stop, tap the red indicator at the top of the screen.").font(.footnote).foregroundStyle(.secondary)
                    } else {
                        ZStack {
                            PrimaryButton(title: "Start Gusan Screen", icon: "record.circle") {}.allowsHitTesting(false)
                            BroadcastPickerButton(preferredExtension: screen.preferredExtension).frame(height: 52)
                        }.frame(minHeight: 52)
                        Text("Pick “Gusan Screen” in the sheet, then Start Broadcast. You can also start it from Control Center's screen recording control.").font(.footnote).foregroundStyle(.secondary)
                    }
                }
                AppCard {
                    Picker("Stop after", selection: Binding(get: { screen.idleMinutes }, set: { screen.setIdle($0) })) {
                        ForEach(ScreenIdleTimer.minuteOptions, id: \.self) { Text("\($0) min without a question").tag($0) }
                    }.frame(minHeight: 44)
                    Text("Applies to the next start. The broadcast always ends when you stop it yourself.").font(.footnote).foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Gusan Screen").navigationBarTitleDisplayMode(.inline)
        .onAppear { screen.start() }
        .onDisappear { screen.stop() }
    }
    private var statusDetail: String {
        guard let status = screen.status, screen.isLive else { return "Start it here, or from Control Center." }
        var parts = ["Since " + status.startedAt.formatted(date: .omitted, time: .shortened)]
        parts.append(status.hasFrame ? "picture \(status.width)×\(status.height)" : "waiting for the first picture")
        parts.append("stops at " + status.idleDeadline.formatted(date: .omitted, time: .shortened) + " if unused")
        return parts.joined(separator: " · ")
    }
    private func bullet(_ icon: String, _ text: String) -> some View {
        HStack(alignment: .top, spacing: 12) {
            Image(systemName: icon).foregroundStyle(Theme.accent).frame(width: 24).accessibilityHidden(true)
            Text(text).font(.footnote)
        }
    }
}

/// Home card: one line of status and a link to the full screen.
struct ScreenCard: View {
    @EnvironmentObject private var screen: ScreenMonitor
    var body: some View {
        AppCard {
            NavigationLink { ScreenView() } label: {
                HStack(spacing: 12) {
                    Image(systemName: "eye").foregroundStyle(Theme.brandBlue).accessibilityHidden(true)
                    VStack(alignment: .leading, spacing: 4) {
                        Text("Gusan Screen").font(.headline)
                        Text(screen.isLive ? "Live. Use the Screen chip in the keyboard." : "Let Gusan see your screen to reply or explain.").font(.footnote).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Image(systemName: "chevron.right").foregroundStyle(.secondary).accessibilityHidden(true)
                }.frame(minHeight: 44)
            }
        }.onAppear { screen.refresh() }
    }
}

struct ScreenSettingsSection: View {
    @EnvironmentObject private var screen: ScreenMonitor
    var body: some View {
        Section {
            NavigationLink { ScreenView() } label: { Label("Gusan Screen", systemImage: "eye").frame(minHeight: 44) }
            Picker("Stop after", selection: Binding(get: { screen.idleMinutes }, set: { screen.setIdle($0) })) {
                ForEach(ScreenIdleTimer.minuteOptions, id: \.self) { Text("\($0) minutes").tag($0) }
            }.frame(minHeight: 44)
        } header: { Text("Screen") } footer: { Text("A picture of your screen is sent only when you ask with the Screen chip, never on its own.") }
    }
}
