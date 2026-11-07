import ReplayKit
import CoreImage
import GusanKit

/// "Gusan Screen": a ReplayKit broadcast upload extension that never uploads.
/// It keeps one downscaled copy of the latest frame in memory and hands it to
/// the keyboard only when the keyboard asks (a `ScreenRequest` on the channel).
/// Nothing is written, logged or sent continuously; the broadcast ends itself
/// after the configured idle period.
final class SampleHandler: RPBroadcastSampleHandler {
    private let queue = DispatchQueue(label: "com.aram2k.gusan.screen", qos: .userInitiated)
    private let frames = ScreenFrameStore(minimumInterval: 0.4)
    private let ciContext = CIContext(options: [.cacheIntermediates: false, .priorityRequestLow: true])
    private var channel: ScreenChannel?
    private var idle = ScreenIdleTimer(timeout: TimeInterval(ScreenIdleTimer.defaultMinutes * 60))
    private var startedAt = Date()
    private var requests = 0
    private var size = (width: 0, height: 0)
    private var heartbeat: DispatchSourceTimer?
    private var observer: DarwinObserver?
    private var paused = false
    private var finished = false

    override func broadcastStarted(withSetupInfo setupInfo: [String: NSObject]?) {
        let resolution = AppGroupResolver.resolve()
        let settings = ScreenSettings(group: resolution.chosen)
        idle = ScreenIdleTimer(timeout: TimeInterval(settings.idleMinutes * 60))
        startedAt = Date(); requests = 0; finished = false
        let channel = ScreenChannel(container: resolution.container)
        self.channel = channel
        channel.clear()
        publishStatus()
        observer = channel.observe(.request) { [weak self] in self?.queue.async { self?.answerRequest() } }
        let timer = DispatchSource.makeTimerSource(queue: queue)
        timer.schedule(deadline: .now() + 1, repeating: 1)
        timer.setEventHandler { [weak self] in self?.tick() }
        timer.resume(); heartbeat = timer
    }
    override func broadcastPaused() { queue.async { self.paused = true; self.frames.clear() } }
    override func broadcastResumed() { queue.async { self.paused = false } }
    override func broadcastFinished() { queue.sync { end() } }

    override func processSampleBuffer(_ sampleBuffer: CMSampleBuffer, with sampleBufferType: RPSampleBufferType) {
        guard sampleBufferType == .video, !paused, frames.shouldAccept(), let pixelBuffer = CMSampleBufferGetImageBuffer(sampleBuffer) else { return }
        var image = CIImage(cvPixelBuffer: pixelBuffer)
        if let raw = CMGetAttachment(sampleBuffer, key: RPVideoSampleOrientationKey as CFString, attachmentModeOut: nil) as? NSNumber,
           let orientation = CGImagePropertyOrientation(rawValue: raw.uint32Value) {
            image = image.oriented(orientation)
        }
        let extent = image.extent.integral
        let scale = ScreenFrameGeometry.scale(width: Int(extent.width), height: Int(extent.height))
        if scale < 1 { image = image.transformed(by: CGAffineTransform(scaleX: scale, y: scale)) }
        guard let cgImage = ciContext.createCGImage(image, from: image.extent.integral) else { return }
        frames.store(cgImage)
        queue.async { self.size = (cgImage.width, cgImage.height) }
    }

    private func tick() {
        guard !finished else { return }
        if idle.expired() {
            let minutes = Int(idle.timeout / 60)
            end()
            finishBroadcastWithError(NSError(domain: "com.aram2k.gusan.screen", code: 1, userInfo: [NSLocalizedDescriptionKey: "Gusan Screen stopped after \(minutes) minutes without a question. Start it again when you need it."]))
            return
        }
        answerRequest()
        publishStatus()
    }
    /// Answers a pending keyboard request with the latest frame, once.
    private func answerRequest() {
        guard !finished, let channel, let request = channel.consumeRequest() else { return }
        idle.touch(); requests += 1
        if let frame = frames.frame(for: request.id) {
            channel.write(frame: frame)
            channel.post(.frame)
        }
        publishStatus()
    }
    private func publishStatus() {
        guard let channel else { return }
        channel.write(status: ScreenStatus(startedAt: startedAt, heartbeat: Date(), idleDeadline: idle.deadline, width: size.width, height: size.height, requests: requests))
        channel.post(.status)
    }
    private func end() {
        guard !finished else { return }
        finished = true
        heartbeat?.cancel(); heartbeat = nil; observer = nil
        frames.clear()
        channel?.clear()
        channel?.post(.status)
    }
}
