import Foundation

public final class SmartHistory {
    public struct Entry: Codable, Equatable, Identifiable {
        public let id: UUID
        public let date: Date
        public let instruction: String
        public let result: String
        public let sourceApp: String?
    }
    private let defaults: UserDefaults
    private let key = "gusan.smart.history"
    public init(defaults: UserDefaults) { self.defaults = defaults }
    public convenience init(group: String?) {
        self.init(defaults: group.flatMap(UserDefaults.init(suiteName:)) ?? .standard)
    }
    public var entries: [Entry] {
        guard let data = defaults.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([Entry].self, from: data)) ?? []
    }
    public func append(instruction: String, result: String, sourceApp: String? = nil, date: Date = Date()) {
        guard !result.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty else { return }
        let entry = Entry(id: UUID(), date: date, instruction: String(instruction.prefix(4_000)), result: String(result.prefix(20_000)), sourceApp: sourceApp)
        if let data = try? JSONEncoder().encode(Array((entries + [entry]).suffix(50))) { defaults.set(data, forKey: key) }
    }
}
