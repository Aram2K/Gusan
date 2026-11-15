import ActivityKit
import Foundation
import GusanKit

/// The "Gusan session" Live Activity (Dynamic Island glow while listening).
/// iOS lets the app start it only in the foreground or inside a LiveActivityIntent;
/// updates and the end work from the background while the audio session keeps the app alive.
@MainActor
final class VoiceLiveActivity {
    typealias State = RecordingActivityAttributes.ContentState
    private var activity: Activity<RecordingActivityAttributes>?
    private var last: State?
    private var lastUpdate = Date.distantPast
    private var levels: [Double] = []

    init() { activity = Activity<RecordingActivityAttributes>.activities.first }

    func begin(mode: String = "smart") {
        if let current = activity, current.activityState == .active || current.activityState == .stale { return }
        guard ActivityAuthorizationInfo().areActivitiesEnabled else { return }
        let state = State(phase: .ready)
        activity = try? Activity.request(attributes: RecordingActivityAttributes(mode: mode), content: ActivityContent(state: state, staleDate: Date().addingTimeInterval(20 * 60)), pushType: nil)
        last = state
    }

    func update(_ status: VoiceStatus, heard: String?, copied: Bool = false) {
        guard activity != nil else { return }
        let phase: State.Phase
        switch status.phase {
        case .off: end(); return
        case .ready: phase = .ready
        case .recording: phase = .listening
        case .transcribing, .thinking: phase = .working
        case .done: phase = .done
        case .failed: phase = .failed
        }
        if phase == .listening {
            if last?.phase != .listening { levels = [] }
            levels = Array((levels + [Double(status.level)]).suffix(State.maxLevels))
        }
        // Keep "Inserted"/"Copied" on screen until the next take instead of flashing back to ready.
        if phase == .ready, last?.phase == .done { return }
        var state = State(phase: phase)
        state.levels = phase == .listening ? levels : []
        state.startedAt = phase == .listening ? (last?.phase == .listening ? last?.startedAt : Date()) : nil
        let text = phase == .failed ? status.message : phase == .done ? status.result : heard
        state.text = text.map { String($0.prefix(300)) }
        state.copied = copied
        guard state != last else { return }
        // ActivityKit budgets updates: phase changes go out at once, waveform levels at most once a second.
        if state.phase == last?.phase, state.text == last?.text, state.copied == last?.copied, Date().timeIntervalSince(lastUpdate) < 1 { return }
        last = state; lastUpdate = Date()
        let content = ActivityContent(state: state, staleDate: Date().addingTimeInterval(20 * 60))
        let current = activity
        Task { await current?.update(content) }
    }

    func end() {
        guard let current = activity else { return }
        activity = nil; last = nil; levels = []
        Task { await current.end(nil, dismissalPolicy: .immediate) }
    }
}
