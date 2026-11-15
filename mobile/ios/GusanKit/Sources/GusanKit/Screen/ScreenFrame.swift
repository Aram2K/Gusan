import Foundation
import CoreGraphics
import ImageIO
#if canImport(UniformTypeIdentifiers)
import UniformTypeIdentifiers
#endif

/// One downscaled screen picture, handed from the broadcast extension to the
/// keyboard only when the user asks for something.
public struct ScreenFrame: Codable, Equatable {
    public let requestID: UUID
    public let capturedAt: Date
    public let width: Int
    public let height: Int
    public let jpeg: Data
    public var phase: ScreenRequest.Phase?
    /// Captured just before the Gusan keyboard came up, so the keyboard is not in the picture.
    public var frozen: Bool
    /// The keyboard's band was cut off the bottom because no pre-keyboard picture existed.
    public var cropped: Bool
    public init(requestID: UUID, capturedAt: Date, width: Int, height: Int, jpeg: Data, frozen: Bool = false, cropped: Bool = false, phase: ScreenRequest.Phase? = nil) {
        self.requestID = requestID; self.capturedAt = capturedAt; self.width = width; self.height = height; self.jpeg = jpeg; self.frozen = frozen; self.cropped = cropped
        self.phase = phase
    }
    public var dataURL: String { "data:image/jpeg;base64," + jpeg.base64EncodedString() }
    /// How long a picture may be trusted to show what the user is asking about.
    public static let maxAge: TimeInterval = 5
    /// A frozen picture is judged against the moment the keyboard appeared (it
    /// shows what was behind the keyboard since); a live one against now.
    public func isFresh(now: Date = Date(), keyboardShownAt: Date? = nil, maxAge: TimeInterval = ScreenFrame.maxAge) -> Bool {
        let reference = frozen ? (keyboardShownAt ?? now) : now
        let age = reference.timeIntervalSince(capturedAt)
        return age <= maxAge && age > -60
    }
}

public enum ScreenFrameGeometry {
    /// The broadcast extension keeps about 50 MB; a 1280-pixel long edge reads
    /// phone text well and stays near 3 MB as a bitmap.
    public static let defaultLongEdge = 1280
    /// Never cut more than this off the bottom; the picture must stay useful.
    public static let maxBottomCrop = 0.7
    public static func scaledSize(width: Int, height: Int, longEdge: Int = defaultLongEdge) -> (width: Int, height: Int) {
        guard width > 0, height > 0 else { return (0, 0) }
        let longest = max(width, height)
        guard longest > longEdge else { return (width, height) }
        let scale = Double(longEdge) / Double(longest)
        return (max(1, Int((Double(width) * scale).rounded())), max(1, Int((Double(height) * scale).rounded())))
    }
    public static func scale(width: Int, height: Int, longEdge: Int = defaultLongEdge) -> Double {
        let size = scaledSize(width: width, height: height, longEdge: longEdge)
        return width > 0 ? Double(size.width) / Double(width) : 1
    }
    /// The part of a picture left when the bottom `fraction` (the keyboard's band) is cut off.
    public static func cropRect(width: Int, height: Int, bottomFraction: Double) -> CGRect {
        let fraction = min(max(bottomFraction, 0), maxBottomCrop)
        let kept = max(1, Int((Double(height) * (1 - fraction)).rounded()))
        return CGRect(x: 0, y: 0, width: width, height: kept)
    }
}

/// Where the Gusan keyboard is on screen, so its band can be cropped out.
public struct ScreenKeyboardState: Codable, Equatable {
    public let shownAt: Date
    public let keyboardHeight: Double
    public let screenHeight: Double
    public init(shownAt: Date = Date(), keyboardHeight: Double, screenHeight: Double) {
        self.shownAt = shownAt; self.keyboardHeight = keyboardHeight; self.screenHeight = screenHeight
    }
    public var bottomFraction: Double { screenHeight > 0 ? min(ScreenFrameGeometry.maxBottomCrop, max(0, keyboardHeight / screenHeight)) : 0 }
}

/// Keeps bounded recent pictures and rate-limits scaling of broadcast frames.
public final class ScreenFrameStore {
    public struct Snapshot: Equatable {
        public let jpeg: Data
        public let capturedAt: Date
        public var image: CGImage? {
            guard let source = CGImageSourceCreateWithData(jpeg as CFData, nil) else { return nil }
            return CGImageSourceCreateImageAtIndex(source, 0, nil)
        }
    }
    public let fastInterval: TimeInterval
    public let idleInterval: TimeInterval
    public let capacity: Int
    private var recent: [Snapshot] = []
    private var lastAccepted: Date?
    private var fastUntil = Date.distantPast
    private var count = 0
    private let lock = NSLock()
    public init(fastInterval: TimeInterval = 0.4, idleInterval: TimeInterval = 1.0, capacity: Int = 4) {
        self.fastInterval = fastInterval; self.idleInterval = idleInterval; self.capacity = max(1, capacity)
    }
    public convenience init(minimumInterval: TimeInterval) { self.init(fastInterval: minimumInterval, idleInterval: minimumInterval) }
    public var latest: CGImage? { lock.lock(); defer { lock.unlock() }; return recent.first?.image }
    public var capturedAt: Date? { lock.lock(); defer { lock.unlock() }; return recent.first?.capturedAt }
    public var accepted: Int { lock.lock(); defer { lock.unlock() }; return count }
    /// The interval in force at `now`: fast right after the keyboard or a request showed up, idle otherwise.
    public func minimumInterval(at now: Date = Date()) -> TimeInterval { lock.lock(); defer { lock.unlock() }; return now < fastUntil ? fastInterval : idleInterval }
    /// Accept frames at the fast rate until `until`.
    public func boost(until: Date) { lock.lock(); defer { lock.unlock() }; fastUntil = max(fastUntil, until) }
    /// True when a frame arriving at `now` should be processed and kept.
    public func shouldAccept(at now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let lastAccepted else { return true }
        return now.timeIntervalSince(lastAccepted) >= (now < fastUntil ? fastInterval : idleInterval)
    }
    public func store(_ image: CGImage, at now: Date = Date()) {
        guard let jpeg = ScreenFrameEncoder.jpeg(image, budgetBytes: ScreenFrameEncoder.defaultBudget),
              jpeg.count <= ScreenFrameEncoder.defaultBudget else { return }
        lock.lock(); defer { lock.unlock() }
        recent.insert(Snapshot(jpeg: jpeg, capturedAt: now), at: 0)
        if recent.count > capacity { recent.removeLast(recent.count - capacity) }
        lastAccepted = now; count += 1
    }
    /// Drops every kept picture (pause, end).
    public func clear() { lock.lock(); defer { lock.unlock() }; recent = []; lastAccepted = nil }
    /// The newest picture captured at or before `instant`, or nil when nothing that old is left.
    public func snapshot(before instant: Date) -> Snapshot? { lock.lock(); defer { lock.unlock() }; return recent.first { $0.capturedAt <= instant } }
    /// The latest picture as a frame for `requestID`, or nil when nothing was captured yet.
    public func frame(for requestID: UUID, budgetBytes: Int = ScreenFrameEncoder.defaultBudget) -> ScreenFrame? {
        frame(for: requestID, keyboard: nil, budgetBytes: budgetBytes)
    }
    /// The newest picture, with the keyboard's band cropped off.
    public func frame(for requestID: UUID, keyboard: ScreenKeyboardState?, budgetBytes: Int = ScreenFrameEncoder.defaultBudget) -> ScreenFrame? {
        lock.lock(); let newest = recent.first; lock.unlock()
        guard let newest else { return nil }
        return Self.encode(newest, requestID: requestID, frozen: false, cropBottom: keyboard?.bottomFraction ?? 0, budgetBytes: budgetBytes)
    }
    public func frame(for request: ScreenRequest, keyboard: ScreenKeyboardState?, allowFallback: Bool = false, budgetBytes: Int = ScreenFrameEncoder.defaultBudget) -> ScreenFrame? {
        lock.lock()
        let match = recent.filter { $0.capturedAt >= request.targetAt }.min { $0.capturedAt < $1.capturedAt }
            ?? (allowFallback ? recent.first : nil)
        lock.unlock()
        guard let match else { return nil }
        var frame = Self.encode(match, requestID: request.id, frozen: false, cropBottom: keyboard?.bottomFraction ?? 0, budgetBytes: budgetBytes)
        frame?.phase = request.phase
        return frame
    }
    static func encode(_ snapshot: Snapshot, requestID: UUID, frozen: Bool, cropBottom: Double, budgetBytes: Int) -> ScreenFrame? {
        guard var image = snapshot.image else { return nil }
        var cropped = false
        if cropBottom > 0 {
            let rect = ScreenFrameGeometry.cropRect(width: image.width, height: image.height, bottomFraction: cropBottom)
            if rect.height < CGFloat(image.height), let cut = image.cropping(to: rect) { image = cut; cropped = true }
        }
        guard let encoded = ScreenFrameEncoder.encodedJPEG(image, budgetBytes: budgetBytes) else { return nil }
        return ScreenFrame(requestID: requestID, capturedAt: snapshot.capturedAt, width: encoded.width, height: encoded.height, jpeg: encoded.data, frozen: frozen, cropped: cropped)
    }
}

public enum ScreenFrameEncoder {
    /// Smart-action bodies may be 8 MB; one frame stays far below that, also on a named pasteboard.
    public static let defaultBudget = 450_000
    public static let minimumLongEdge = 480
    public static let qualities: [Double] = [0.75, 0.6, 0.45, 0.3]
    public static func jpeg(_ image: CGImage, quality: Double) -> Data? {
        let data = NSMutableData()
        #if canImport(UniformTypeIdentifiers)
        let type = UTType.jpeg.identifier as CFString
        #else
        let type = "public.jpeg" as CFString
        #endif
        guard let destination = CGImageDestinationCreateWithData(data, type, 1, nil) else { return nil }
        CGImageDestinationAddImage(destination, image, [kCGImageDestinationLossyCompressionQuality: quality] as CFDictionary)
        guard CGImageDestinationFinalize(destination) else { return nil }
        return data as Data
    }
    /// Lowers quality first, then resolution, until the JPEG fits the budget.
    public static func jpeg(_ image: CGImage, budgetBytes: Int = defaultBudget) -> Data? {
        encodedJPEG(image, budgetBytes: budgetBytes)?.data
    }
    static func encodedJPEG(_ image: CGImage, budgetBytes: Int) -> (data: Data, width: Int, height: Int)? {
        guard budgetBytes > 0 else { return nil }
        var image = image
        while true {
            for quality in qualities {
                if let data = jpeg(image, quality: quality), data.count <= budgetBytes {
                    return (data, image.width, image.height)
                }
            }
            let longEdge = max(image.width, image.height)
            guard longEdge > minimumLongEdge else { return nil }
            let nextEdge = max(minimumLongEdge, Int(Double(longEdge) * 0.75))
            let size = ScreenFrameGeometry.scaledSize(width: image.width, height: image.height, longEdge: nextEdge)
            guard let context = CGContext(data: nil, width: size.width, height: size.height, bitsPerComponent: 8, bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(), bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else { return nil }
            context.interpolationQuality = .high
            context.draw(image, in: CGRect(x: 0, y: 0, width: size.width, height: size.height))
            guard let scaled = context.makeImage() else { return nil }
            image = scaled
        }
    }
}

/// When the broadcast ends on its own: after `timeout` without a frame request.
public struct ScreenIdleTimer: Equatable {
    public static let defaultMinutes = 10
    public static let minuteOptions = [2, 5, 10, 20, 30]
    public let timeout: TimeInterval
    public private(set) var lastActivity: Date
    public init(timeout: TimeInterval, startedAt: Date = Date()) { self.timeout = timeout; lastActivity = startedAt }
    public var deadline: Date { lastActivity.addingTimeInterval(timeout) }
    public mutating func touch(at now: Date = Date()) { lastActivity = max(lastActivity, now) }
    public func expired(at now: Date = Date()) -> Bool { now >= deadline }
}
