import Foundation

public struct VoicePendingInsert: Codable, Equatable {
    public static let key = "pendingInsert"
    public let text: String
    public let at: Double
    public let account: String?
    public init(text: String, at: Date = Date(), account: String? = nil) {
        self.text = text; self.at = at.timeIntervalSince1970; self.account = account
    }
    public func write(to defaults: UserDefaults) {
        guard let data = try? JSONEncoder().encode(self) else { return }
        defaults.set(data, forKey: Self.key)
    }
    public static func read(from defaults: UserDefaults, account: String?, now: Date = Date()) -> Self? {
        let value = defaults.object(forKey: key)
        let data: Data?
        if let bytes = value as? Data { data = bytes }
        else if let json = value as? String { data = json.data(using: .utf8) }
        else if let object = value as? [String: Any] { data = try? JSONSerialization.data(withJSONObject: object) }
        else { data = nil }
        guard let data, let pending = try? JSONDecoder().decode(Self.self, from: data),
              let account, !account.isEmpty, pending.account == account,
              !pending.text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
              (0..<600).contains(now.timeIntervalSince1970 - pending.at) else {
            clear(in: defaults); return nil
        }
        return pending
    }
    public static func clear(in defaults: UserDefaults) { defaults.removeObject(forKey: key) }
}
