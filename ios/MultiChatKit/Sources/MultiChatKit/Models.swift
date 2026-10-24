import Foundation

// Mirrors of the server's JSON (snake_case keys are converted to camelCase). Dates stay strings.

public struct ModelInfo: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var endpointId: Int
    public var endpointName: String
    public var endpointKind: String
    public var modelName: String
    public var displayName: String
    public var color: String
    public var inputPrice: Double
    public var outputPrice: Double
    public var enabled: Bool
    public var supportsTools: Bool
    public var source: String
}

public struct Endpoint: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var name: String
    public var kind: String
    public var driver: String?
    public var baseUrl: String?
    public var apiKeyMasked: String?
    public var source: String
    public var enabled: Bool
}

public struct Participant: Codable, Hashable, Sendable {
    public var modelId: Int
    public var displayName: String
    public var color: String
    public var endpointKind: String
    public var position: Int
}

public struct Session: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var title: String
    public var workspaceKind: String
    public var runnerId: Int?
    public var sshConnectionId: Int?
    public var workspacePath: String?
    public var workspaceLabel: String
    public var phase: String
    public var contextMode: String
    public var builderModelId: Int?
    public var approveEdits: String
    public var approveCommands: String
    public var createdAt: String
    public var updatedAt: String
    public var participants: [Participant]
    public var busy: Bool

    public var isBuild: Bool { phase == "build" }
}

public struct ToolCall: Codable, Hashable, Sendable {
    public var id: String
    public var name: String
    public var arguments: JSONValue?
    public var external: Bool?
}

public struct Message: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var seq: Int
    public var turnId: String
    public var role: String
    public var modelId: Int?
    public var phase: String
    public var content: String
    public var toolCalls: [ToolCall]?
    public var toolCallId: String?
    public var recipients: [Int]?
    public var status: String
    public var vote: Int

    enum CodingKeys: String, CodingKey {
        case id, seq, turnId, role, modelId, phase, content, toolCalls, toolCallId, recipients, status, vote
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        seq = try c.decodeIfPresent(Int.self, forKey: .seq) ?? 0
        turnId = try c.decodeIfPresent(String.self, forKey: .turnId) ?? ""
        role = try c.decode(String.self, forKey: .role)
        modelId = try c.decodeIfPresent(Int.self, forKey: .modelId)
        phase = try c.decodeIfPresent(String.self, forKey: .phase) ?? "plan"
        content = try c.decodeIfPresent(String.self, forKey: .content) ?? ""
        toolCalls = try c.decodeIfPresent([ToolCall].self, forKey: .toolCalls)
        toolCallId = try c.decodeIfPresent(String.self, forKey: .toolCallId)
        recipients = try c.decodeIfPresent([Int].self, forKey: .recipients)
        status = try c.decodeIfPresent(String.self, forKey: .status) ?? "complete"
        vote = try c.decodeIfPresent(Int.self, forKey: .vote) ?? 0
    }

    public var isReply: Bool { role == "assistant" }
}

public struct UsageRow: Codable, Hashable, Sendable {
    public var modelId: Int?
    public var displayName: String
    public var color: String
    public var inputTokens: Int
    public var outputTokens: Int
    public var cost: Double
    public var estimated: Bool
}

public struct StatRow: Codable, Hashable, Identifiable, Sendable {
    public var modelId: Int
    public var displayName: String
    public var color: String
    public var endpoint: String
    public var endpointKind: String
    public var inputTokens: Int
    public var outputTokens: Int
    public var cost: Double
    public var calls: Int
    public var replies: Int
    public var avgLatencyMs: Double?
    public var avgTtftMs: Double?
    public var tokensPerS: Double?
    public var upvotes: Int
    public var downvotes: Int
    public var approval: Double?
    public var builderPicks: Int
    public var sessions: Int
    public var winRate: Double?
    public var costPerUpvote: Double?
    public var estimated: Bool

    public var id: Int { modelId }
}

public struct DailyCost: Codable, Hashable, Sendable {
    public var day: String
    public var modelId: Int?
    public var cost: Double
    public var tokens: Int
}

public struct StatTotals: Codable, Hashable, Sendable {
    public var cost: Double
    public var inputTokens: Int
    public var outputTokens: Int
    public var upvotes: Int
    public var downvotes: Int
}

public struct Stats: Codable, Hashable, Sendable {
    public var models: [StatRow]
    public var daily: [DailyCost]
    public var totals: StatTotals
}

public struct RunnerInfo: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var name: String
    public var os: String
    public var shell: String
    public var hostname: String
    public var roots: [String]
    public var version: String
    public var online: Bool
}

public struct SshConnection: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var name: String
    public var host: String
    public var port: Int
    public var username: String
    public var auth: String
    public var shell: String
    public var hostKeyFingerprint: String?
    public var source: String
}

public struct BrowseEntry: Codable, Hashable, Sendable {
    public var name: String
    public var isDir: Bool
}

public struct BrowseResult: Codable, Hashable, Sendable {
    public var path: String
    public var entries: [BrowseEntry]
}

public struct FileChange: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var turnId: String
    public var modelId: Int?
    public var path: String
    public var before: String?
    public var after: String?
    public var reverted: Bool
}

public struct ServerSettings: Codable, Hashable, Sendable {
    public struct Search: Codable, Hashable, Sendable {
        public var provider: String?
        public var configured: Bool
    }
    public var webSearch: Search
}
