import SwiftUI
import AVFoundation
import AudioToolbox
import UIKit
import UserNotifications
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

struct VoiceIntentFailure: LocalizedError { let errorDescription: String? }

@MainActor
final class VoiceSession: ObservableObject {
    /// Set by the app: spoken requests that are agent tasks ("find…", "book…") go here instead of Smart Actions.
    var agentHandoff: ((AgentIntent, String) async throws -> Void)?
    @Published private(set) var status = VoiceStatus()
    @Published private(set) var error = ""
    @Published private(set) var permission = AVAudioSession.sharedInstance().recordPermission
    @Published private(set) var requestingMicrophone = false
    @Published private(set) var starting = false
    private var microphoneRequest: Task<Bool, Never>?
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
    /// The take started by a system trigger (Control Center, Action button, Siri, Live Activity): its result goes to the clipboard.
    private var systemRequestID: UUID?
    let activity = VoiceLiveActivity()
    var alive: Bool { machine.phase != .off }

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
            start(pendingWithin: 60)
        } else {
            error = "Couldn't start — try again"
            let failure = channel.failureStatus(message: error)
            try? channel.write(status: failure); channel.post(.status)
        }
    }
    func closePresentation() { keyboardActivation = false; presented = false }
    func refreshPermission() {
        let current = AVAudioSession.sharedInstance().recordPermission
        if permission != current { permission = current }
    }
    @discardableResult
    func requestMicrophonePermission() -> Task<Bool, Never> {
        if let microphoneRequest { return microphoneRequest }
        refreshPermission()
        guard permission == .undetermined else { return Task { permission == .granted } }
        requestingMicrophone = true
        let request = Task { @MainActor in
            defer { requestingMicrophone = false; microphoneRequest = nil }
            let allowed = await withCheckedContinuation { continuation in
                if #available(iOS 17.0, *) { AVAudioApplication.requestRecordPermission { continuation.resume(returning: $0) } }
                else { AVAudioSession.sharedInstance().requestRecordPermission { continuation.resume(returning: $0) } }
            }
            refreshPermission()
            return allowed
        }
        microphoneRequest = request
        return request
    }
    func start(pendingWithin: TimeInterval = 15) {
        if machine.phase != .off { receive(); return }
        guard startup == nil else { return }
        starting = true
        error = ""
        status = VoiceStatus()
        startup = Task {
            defer { startup = nil; starting = false }
            do {
                if let error = store.sessionState(fullAccess: store.diagnostics().peer?.fullAccess).voiceError { throw error }
                guard let session = store.read() else { throw VoiceError.notSignedIn }
                let allowed = await requestMicrophonePermission().value
                refreshPermission()
                try Task.checkCancellation()
                guard allowed else { throw VoiceError.micPermissionDenied }
                try activateAudio()
                // Keep the engine running for the whole ready window: an active input keeps the app alive in the
                // background so the keyboard mic starts instantly. Samples are dropped unless a take is recording.
                try resumeCapture()
                activity.begin()
                machine.idleTimeout = settings.idleTimeout
                execute(machine.handle(.sessionStarted(Date())))
                timer = Timer.scheduledTimer(withTimeInterval: 1, repeats: true) { [weak self] _ in
                    Task { @MainActor in
                        guard let self else { return }
                        self.machine.idleTimeout = self.settings.idleTimeout
                        self.execute(self.machine.handle(.tick(Date())))
                        if self.machine.phase != .off { self.publish() }
                    }
                }
                if let command = channel.readCommand(), Date().timeIntervalSince(command.sentAt) <= pendingWithin {
                    receive(acceptPendingStart: pendingWithin > 15)
                }
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
                    self.publish(level: level)
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
    private func receive(acceptPendingStart: Bool = false) {
        if let id = channel.consumeReceipt() {
            if status.phase == .failed && status.requestID == id { error = ""; if machine.phase == .off { status = VoiceStatus() } }
            execute(machine.handle(.acknowledged(id)))
        }
        guard machine.phase != .off else { return }
        guard var command = channel.consumeCommand(), command.id != consumed else { return }
        if acceptPendingStart, command.kind == .start, (0...60).contains(Date().timeIntervalSince(command.sentAt)) {
            command.sentAt = Date()
        }
        consumed = command.id
        if command.kind == .start { error = ""; systemRequestID = nil; machine.action = command.action ?? settings.defaultAction }
        execute(machine.handle(.command(command, Date())))
        if command.kind == .cancel { channel.discardRequest(command.requestID) }
    }
    private func execute(_ effects: [VoiceMachine.Effect]) {
        for effect in effects {
            switch effect {
            case .beginCapture:
                do { if !tapped { try resumeCapture() }; AudioServicesPlaySystemSound(1113); audio.begin(); status.level = 0 }
                catch { audioFailure(error.localizedDescription); return }
            case .discardCapture: _ = audio.finish(); status.level = 0
            case .cancelWork: work?.cancel(); work = nil
            case .finishCapture(let id):
                AudioServicesPlaySystemSound(1114)
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
                // Agent only when the user picked Agent mode in the strip; a system-trigger take follows that choice.
                if id == systemRequestID, SettingsStore(group: store.resolution.chosen).keyboardMode == .agent, let agentHandoff {
                    let intent = AgentIntent.detect(transcript) ?? AgentIntent(mode: .research, instruction: transcript)
                    work = Task {
                        do {
                            try await agentHandoff(intent, context?.selected ?? "")
                            try Task.checkCancellation()
                            execute(machine.handle(.failed(id, "Agent task started. See Sessions in the Gusan app.")))
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
                        if id == systemRequestID { deliverToClipboard(result.text, id: id) }
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
    /// No text field to write into: the result goes to the clipboard and the Live Activity says "Copied".
    private func deliverToClipboard(_ text: String, id: UUID) {
        UIPasteboard.general.string = text
        // The keyboard offers "Insert" the next time a field is focused; a notification says where the text went.
        if let defaults = UserDefaults(suiteName: store.resolution.chosen) {
            VoicePendingInsert(text: text, at: Date(), account: store.read().flatMap { $0.userID ?? $0.email }).write(to: defaults)
        }
        // No result text in the notice: lock-screen previews would show it.
        let note = UNMutableNotificationContent()
        note.title = "Copied to clipboard"
        note.body = "Open a text field and tap Insert, or paste."
        let center = UNUserNotificationCenter.current()
        // Never asks for permission here (AgentCenter owns that prompt); the Live Activity already says "Copied".
        center.getNotificationSettings { settings in
            guard [.authorized, .provisional, .ephemeral].contains(settings.authorizationStatus) else { return }
            center.add(UNNotificationRequest(identifier: "gusan.voice.copied", content: note, trigger: nil))
        }
        activity.update(status, heard: nil, copied: true)
        systemRequestID = nil
        execute(machine.handle(.acknowledged(id)))
    }
    private func publish(level: Float? = nil) {
        if machine.phase != .off && !channel.available { error = channel.unavailableError.localizedDescription; stop(); return }
        let readyUntil = machine.idleTimeout.isFinite ? machine.lastActivity.addingTimeInterval(machine.idleTimeout) : nil
        let updated = VoiceStatus(phase: machine.phase, requestID: machine.requestID, sessionEndsAt: machine.phase == .recording ? machine.recordingStarted?.addingTimeInterval(machine.maxRecording) : readyUntil, level: level ?? status.level, message: machine.message, result: machine.result, replacesSelection: machine.replacesSelection, resultKind: machine.resultKind)
        var presentation = updated
        presentation.heartbeat = status.heartbeat
        if presentation != status { status = presentation }
        try? channel.write(status: updated); channel.post(.status)
        if !(status.phase == .done && status.requestID == systemRequestID) {
            let heard = [.thinking, .transcribing].contains(status.phase) ? channel.readTranscript().flatMap { $0.requestID == status.requestID ? $0.text : nil } : nil
            activity.update(updated, heard: heard)
        }
        if status.phase == .failed, let id = status.requestID, id == systemRequestID { systemRequestID = nil; execute(machine.handle(.acknowledged(id))) }
    }
    /// System triggers (StartListeningIntent and the Live Activity buttons) land here, in the app process.
    func perform(_ command: VoiceIntentCommand) async throws {
        switch command {
        case .endReady: stop()
        case .cancel:
            if let id = machine.requestID { systemRequestID = nil; execute(machine.handle(.command(VoiceCommand(kind: .cancel, requestID: id), Date()))) }
        case .stop:
            if machine.phase == .recording, let id = machine.requestID { execute(machine.handle(.command(VoiceCommand(kind: .stop, requestID: id), Date()))) }
        case .start, .toggle:
            if machine.phase == .recording {
                if command == .toggle, let id = machine.requestID { execute(machine.handle(.command(VoiceCommand(kind: .stop, requestID: id), Date()))) }
                return
            }
            guard ![.transcribing, .thinking].contains(machine.phase) else { return }
            // Apple: an AudioRecordingIntent must show a Live Activity before recording.
            activity.begin()
            if machine.phase == .off {
                start(pendingWithin: 0)
                await startup?.value
                guard machine.phase != .off else { throw VoiceIntentFailure(errorDescription: error.isEmpty ? "Gusan couldn't start listening." : error) }
            }
            let id = UUID()
            systemRequestID = id
            machine.action = settings.defaultAction
            execute(machine.handle(.command(VoiceCommand(kind: .start, requestID: id), Date())))
        }
    }
    func stop() {
        startup?.cancel(); work?.cancel(); work = nil; timer?.invalidate(); timer = nil
        engine.stop()
        if tapped { engine.inputNode.removeTap(onBus: 0); tapped = false }
        _ = audio.finish(); _ = machine.handle(.sessionStopped); systemRequestID = nil
        try? AVAudioSession.sharedInstance().setActive(false, options: .notifyOthersOnDeactivation)
        publish(level: 0)
        activity.end()
    }
    private func interrupted(_ notification: Notification) {
        guard machine.phase != .off, let value = notification.userInfo?[AVAudioSessionInterruptionTypeKey] as? UInt, let type = AVAudioSession.InterruptionType(rawValue: value) else { return }
        if type == .began { pauseCapture() }
        else {
            let options = AVAudioSession.InterruptionOptions(rawValue: notification.userInfo?[AVAudioSessionInterruptionOptionKey] as? UInt ?? 0)
            guard options.contains(.shouldResume) else { stop(); return }
            do {
                try AVAudioSession.sharedInstance().setActive(true)
                if machine.phase != .off { try resumeCapture() }
            } catch { audioFailure(error.localizedDescription) }
        }
    }
}
