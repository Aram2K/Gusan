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
        guard Date().timeIntervalSince(lastLevel) >= 1.0 / 4 else { return nil }
        lastLevel = Date()
        let energy = chunk.reduce(0.0) { $0 + pow(Double($1) / 32768, 2) }
        return Float(min(1, sqrt(energy / Double(max(1, count))) * 5))
    }
}

@MainActor
final class VoiceSession: ObservableObject {
    /// Set by the app: spoken requests that are agent tasks ("find…", "book…") go here instead of Smart Actions.
    var agentHandoff: ((AgentIntent, String) async throws -> Void)?
    @Published private(set) var status = VoiceStatus()
    @Published private(set) var error = ""
    @Published private(set) var permission = AVAudioSession.sharedInstance().recordPermission
    @Published var presented = false
    @Published private(set) var keyboardActivation = false
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
        channel = store.makeVoiceChannel()
        #if DEBUG
        if ProcessInfo.processInfo.arguments.contains("--gusan-debug-activation-guide") { keyboardActivation = true; presented = true }
        #endif
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
        let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems
        presented = true
        if machine.phase == .off { error = ""; status = VoiceStatus() }
        if channel.consumeLaunch(nonce: items?.first { $0.name == "nonce" }?.value) {
            keyboardActivation = true
            hostBundleID = items?.first { $0.name == "return" }?.value
            start(pendingWithin: 60)
        } else { hostBundleID = nil }
    }
    func refreshPermission() { permission = AVAudioSession.sharedInstance().recordPermission }
    func start(pendingWithin: TimeInterval = 15) {
        if machine.phase != .off { receive(); scheduleReturn(); return }
        guard startup == nil else { return }
        error = ""
        status = VoiceStatus()
        startup = Task {
            defer { startup = nil }
            do {
                if let error = store.sessionState(fullAccess: store.diagnostics().peer?.fullAccess).voiceError { throw error }
                guard let session = store.read() else { throw VoiceError.notSignedIn }
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
                let failure = channel.failureStatus(message: self.error)
                stop()
                status = failure
                try? channel.write(status: status); channel.post(.status)
            }
        }
    }
    private func activateAudio() throws {
        let session = AVAudioSession.sharedInstance()
        try session.setCategory(.playAndRecord, options: [.mixWithOthers, .allowBluetoothHFP, .defaultToSpeaker])
        try session.setActive(true)
    }
    private func resumeCapture() throws {
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
    private func pauseCapture() {
        engine.pause()
        if tapped { engine.inputNode.removeTap(onBus: 0); tapped = false }
    }
    private func audioFailure(_ message: String) {
        let requestID = machine.requestID
        error = VoiceError.audioFailed(message).localizedDescription; stop()
        status = VoiceStatus(phase: .failed, requestID: requestID, heartbeat: .distantPast, message: error)
        try? channel.write(status: status); channel.post(.status)
    }
    private func receive() {
        if let id = channel.consumeReceipt() {
            if status.phase == .failed && status.requestID == id { error = ""; if machine.phase == .off { status = VoiceStatus() } }
            execute(machine.handle(.acknowledged(id)))
        }
        guard machine.phase != .off else { return }
        guard let command = channel.consumeCommand(), command.id != consumed else { return }
        consumed = command.id
        if command.kind == .start { error = ""; machine.action = settings.defaultAction }
        execute(machine.handle(.command(command, Date())))
        if command.kind == .cancel { channel.discardRequest(command.requestID) }
    }
    private func execute(_ effects: [VoiceMachine.Effect]) {
        for effect in effects {
            switch effect {
            case .beginCapture:
                do { try resumeCapture(); audio.begin(); status.level = 0 }
                catch { audioFailure(error.localizedDescription); return }
            case .discardCapture: pauseCapture(); _ = audio.finish(); status.level = 0
            case .cancelWork: work?.cancel(); work = nil
            case .finishCapture(let id):
                pauseCapture()
                let wav = WAV.encode(samples: audio.finish()); status.level = 0
                work = Task {
                    do {
                        let text = try await authorized { token in try await TranscribeClient().run(wav: wav, language: self.settings.language, accessToken: token) }
                        try Task.checkCancellation()
                        try channel.write(transcript: VoiceTranscript(requestID: id, text: text))
                        execute(machine.handle(.transcribed(id, text)))
                    } catch is CancellationError {} catch { execute(machine.handle(.failed(id, error.localizedDescription))) }
                }
            case .runAction(let id, let transcript, let context):
                if machine.action == .automatic, let intent = AgentIntent.detect(transcript), let agentHandoff {
                    work = Task {
                        do {
                            try await agentHandoff(intent, context?.selected ?? "")
                            try Task.checkCancellation()
                            execute(machine.handle(.failed(id, "Agent task started. See Activity in the Gusan app.")))
                        } catch is CancellationError {} catch { execute(machine.handle(.failed(id, (error as? AgentError)?.errorDescription ?? error.localizedDescription))) }
                    }
                    continue
                }
                work = Task {
                    do {
                        let result = try await authorized { token in
                            try await VoiceJob(transcript: transcript, context: context, action: self.machine.action, accessToken: token, target: SettingsStore(group: self.store.resolution.chosen).translateTarget).result()
                        }
                        try Task.checkCancellation()
                        guard !result.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { throw VoiceError.invalidResponse }
                        if self.machine.action != .transcribeOnly {
                            SmartHistory(group: self.store.resolution.chosen).append(instruction: transcript, result: result.text)
                        }
                        execute(machine.handle(.completed(id, result)))
                    } catch is CancellationError {} catch { execute(machine.handle(.failed(id, error.localizedDescription))) }
                }
            case .deliver: break
            case .stopSession: stop()
            case .publish: publish()
            }
        }
    }
    private func authorized<T>(_ operation: (String) async throws -> T) async throws -> T {
        guard let session = try await auth.refresh() else { throw VoiceError.notSignedIn }
        do { return try await operation(session.accessToken) }
        catch where (error as? VoiceError) == .unauthorized || (error as? GusanError) == .unauthorized {
            guard let refreshed = try await auth.refresh(force: true) else { throw VoiceError.notSignedIn }
            return try await operation(refreshed.accessToken)
        }
    }
    private func publish() {
        if machine.phase != .off && !channel.available { error = channel.unavailableError.localizedDescription; stop(); return }
        status = VoiceStatus(phase: machine.phase, requestID: machine.requestID, sessionEndsAt: machine.phase == .recording ? machine.recordingStarted?.addingTimeInterval(machine.maxRecording) : machine.lastActivity.addingTimeInterval(machine.idleTimeout), level: status.level, message: machine.message, result: machine.result, replacesSelection: machine.replacesSelection, resultKind: machine.resultKind)
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
        if type == .began { pauseCapture() }
        else {
            let options = AVAudioSession.InterruptionOptions(rawValue: notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            guard options.contains(.shouldResume) else { stop(); return }
            do {
                try AVAudioSession.sharedInstance().setActive(true)
                if machine.phase == .recording { try resumeCapture() }
            } catch { audioFailure(error.localizedDescription) }
        }
    }
    private func scheduleReturn() {
        guard settings.autoReturn, let bundleID = hostBundleID, !bundleID.isEmpty else { return }
        hostBundleID = nil
        Task { try? await Task.sleep(nanoseconds: 500_000_000); if alive { returnToHost(bundleID: bundleID) } }
    }
    func returnToHost(bundleID: String) {
        #if GUSAN_SIDELOAD
        guard settings.autoReturn, let cls = NSClassFromString("LSApplicationWorkspace") as? NSObject.Type else { return }
        let selector = NSSelectorFromString("defaultWorkspace")
        guard cls.responds(to: selector), let workspace = cls.perform(selector)?.takeUnretainedValue() as? NSObject else { return }
        let open = NSSelectorFromString("openApplicationWithBundleID:")
        guard workspace.responds(to: open) else { return }
        typealias Open = @convention(c) (AnyObject, Selector, NSString) -> Bool
        let invoke = unsafeBitCast(workspace.method(for: open), to: Open.self)
        _ = invoke(workspace, open, bundleID as NSString)
        #endif
    }
}
