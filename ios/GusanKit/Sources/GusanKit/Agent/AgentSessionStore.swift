import Foundation

public final class AgentSessionStore {
    public struct Record: Codable, Equatable, Identifiable {
        public let id: UUID
        public let sessionID: UUID
        public let message: String
        public let date: Date
    }
    private let defaults: UserDefaults
    private let key = "gusan.agent.sessions"
    public init(defaults: UserDefaults) { self.defaults = defaults }
    public var records: [Record] {
        guard let data = defaults.data(forKey: key) else { return [] }
        return (try? JSONDecoder().decode([Record].self, from: data)) ?? []
    }
    @discardableResult
    public func record(taskID: UUID, message: String, continuing parentID: UUID? = nil, date: Date = Date()) -> UUID {
        var all = records
        if let existing = all.first(where: { $0.id == taskID }) { return existing.sessionID }
        let root = parentID.flatMap { parent in all.first { $0.id == parent }?.sessionID ?? parent } ?? taskID
        all.append(Record(id: taskID, sessionID: root, message: String(message.prefix(4_000)), date: date))
        if let data = try? JSONEncoder().encode(Array(all.suffix(200))) { defaults.set(data, forKey: key) }
        return root
    }
    public func tasks(in sessionID: UUID) -> [Record] {
        records.filter { $0.sessionID == sessionID }.sorted { $0.date < $1.date }
    }
}
