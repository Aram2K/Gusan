import ReplayKit
import CoreImage
import CoreImage.CIFilterBuiltins
import os
import GusanKit

/// "Gusan Screen": a ReplayKit broadcast upload extension that never uploads.
/// It keeps a second or two of downscaled frames in memory and hands one to
/// the keyboard only when the keyboard asks (a `ScreenRequest` on the channel).
/// While the Gusan keyboard is up it freezes the last frame from before the
/// keyboard appeared, so the keyboard itself is never in the picture.
/// Nothing is written, logged or sent continuously; the broadcast ends itself
/// after the configured idle period.
final class SampleHandler: RPBroadcastSampleHandler {
    /// Frames are accepted at the fast rate for this long after the keyboard or a request shows up, then once a second.
    static let fastWindow: TimeInterval = 10
    /// A request that arrives between frames (right after a pause, say) waits this long for the next one.
    static let pendingWindow: TimeInterval = 4
    private let queue = DispatchQueue(label: "com.aram2k.gusan.screen", qos: .userInitiated)
    private let frames = ScreenFrameStore(fastInterval: 0.4, idleInterval: 1.0, capacity: 4)
    private let ciContext = CIContext(options: [.cacheIntermediates: false, .priorityRequestLow: true])
    private let lanczos = CIFilter.lanczosScaleTransform()
    private var channel: ScreenChannel?
    private var settings: ScreenSettings?
    private var idle = ScreenIdleTimer(timeout: TimeInterval(ScreenIdleTimer.defaultMinutes * 60))
    private var startedAt = Date()
    private var requests = 0
    private var size = (width: 0, height: 0)
    private var heartbeat: DispatchSourceTimer?
    private var requestObserver: DarwinObserver?
    private var keyboardObserver: DarwinObserver?
    private var pending: ScreenRequest?
    private var paused = false
    private let endLock = NSLock()
    private var finished = false
    private let log = Logger(subsystem: "com.aram2k.gusan.screen", category: "broadcast")

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        let resolution = AppGroupResolver.resolve()
        let settings = ScreenSettings(group: resolution.chosen)
        self.settings = settings
        idle = ScreenIdleTimer(timeout: TimeInterval(settings.idleMinutes * 60))
        startedAt = Date(); requests = 0; pending = nil
        endLock.lock(); finished = false; endLock.unlock()
        let keyboardSettings = SettingsStore(group: resolution.chosen)
        let channel = ScreenChannel(container: resolution.container, pasteboardAllowed: { keyboardSettings.refresh(); return keyboardSettings.pasteboardFallback })
        self.channel = channel
        channel.clear()
        settings.clearIdleStop()
        frames.boost(until: Date().addingTimeInterval(Self.fastWindow))
        logMemory("start")
        publishStatus()
        requestObserver = channel.observe(.request) { [weak self] in self?.queue.async { self?.answerRequest() } }
        keyboardObserver = channel.observe(.keyboard) { [weak self] in self?.queue.async { self?.keyboardChanged() } }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + ScreenStatus.heartbeatInterval, repeating: ScreenStatus.heartbeatInterval)
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume(); heartbeat = timer
    }
    override func broadcastPaused() { queue.async { self.paused = true; self.frames.clear() } }
    override func broadcastResumed() { queue.async { self.paused = false; self.frames.boost(until: Date().addingTimeInterval(Self.fastWindow)) } }
    /// May be called on any thread, including re-entrantly from `finishBroadcastWithError`: `end()` is idempotent and takes no queue.
    override func broadcastFinished() { end() }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        guard sampleBufferType == .video, !paused, frames.shouldAccept(), let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        var image = CIImage(cvPixelBuffer: pixelBuffer)
        if let raw = CMGetAttachment(sampleBuffer, key: RPVideoSampleOrientationKey as CFString, attachmentModeOut: nil) as? NSNumber,
           let orientation = CGImagePropertyOrientation(rawValue: raw.uint32Value) {
            image = image.oriented(orientation)
        }
        let extent = image.extent.integral
        let scale = ScreenFrameGeometry.scale(width: Int(extent.width), height: Int(extent.height))
        if scale < 1 {
            lanczos.inputImage = image; lanczos.scale = Float(scale); lanczos.aspectRatio = 1
            guard let scaled = lanczos.outputImage else { return }
            image = scaled
        }
        guard let cgImage = ciContext.createCGImage(image, from: image.extent.integral) else { return }
        frames.store(cgImage)
        queue.async {
            self.size = (cgImage.width, cgImage.height)
            if self.pending != nil { self.answerPending() }
        }
    }

    private func tick() {
        guard !isFinished else { return }
        if idle.expired() {
            let minutes = Int(idle.timeout / 60)
            settings?.recordIdleStop(minutes: minutes)
            end()
            finishBroadcastWithError(NSError(domain: "com.aram2k.gusan.screen", code: 1, userInfo: [NSLocalizedDescriptionKey: "Gusan Screen stopped: \(minutes) minutes without a question."]))
            return
        }
        if let pending, Date().timeIntervalSince(pending.issuedAt) > Self.pendingWindow { self.pending = nil }
        keyboardChanged()
        answerRequest()
        publishStatus()
    }
    /// The keyboard came up or went away: freeze the last pre-keyboard frame, or let go of it.
    private func keyboardChanged() {
        guard !isFinished, let channel else { return }
        if let keyboard = channel.readKeyboard() {
            if frames.frozen == nil { frames.boost(until: Date().addingTimeInterval(Self.fastWindow)) }
            frames.freeze(before: keyboard.shownAt)
        } else {
            frames.unfreeze()
        }
    }
    /// Answers a pending keyboard request with the right frame, once. A request
    /// that finds no frame (right after a pause) is kept until the next frame arrives.
    private func answerRequest() {
        guard !isFinished, let channel, let request = channel.consumeRequest() else { return }
        idle.touch(); requests += 1
        frames.boost(until: Date().addingTimeInterval(Self.fastWindow))
        logMemory("request")
        pending = request
        answerPending()
        publishStatus()
    }
    private func answerPending() {
        guard let channel, let request = pending else { return }
        guard let frame = frames.frame(for: request.id, keyboard: channel.readKeyboard()) else { return }
        pending = nil
        channel.write(frame: frame)
        channel.post(.frame)
    }
    private func publishStatus() {
        guard let channel else { return }
        channel.write(status: ScreenStatus(startedAt: startedAt, heartbeat: Date(), idleDeadline: idle.deadline, width: size.width, height: size.height, requests: requests))
        channel.post(.status)
    }
    private var isFinished: Bool { endLock.lock(); defer { endLock.unlock() }; return finished }
    private func end() {
        endLock.lock()
        guard !finished else { endLock.unlock(); return }
        finished = true
        endLock.unlock()
        heartbeat?.cancel(); heartbeat = nil; requestObserver = nil; keyboardObserver = nil
        frames.clear()
        channel?.clear()
        channel?.post(.status)
    }
    /// Debug builds only: how much memory the extension still has (its limit is about 50 MB).
    private func logMemory(_ moment: String) {
        #if DEBUG
        let megabytes = Double(os_proc_available_memory()) / 1_048_576
        log.debug("Gusan Screen \(moment, privacy: .public): \(megabytes, format: .fixed(precision: 1), privacy: .public) MB available")
        #endif
    }
}
