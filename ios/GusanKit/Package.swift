// swift-tools-version: 5.9
import PackageDescription

let package = Package(
    name: "GusanKit",
    platforms: [.iOS(.v16), .macOS(.v13)],
    products: [.library(name: "GusanKit", targets: ["GusanKit"])],
    targets: [
        .target(name: "GusanKit", resources: [.process("Resources")]),
        .testTarget(name: "GusanKitTests", dependencies: ["GusanKit"], resources: [.process("Fixtures")])
    ]
)
