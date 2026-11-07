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
    public init(requestID: UUID, capturedAt: Date, width: Int, height: Int, jpeg: Data) {
        self.requestID = requestID; self.capturedAt = capturedAt; self.width = width; self.height = height; self.jpeg = jpeg
    }
    public var dataURL: String { "data:image/jpeg;base64," + jpeg.base64EncodedString() }
}

public enum ScreenFrameGeometry {
    /// The broadcast extension keeps about 50 MB; a 1280-pixel long edge reads
    /// phone text well and stays near 3 MB as a bitmap.
    public static let defaultLongEdge = 1280
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
}

/// Holds exactly one picture, the latest, and rate-limits how often a new one
/// is accepted so the extension doesn't scale 60 frames a second.
public final class ScreenFrameStore {
    public private(set) var latest: CGImage?
    public private(set) var capturedAt: Date?
    public private(set) var accepted = 0
    public let minimumInterval: TimeInterval
    private var lastAccepted: Date?
    private let lock = NSLock()
    public init(minimumInterval: TimeInterval = 0.4) { self.minimumInterval = minimumInterval }
    /// True when a frame arriving at `now` should be processed and kept.
    public func shouldAccept(at now: Date = Date()) -> Bool {
        lock.lock(); defer { lock.unlock() }
        guard let lastAccepted else { return true }
        return now.timeIntervalSince(lastAccepted) >= minimumInterval
    }
    public func store(_ image: CGImage, at now: Date = Date()) {
        lock.lock(); defer { lock.unlock() }
        latest = image; capturedAt = now; lastAccepted = now; accepted += 1
    }
    public func clear() { lock.lock(); defer { lock.unlock() }; latest = nil; capturedAt = nil; lastAccepted = nil }
    /// The latest picture as a frame for `requestID`, or nil when nothing was captured yet.
    public func frame(for requestID: UUID, budgetBytes: Int = ScreenFrameEncoder.defaultBudget) -> ScreenFrame? {
        lock.lock(); let image = latest; let time = capturedAt; lock.unlock()
        guard let image, let time, let jpeg = ScreenFrameEncoder.jpeg(image, budgetBytes: budgetBytes) else { return nil }
        return ScreenFrame(requestID: requestID, capturedAt: time, width: image.width, height: image.height, jpeg: jpeg)
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
