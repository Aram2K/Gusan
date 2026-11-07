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
    /// Captured just before the Gusan keyboard came up, so the keyboard is not in the picture.
    public var frozen: Bool
    /// The keyboard's band was cut off the bottom because no pre-keyboard picture existed.
    public var cropped: Bool
    public init(requestID: UUID, capturedAt: Date, width: Int, height: Int, jpeg: Data, frozen: Bool = false, cropped: Bool = false) {
        self.requestID = requestID; self.capturedAt = capturedAt; self.width = width; self.height = height; self.jpeg = jpeg; self.frozen = frozen; self.cropped = cropped
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

/// Where the Gusan keyboard is on screen, published by the keyboard while it is up
/// so the extension can hand over a picture from before it appeared, or crop it out.
public struct ScreenKeyboardState: Codable, Equatable {
    public let shownAt: Date
    public let keyboardHeight: Double
    public let screenHeight: Double
    public init(shownAt: Date = Date(), keyboardHeight: Double, screenHeight: Double) {
        self.shownAt = shownAt; self.keyboardHeight = keyboardHeight; self.screenHeight = screenHeight
    }
    public var bottomFraction: Double { screenHeight > 0 ? min(ScreenFrameGeometry.maxBottomCrop, max(0, keyboardHeight / screenHeight)) : 0 }
}

/// Holds the last few pictures (newest first, a second or two of history) and
/// rate-limits how often a new one is accepted so the extension doesn't scale
/// 60 frames a second. While the keyboard is up it also keeps one frozen copy
/// taken before the keyboard appeared, which is what requests get.
public final class ScreenFrameStore {
    public struct Snapshot: Equatable {
        public let image: CGImage
        public let capturedAt: Date
    }
    public let fastInterval: TimeInterval
    public let idleInterval: TimeInterval
    public let capacity: Int
    private var recent: [Snapshot] = []
    private var frozenSnapshot: Snapshot?
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
    public var frozen: Snapshot? { lock.lock(); defer { lock.unlock() }; return frozenSnapshot }
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
        lock.lock(); defer { lock.unlock() }
        recent.insert(Snapshot(image: image, capturedAt: now), at: 0)
        if recent.count > capacity { recent.removeLast(recent.count - capacity) }
        lastAccepted = now; count += 1
    }
    /// Drops every kept picture (pause, end).
    public func clear() { lock.lock(); defer { lock.unlock() }; recent = []; frozenSnapshot = nil; lastAccepted = nil }
    /// The newest picture captured at or before `instant`, or nil when nothing that old is left.
    public func snapshot(before instant: Date) -> Snapshot? { lock.lock(); defer { lock.unlock() }; return recent.first { $0.capturedAt <= instant } }
    /// Keeps the newest picture from before the keyboard appeared. Returns false
    /// when none is left, in which case requests fall back to cropping.
    @discardableResult public func freeze(before instant: Date) -> Bool {
        lock.lock(); defer { lock.unlock() }
        if let frozenSnapshot, frozenSnapshot.capturedAt <= instant { return true }
        guard let match = recent.first(where: { $0.capturedAt <= instant }) else { return false }
        frozenSnapshot = match; return true
    }
    public func unfreeze() { lock.lock(); defer { lock.unlock() }; frozenSnapshot = nil }
    /// The latest picture as a frame for `requestID`, or nil when nothing was captured yet.
    public func frame(for requestID: UUID, budgetBytes: Int = ScreenFrameEncoder.defaultBudget) -> ScreenFrame? {
        frame(for: requestID, keyboard: nil, budgetBytes: budgetBytes)
    }
    /// The frame answering a request while the keyboard described by `keyboard`
    /// is up: the frozen pre-keyboard picture when there is one, otherwise the
    /// latest picture with the keyboard's band cropped off.
    public func frame(for requestID: UUID, keyboard: ScreenKeyboardState?, budgetBytes: Int = ScreenFrameEncoder.defaultBudget) -> ScreenFrame? {
        if let keyboard {
            if freeze(before: keyboard.shownAt), let snapshot = frozen {
                return Self.encode(snapshot, requestID: requestID, frozen: true, cropBottom: 0, budgetBytes: budgetBytes)
            }
            lock.lock(); let newest = recent.first; lock.unlock()
            guard let newest else { return nil }
            return Self.encode(newest, requestID: requestID, frozen: false, cropBottom: keyboard.bottomFraction, budgetBytes: budgetBytes)
        }
        lock.lock(); let newest = recent.first; lock.unlock()
        guard let newest else { return nil }
        return Self.encode(newest, requestID: requestID, frozen: false, cropBottom: 0, budgetBytes: budgetBytes)
    }
    static func encode(_ snapshot: Snapshot, requestID: UUID, frozen: Bool, cropBottom: Double, budgetBytes: Int) -> ScreenFrame? {
        var image = snapshot.image
        var cropped = false
        if cropBottom > 0 {
            let rect = ScreenFrameGeometry.cropRect(width: image.width, height: image.height, bottomFraction: cropBottom)
            if rect.height < CGFloat(image.height), let cut = image.cropping(to: rect) { image = cut; cropped = true }
        }
        guard let jpeg = ScreenFrameEncoder.jpeg(image, budgetBytes: budgetBytes) else { return nil }
        return ScreenFrame(requestID: requestID, capturedAt: snapshot.capturedAt, width: image.width, height: image.height, jpeg: jpeg, frozen: frozen, cropped: cropped)
    }
}

public enum ScreenFrameEncoder {
    /// Smart-action bodies may be 8 MB; one frame stays far below that, also on a named pasteboard.
    public static let defaultBudget = 450_000
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
    /// The first quality whose JPEG fits the budget; the lowest quality when none does.
    public static func jpeg(_ image: CGImage, budgetBytes: Int = defaultBudget) -> Data? {
        var smallest: Data?
        for quality in qualities {
            guard let data = jpeg(image, quality: quality) else { continue }
            if data.count <= budgetBytes { return data }
            smallest = data
        }
        return smallest
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
