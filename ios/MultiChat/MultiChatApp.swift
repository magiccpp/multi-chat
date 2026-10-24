import MultiChatKit
import SwiftUI

@main
struct MultiChatApp: App {
    @State private var app = AppModel()

    var body: some Scene {
        WindowGroup {
            Group {
                if app.api == nil {
                    ConnectView()
                } else {
                    RootView()
                }
            }
            .environment(app)
            .task { await app.restore() }
        }
    }
}

struct RootView: View {
    var body: some View {
        TabView {
            SessionListView()
                .tabItem { Label("Sessions", systemImage: "bubble.left.and.bubble.right") }
            NavigationStack { StatsView() }
                .tabItem { Label("Stats", systemImage: "chart.bar") }
            NavigationStack { SettingsView() }
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
    }
}
