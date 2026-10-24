// swift-tools-version:5.9
import PackageDescription

// Networking, models and session state for the MultiChat iPhone app.
// Platform-neutral so it also builds and tests on Linux (`swift test`).
let package = Package(
    name: "MultiChatKit",
    platforms: [.iOS(.v17), .macOS(.v14)],
    products: [.library(name: "MultiChatKit", targets: ["MultiChatKit"])],
    targets: [
        .target(name: "MultiChatKit"),
        .testTarget(name: "MultiChatKitTests", dependencies: ["MultiChatKit"]),
    ]
)
