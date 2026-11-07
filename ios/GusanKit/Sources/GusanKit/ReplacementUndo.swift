import Foundation

public struct ReplacementUndo: Equatable {
    public struct Step: Equatable {
        public let advanceUTF16: Int
        public let deleteCount: Int
        public let text: String
        public let restoreUTF16: Int
    }
    public let original: String
    public let replacement: String
    private let snapshot: VoiceContext
    private let originalCursorAfter: Int
    private let replacementCursorAfter: Int
    private var tail = ""
    public init(original: String, replacement: String, snapshot: VoiceContext, restoreUTF16: Int = 0) {
        self.original = original; self.replacement = replacement; self.snapshot = snapshot
        originalCursorAfter = min(max(0, restoreUTF16), original.utf16.count); replacementCursorAfter = 0
    }
    private init(original: String, replacement: String, snapshot: VoiceContext, originalCursorAfter: Int, replacementCursorAfter: Int, tail: String) {
        self.original = original; self.replacement = replacement; self.snapshot = snapshot
        self.originalCursorAfter = originalCursorAfter; self.replacementCursorAfter = replacementCursorAfter; self.tail = tail
    }
    public mutating func append(_ text: String) -> Bool {
        guard replacementCursorAfter == 0 else { return false }
        tail += text; return true
    }
    public mutating func deleteBackward() -> Bool {
        guard replacementCursorAfter == 0, !tail.isEmpty else { return false }
        tail.removeLast(); return true
    }
    public func step(context: VoiceContext) -> Step? {
        guard let document = snapshot.documentID, document == context.documentID, context.selected.isEmpty else { return nil }
        let split = replacement.utf16.count - replacementCursorAfter
        let prefix = String(decoding: replacement.utf16.prefix(split), as: UTF16.self)
        let suffix = String(decoding: replacement.utf16.suffix(replacementCursorAfter), as: UTF16.self)
        let expectedBefore = snapshot.before + prefix + tail
        let expectedAfter = suffix + snapshot.after
        guard expectedBefore.hasSuffix(context.before), expectedAfter == context.after,
              context.before.isEmpty ? expectedBefore.isEmpty : (prefix.isEmpty || context.before.count > tail.count) else { return nil }
        return Step(advanceUTF16: replacementCursorAfter - tail.utf16.count, deleteCount: replacement.count, text: original, restoreUTF16: tail.utf16.count - originalCursorAfter)
    }
    public func inverted() -> ReplacementUndo {
        ReplacementUndo(original: replacement, replacement: original, snapshot: snapshot, originalCursorAfter: replacementCursorAfter, replacementCursorAfter: originalCursorAfter, tail: tail)
    }
}
