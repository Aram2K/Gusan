import SwiftUI
import AVFoundation
import GusanKit

private final class VoiceAudioBuffer {
    private let lock = NSLock()
    private var samples: [Int16] = []
    private var recording = false
    private var lastLevel = Date.distantPast
    func begin() { lock.lock(); defer { lock.unlock() }; samples = []; recording = true }
    func finish() -> [Int16] { lock.lock(); defer { lock.unlock() }; recording = false; let result = samples; samples = []; return result }
    func append(_ buffer: AVAudioPCMBuffer) -> Float? {
        lock.lock(); defer { lock.unlock() }
        guard recording, let pointer = buffer.int16ChannelData?[0] else { return nil }
        let count = Int(buffer.frameLength)
        let chunk = UnsafeBufferPointer(start: pointer, count: count)
        if samples.count < 16000 * 300 { samples.append(contentsOf: chunk.prefix(16000 * 300 - samples.count)) }
        guard Date().timeIntervalSince(lastLevel) >= 1.0 / 15 else { return nil }
        lastLevel = Date()
        let energy = chunk.reduce(0.0) { $0 + pow(Double($1) / 32768, 2) }
        return Float(min(1, sqrt(energy / Double(max(1, count))) * 5))
    }
}

@MainActor
final class VoiceSession: ObservableObject {
    @Published private(set) var status = VoiceStatus()
    @Published private(set) var error = ""
    @Published private(set) var permission = AVAudioSession.sharedInstance().recordPermission
    @Published var presented = false
    let store: SessionStore
    let settings: VoiceSettings
    private let auth: AuthClient
    private let channel: VoiceChannel
    private var machine = VoiceMachine()
    private var engine = AVAudioEngine()
    private let audio = VoiceAudioBuffer()
    private var observer: DarwinObserver?
    private var audioObservers: [NSObjectProtocol] = []
    private var timer: Timer?
    private var work: Task<Void, Never>?
    private var startup: Task<Void, Never>?
    private var consumed: UUID?
    private var tapped = false
    private var hostBundleID: String?
    var alive: Bool { status.isAlive() }

    init(store: SessionStore = SessionStore()) {
        self.store = store; auth = AuthClient(store: store)
        settings = VoiceSettings(group: store.resolution.chosen)
        channel = VoiceChannel(container: store.resolution.container, group: store.resolution.chosen ?? "group.com.aram2k.gusan")
        observer = channel.observe(.command) { [weak self] in self?.receive() }
        audioObservers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.interruptionNotification, object: nil, queue: .main) { [weak self] notification in
            Task { @MainActor in self?.interrupted(notification) }
        })
        audioObservers.append(NotificationCenter.default.addObserver(forName: AVAudioSession.mediaServicesWereResetNotification, object: nil, queue: .main) { [weak self] _ in
            Task { @MainActor in self?.error = VoiceError.sessionEnded.localizedDescription; self?.stop(); self?.engine = AVAudioEngine() }
        })
    }
    func handleURL(_ url: URL) {
        guard url.scheme == "gusan", url.host == "voice" else { return }
        hostBundleID = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems?.first { $0.name == "return" }?.value
        presented = true; start()
    }
    func refreshPermission() { permission = AVAudioSession.sharedInstance().recordPermission }
    func start(pendingWithin: TimeInterval = 15) {
        if machine.phase != .off { receive(); scheduleReturn(); return }
        guard startup == nil else { return }
        error = ""
        startup = Task {
            defer { startup = nil }
            do {
                guard channel.available else { throw VoiceError.notShared }
                guard store.read() != nil else { throw VoiceError.notSignedIn }
                let allowed = await withCheckedContinuation { continuation in
                    if #available(iOS 17.0, *) { AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) } }
                    else { AVAudioSession.sharedInstance().requestRecordPermission { continuation.resume(returning: $0) } }
                }
                refreshPermission()
                try Task.checkCancellation()
                guard allowed else { throw VoiceError.micPermissionDenied }
                try activateAudio()
                machine.idleTimeout = TimeInterval(settings.idleMinutes * 60)
                execute(machine.handle(.sessionStarted(Date())))
                timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                    Task { @MainActor in
                        guard let self else { return }
                        self.machine.idleTimeout = TimeInterval(self.settings.idleMinutes * 60)
                        self.execute(self.machine.handle(.tick(Date())))
                        if self.machine.phase != .off { self.publish() }
                    }
                }
                if let command = channel.readCommand(), Date().timeIntervalSince(command.sentAt) <= pendingWithin { receive() }
                scheduleReturn()
            } catch is CancellationError {} catch {
                self.error = error.localizedDescription
                stop()
                status = VoiceStatus(phase: .failed, requestID: channel.readCommand()?.requestID, heartbeat: .distantPast, message: self.error)
                try? channel.write(status: status); channel.post(.status)
            }
        }
    }
    private func activateAudio() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, options: [.mixWithOthers, .allowBluetoothHFP, .defaultToSpeaker])
        try session.setActive(true)
        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0, let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: 16000, channels: 1, interleaved: false), let converter = AVAudioConverter(from: format, to: target) else { throw VoiceError.audioFailed("Microphone unavailable.") }
        input.installTap(onBus: 0, bufferSize: 1024, format: format) { [weak self, audio] buffer, _ in
            let capacity = AVAudioFrameCount(ceil(Double(buffer.frameLength) * 16000 / format.sampleRate) + 32)
            guard let output = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
            var supplied = false
            var failure: NSError?
            let result = converter.convert(to: output, error: &failure) { _, state in
                if supplied { state.pointee = .noDataNow; return nil }
                supplied = true; state.pointee = .haveData; return buffer
            }
            guard result != .error, failure == nil else {
                Task { @MainActor in self?.audioFailure(failure?.localizedDescription ?? "Conversion failed.") }; return
            }
            if let level = audio.append(output) {
                Task { @MainActor in
                    guard let self, self.machine.phase == .recording else { return }
                    self.status.level = level; self.publish()
                }
            }
        }
        tapped = true
        engine.prepare(); try engine.start()
    }
    private func audioFailure(_ message: String) {
        let requestID = machine.requestID
        error = VoiceError.audioFailed(message).localizedDescription; stop()
        status = VoiceStatus(phase: .failed, requestID: requestID, heartbeat: .distantPast, message: error)
        try? channel.write(status: status); channel.post(.status)
    }
    private func receive() {
        guard machine.phase != .off, let command = channel.readCommand(), command.id != consumed else { return }
        consumed = command.id
        if command.kind == .start { machine.action = settings.defaultAction }
        execute(machine.handle(.command(command, Date())))
    }
    private func execute(_ effects: [VoiceMachine.Effect]) {
        for effect in effects {
            switch effect {
            case .beginCapture: audio.begin(); status.level = 0
            case .discardCapture: _ = audio.finish(); status.level = 0
            case .cancelWork: work?.cancel(); work = nil
            case .finishCapture(let id):
                let wav = WAV.encode(samples: audio.finish()); status.level = 0
                work = Task {
                    do {
                        let text = try await authorized { token in try await TranscribeClient().run(wav: wav, language: self.settings.language, accessToken: token) }
                        try Task.checkCancellation()
                        execute(machine.handle(.transcribed(id, text)))
                    } catch is CancellationError {} catch { execute(machine.handle(.failed(id, error.localizedDescription))) }
                }
            case .runAction(let id, let transcript, let context):
                let plan = VoicePlan.plan(transcript: transcript, context: context, action: machine.action)
                work = Task {
                    do {
                        let result = try await authorized { token in
                            switch plan {
                            case .insert(let text): return text
                            case .smart(let mode, let text): return try await SmartActionClient().run(text: text, mode: mode, accessToken: token, target: SettingsStore(group: self.store.resolution.chosen).translateTarget)
                            case .instruction(let instruction, let text): return try await SmartActionClient().run(instruction: instruction, text: text, accessToken: token)
                            }
                        }
                        try Task.checkCancellation()
                        guard !result.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw VoiceError.invalidResponse }
                        execute(machine.handle(.finished(id, result, !(context?.selected.isEmpty ?? true))))
                    } catch is CancellationError {} catch { execute(machine.handle(.failed(id, error.localizedDescription))) }
                }
            case .deliver: break
            case .stopSession: stop()
            case .publish: publish()
            }
        }
    }
    private func authorized(_ operation: (String) async throws -> String) async throws -> String {
        guard let session = try await auth.refresh() else { throw VoiceError.notSignedIn }
        do { return try await operation(session.accessToken) }
        catch where (error as? VoiceError) == .unauthorized || (error as? GusanError) == .unauthorized {
            guard let refreshed = try await auth.refresh(force: true) else { throw VoiceError.notSignedIn }
            return try await operation(refreshed.accessToken)
        }
    }
    private func publish() {
        status = VoiceStatus(phase: machine.phase, requestID: machine.requestID, sessionEndsAt: machine.phase == .recording ? machine.recordingStarted?.addingTimeInterval(machine.maxRecording) : machine.lastActivity.addingTimeInterval(machine.idleTimeout), level: status.level, message: machine.message, result: machine.result, replacesSelection: machine.replacesSelection)
        try? channel.write(status: status); channel.post(.status)
    }
    func stop() {
        startup?.cancel(); work?.cancel(); work = nil; timer?.invalidate(); timer = nil
        engine.stop()
        if tapped { engine.inputNode.removeTap(onBus: 0); tapped = false }
        _ = audio.finish(); _ = machine.handle(.sessionStopped)
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        status.level = 0; publish()
    }
    private func interrupted(_ notification: Notification) {
        guard machine.phase != .off, let value = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt, let type = AVAudioSession.InterruptionType(rawValue: value) else { return }
        if type == .began { engine.pause() }
        else {
            let options = AVAudioSession.InterruptionOptions(rawValue: notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            guard options.contains(.shouldResume) else { stop(); return }
            do { try AVAudioSession.sharedInstance().setActive(true); try engine.start() } catch { audioFailure(error.localizedDescription) }
        }
    }
    private func scheduleReturn() {
        guard settings.autoReturn, let bundleID = hostBundleID, !bundleID.isEmpty else { return }
        hostBundleID = nil
        Task { try? await Task.sleep(nanoseconds: 500_000_000); if alive { returnToHost(bundleID: bundleID) } }
    }
    func returnToHost(bundleID: String) {
        guard settings.autoReturn, let cls = NSClassFromString("LSApplicationWorkspace") as? NSObject.Type else { return }
        let selector = NSSelectorFromString("defaultWorkspace")
        guard cls.responds(to: selector), let workspace = cls.perform(selector)?.takeUnretainedValue() as? NSObject else { return }
        let open = NSSelectorFromString("openApplicationWithBundleID:")
        guard workspace.responds(to: open) else { return }
        typealias Open = @convention(c) (AnyObject, Selector, NSString) -> Bool
        let invoke = unsafeBitCast(workspace.method(for: open), to: Open.self)
        _ = invoke(workspace, open, bundleID as NSString)
    }
}
