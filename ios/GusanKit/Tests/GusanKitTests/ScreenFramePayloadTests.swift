import XCTest
@testable import GusanKit

final class ScreenFramePayloadTests: XCTestCase {
    func testFramePayloadLimitAndFallback() throws {
        let oversized = MemoryBackend(), fallback = MemoryBackend()
        let channel = ScreenChannel(stores: [oversized, fallback])
        let frame = ScreenFrame(requestID: UUID(), capturedAt: Date(), width: 1, height: 1, jpeg: Data([1]))
        XCTAssertTrue(channel.write(frame: frame))
        XCTAssertEqual(channel.readFrame(), frame)
        let packet = try XCTUnwrap(oversized.values["frame"])
        oversized.values["frame"] = packet + Data(repeating: 32, count: ScreenChannel.maximumFramePayloadBytes - packet.count)
        XCTAssertEqual(ScreenChannel(stores: [oversized]).readFrame(), frame)
        oversized.values["frame"]?.append(32)
        XCTAssertNil(ScreenChannel(stores: [oversized]).readFrame())
        XCTAssertEqual(channel.readFrame(), frame)
    }
    func testMissingEmptyAndMalformedPayloads() {
        let store = MemoryBackend(), channel = ScreenChannel(stores: [])
        XCTAssertNil(channel.readFrame())
        for data in [Data(), Data("invalid".utf8)] {
            store.values["frame"] = data
            XCTAssertNil(ScreenChannel(stores: [store]).readFrame())
        }
    }
}
