import Foundation
import MultiChatKit
import Observation
import Security
import SwiftUI

/// Connection and shared lists (sessions, models). All history lives on the server.
@MainActor
@Observable
final class AppModel {
    var serverURL: String = UserDefaults.standard.string(forKey: "serverURL") ?? ""
    var api: APIClient?
    var sessions: [Session] = []
    var models: [ModelInfo] = []
    var error: String?
    var loading = false

    func restore() async {
        guard api == nil, let url = URL(string: serverURL), !serverURL.isEmpty, let token = Keychain.read(account: serverURL) else { return }
        let client = APIClient(baseURL: url, token: token)
        if (try? await client.models()) != nil {
            api = client
            await refresh()
        }
    }

    /// Validates URL + token against the server, then stores the token in the Keychain.
    func connect(url: String, token: String) async -> Bool {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines).trimmingCharacters(in: CharacterSet(charactersIn: "/"))
        guard let base = URL(string: trimmed), base.scheme != nil else {
            error = "Enter a full URL, e.g. https://multichat.example.com"
            return false
        }
        let client = APIClient(baseURL: base, token: token)
        do {
            _ = try await client.models()
        } catch {
            self.error = error.localizedDescription
            return false
        }
        serverURL = trimmed
        UserDefaults.standard.set(trimmed, forKey: "serverURL")
        Keychain.save(token, account: trimmed)
        api = client
        error = nil
        await refresh()
        return true
    }

    func signOut() {
        Keychain.delete(account: serverURL)
        api = nil
        sessions = []
        models = []
    }

    func refresh() async {
        guard let api else { return }
        loading = true
        defer { loading = false }
        do {
            async let s = api.sessions()
            async let m = api.models()
            (sessions, models) = try await (s, m)
        } catch {
            self.error = error.localizedDescription
        }
    }

    /// Starts a session. If the latest one is still empty the server reuses it instead of adding another blank one.
    func newSession(title: String = "New session", modelIds: [Int], contextMode: String, contextWindows: [Int: Int] = [:]) async -> Session? {
        guard let api else { return nil }
        do {
            let s = try await api.createSession(title: title, modelIds: modelIds, contextMode: contextMode, reuseEmpty: true,
                                                contextWindows: contextWindows)
            sessions.removeAll { $0.id == s.id }
            sessions.insert(s, at: 0)
            return s
        } catch {
            self.error = error.localizedDescription
            return nil
        }
    }

    /// The server's current model list (availability may have changed since the app loaded it).
    func reloadModels() async {
        guard let api, let fresh = try? await api.models() else { return }
        models = fresh
    }

    /// Ask the server to re-check keys and vendor model lists, then reload the models.
    func checkAvailability() async {
        guard let api else { return }
        do {
            _ = try await api.checkEndpoints()
            models = try await api.models()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func delete(_ session: Session) async {
        guard let api else { return }
        do {
            try await api.deleteSession(session.id)
            sessions.removeAll { $0.id == session.id }
        } catch {
            self.error = error.localizedDescription
        }
    }
}

/// Minimal Keychain wrapper for the server token.
enum Keychain {
    static let service = "MultiChat"

    static func save(_ value: String, account: String) {
        delete(account: account)
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecValueData as String: Data(value.utf8),
            kSecAttrAccessible as String: kSecAttrAccessibleAfterFirstUnlock,
        ]
        SecItemAdd(query as CFDictionary, nil)
    }

    static func read(account: String) -> String? {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
            kSecReturnData as String: true,
            kSecMatchLimit as String: kSecMatchLimitOne,
        ]
        var item: CFTypeRef?
        guard SecItemCopyMatching(query as CFDictionary, &item) == errSecSuccess, let data = item as? Data else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static func delete(account: String) {
        let query: [String: Any] = [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: service,
            kSecAttrAccount as String: account,
        ]
        SecItemDelete(query as CFDictionary)
    }
}

extension Color {
    /// "#rrggbb" from the server.
    init(hex: String) {
        let s = hex.trimmingCharacters(in: CharacterSet(charactersIn: "#"))
        var value: UInt64 = 0
        Scanner(string: s).scanHexInt64(&value)
        self.init(
            red: Double((value >> 16) & 0xFF) / 255,
            green: Double((value >> 8) & 0xFF) / 255,
            blue: Double(value & 0xFF) / 255
        )
    }
}

func money(_ value: Double) -> String {
    if value == 0 { return "$0" }
    return value < 0.01 ? String(format: "$%.4f", value) : String(format: "$%.2f", value)
}

func tokenCount(_ n: Int) -> String {
    n >= 1000 ? String(format: "%.1fk", Double(n) / 1000) : "\(n)"
}
