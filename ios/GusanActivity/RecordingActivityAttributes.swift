import Foundation
import ActivityKit

/// The "Gusan session" Live Activity: ready → listening → working → done.
/// Shared by the app (starts/updates it) and GusanActivity (draws it).
struct RecordingActivityAttributes: ActivityAttributes {
    struct ContentState: Codable, Hashable {
        enum Phase: String, Codable, Hashable { case ready, listening, working, done, failed }
        var phase: Phase
        /// Last microphone levels, 0…1, at most `maxLevels`.
        var levels: [Double] = []
        var startedAt: Date?
        /// What Gusan heard (working) or the result preview (done), at most 300 characters.
        var text: String?
        /// The result went to the clipboard instead of a text field.
        var copied = false
        static let maxLevels = 24
    }
    /// "smart" or "agent".
    var mode: String
}
