import Foundation

/// Decides whether a typed or spoken request is an agent task (something to go and do on the web)
/// rather than a text edit, and which mode it needs. Pure and deterministic so the keyboard can
/// judge instantly before any network call.
public struct AgentIntent: Equatable {
    public let mode: AgentMode
    public let instruction: String

    public init(mode: AgentMode, instruction: String) { self.mode = mode; self.instruction = instruction }

    static let doVerbs = ["book", "order", "buy", "purchase", "reserve", "schedule", "sign up", "sign me up", "register", "subscribe", "unsubscribe", "cancel my", "send", "submit", "post", "apply for", "apply to", "pay", "renew", "return", "rsvp", "add to cart", "checkout", "check out", "fill in", "fill out", "file", "transfer", "donate", "enroll", "enrol"]
    static let researchVerbs = ["find", "research", "look up", "lookup", "search", "search for", "compare", "check", "investigate", "list", "get me", "tell me", "what is", "what's", "what are", "who is", "who's", "when is", "when does", "where is", "where can", "how much", "how many", "how do", "how does", "which", "is there", "are there", "show me", "give me", "recommend", "suggest", "summarize the latest", "news about", "price of", "best", "cheapest", "nearest", "reviews of", "reviews for"]
    static let editVerbs = ["check grammar", "check spelling", "check the grammar", "check the spelling", "check my", "check this", "check it", "list the pros", "list pros", "order the", "order these", "sort", "which is better", "which one", "which sounds", "find a nicer", "find a better", "find another way", "find a way to say", "find a word", "find a synonym", "rewrite", "rephrase", "reword", "fix", "correct", "translate", "shorten", "expand", "proofread", "make it", "make this", "summarize this", "summarise this", "summarize the text", "formal", "casual", "polite", "friendlier", "bullet", "reply to this", "answer this", "respond to this", "write an email", "write a message", "write a reply", "draft", "continue", "edit", "tone", "emoji", "grammar", "spelling", "capitalize", "punctuation"]
    static let researchPrefixes = ["please ", "can you ", "could you ", "hey gusan, ", "hey gusan ", "gusan, ", "gusan ", "agent, ", "agent "]

    /// Returns nil when the request reads like a text edit (the Smart Actions pipeline handles it).
    public static func detect(_ raw: String) -> AgentIntent? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        guard text.count >= 6, text.count <= 4000 else { return nil }
        var lowered = text.lowercased()
        for prefix in researchPrefixes where lowered.hasPrefix(prefix) {
            text = String(text.dropFirst(prefix.count)).trimmingCharacters(in: .whitespaces)
            lowered = text.lowercased()
        }
        guard !lowered.isEmpty else { return nil }
        // "it"/"this"/"the text" after a verb means the user is talking about the draft, not the web.
        if editVerbs.contains(where: { lowered.hasPrefix($0) }) { return nil }
        if lowered.range(of: #"^\w+ (it|this|that|the text|my text|the message|this message|my message)\b"#, options: .regularExpression) != nil { return nil }

        if doVerbs.contains(where: { matches(lowered, verb: $0) }) {
            return AgentIntent(mode: .do, instruction: text)
        }
        if researchVerbs.contains(where: { matches(lowered, verb: $0) }) {
            return AgentIntent(mode: .research, instruction: text)
        }
        return nil
    }

    private static func matches(_ lowered: String, verb: String) -> Bool {
        guard lowered.hasPrefix(verb) else { return false }
        let next = lowered.dropFirst(verb.count).first
        return next == nil || next == " " || next == "," || next == "?" || next == ":"
    }

    /// Whether the instruction talks about the draft ("using this", "from this text"), so the text should be sent as context.
    public static func referencesDraft(_ instruction: String) -> Bool {
        instruction.lowercased().range(of: #"\b(this|these|the text|my text|the draft|my draft|the selection|the selected text|above|below|here)\b"#, options: .regularExpression) != nil
    }

    /// Short card title, e.g. "Research: cheapest flights to Rome".
    public var title: String {
        let trimmed = instruction.trimmingCharacters(in: .whitespacesAndNewlines)
        let cut = trimmed.count > 90 ? String(trimmed.prefix(87)).trimmingCharacters(in: .whitespaces) + "…" : trimmed
        return cut
    }
}
