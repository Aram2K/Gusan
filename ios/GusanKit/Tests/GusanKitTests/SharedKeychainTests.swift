import XCTest
@testable import GusanKit

final class SharedKeychainTests: XCTestCase {
    private final class Memory: SessionBackend {
        let name: String; var items: [String: Data] = [:]; var accepts = true
        init(_ name: String, accepts: Bool = true) { self.name = name; self.accepts = accepts }
        func read(_ key: String) -> SessionBackendRead { SessionBackendRead(data: items[key], readable: accepts) }
        func write(_ data: Data, key: String) -> Bool { guard accepts else { return false }; items[key] = data; return true }
        func clear(_ key: String) { items[key] = nil }
    }
    func testTeamPrefixParsing() {
        XCTAssertEqual(SharedKeychain.parsePrefix("N88PGMQ3GH.com.aram2k.gusan.N88PGMQ3GH"), "N88PGMQ3GH")
        XCTAssertNil(SharedKeychain.parsePrefix("com.aram2k.gusan"))
        XCTAssertNil(SharedKeychain.parsePrefix(".com.aram2k.gusan"))
        XCTAssertNil(SharedKeychain.parsePrefix(nil))
        XCTAssertNil(SharedKeychain.parsePrefix("TOO-LONG-PREFIX.com.x"))
    }
    func testLayeredBackendPrefersFirstWritableLayerAndClearsOthers() {
        let keychain = Memory("Keychain"), board = Memory("Team pasteboard")
        board.items["status"] = Data([9])
        let layered = LayeredSessionBackend([keychain, board])
        XCTAssertTrue(layered.write(Data([1]), key: "status"))
        XCTAssertEqual(keychain.items["status"], Data([1]))
        XCTAssertNil(board.items["status"])
        XCTAssertEqual(layered.read("status").data, Data([1]))
        keychain.accepts = false
        XCTAssertTrue(layered.write(Data([2]), key: "status"))
        XCTAssertEqual(board.items["status"], Data([2]))
        layered.clear("status")
        XCTAssertNil(layered.read("status").data)
    }
    func testSizeLimitRefusesLargePayloads() {
        let inner = Memory("Keychain")
        let limited = SizeLimitedSessionBackend(wrapping: inner, maxBytes: 4)
        XCTAssertTrue(limited.write(Data([1, 2, 3, 4]), key: "frame"))
        XCTAssertFalse(limited.write(Data([1, 2, 3, 4, 5]), key: "frame"))
        XCTAssertEqual(limited.read("frame").data, Data([1, 2, 3, 4]))
    }
}
