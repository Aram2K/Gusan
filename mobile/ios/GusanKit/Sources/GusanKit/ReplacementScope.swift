import Foundation

public extension VoiceContext {
    var paragraph: VoiceContext {
        guard selected.isEmpty else { return self }
        let start = before.lastIndex(where: { $0.isNewline }).map { before.index(after: $0) } ?? before.startIndex
        let end = after.firstIndex(where: { $0.isNewline }) ?? after.endIndex
        return VoiceContext(before: String(before[start...]), after: String(after[..<end]), documentID: documentID)
    }
}
