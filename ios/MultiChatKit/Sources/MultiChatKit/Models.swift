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
    /// False when the server can't use the model right now (no or rejected key, not offered by the vendor).
    public var available: Bool = true
    public var availabilityDetail: String = ""
    public var supportsImages: Bool = true
    public var contextLength: Int?
    public var supportsEffort: Bool = false

    /// Context windows to choose from when a session starts: 256K or the model's maximum when that is over 512K;
    /// otherwise just the maximum.
    public var contextChoices: [Int] {
        guard let max = contextLength, max > 0 else { return [] }
        return max > 512_000 ? [256_000, max] : [max]
    }
}

extension ModelInfo {
    enum CodingKeys: String, CodingKey {
        case id, endpointId, endpointName, endpointKind, modelName, displayName, color, inputPrice, outputPrice, enabled,
             supportsTools, source, available, availabilityDetail, supportsImages, contextLength, supportsEffort
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(Int.self, forKey: .id)
        endpointId = try c.decode(Int.self, forKey: .endpointId)
        endpointName = try c.decode(String.self, forKey: .endpointName)
        endpointKind = try c.decode(String.self, forKey: .endpointKind)
        modelName = try c.decode(String.self, forKey: .modelName)
        displayName = try c.decode(String.self, forKey: .displayName)
        color = try c.decode(String.self, forKey: .color)
        inputPrice = try c.decode(Double.self, forKey: .inputPrice)
        outputPrice = try c.decode(Double.self, forKey: .outputPrice)
        enabled = try c.decode(Bool.self, forKey: .enabled)
        supportsTools = try c.decodeIfPresent(Bool.self, forKey: .supportsTools) ?? true
        source = try c.decodeIfPresent(String.self, forKey: .source) ?? "api"
        available = try c.decodeIfPresent(Bool.self, forKey: .available) ?? true
        availabilityDetail = try c.decodeIfPresent(String.self, forKey: .availabilityDetail) ?? ""
        supportsImages = try c.decodeIfPresent(Bool.self, forKey: .supportsImages) ?? true
        contextLength = try c.decodeIfPresent(Int.self, forKey: .contextLength)
        supportsEffort = try c.decodeIfPresent(Bool.self, forKey: .supportsEffort) ?? false
    }
}

/// A file attached to a user message (images are shown, documents are read by the models).
public struct AttachmentInfo: Codable, Hashable, Identifiable, Sendable {
    public var id: Int
    public var filename: String
    public var mime: String
    public var size: Int
    public var kind: String  // image | document | binary

    public var isImage: Bool { kind == "image" }
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
    /// unchecked | ok | no_key | error — the server checks keys and model lists at start-up and periodically.
    public var status: String?
    public var statusDetail: String?
    public var checkedAt: String?
}

public struct EndpointStatus: Codable, Hashable, Sendable {
    public var id: Int
    public var name: String
    public var status: String
    public var detail: String
}

public struct Participant: Codable, Hashable, Sendable {
    public var modelId: Int
    public var displayName: String
    public var color: String
    public var endpointKind: String
    public var position: Int
    /// Chosen when the session started (nil = the model's maximum); fixed afterwards.
    public var contextWindow: Int?
    public var contextLength: Int?
    /// low | medium | high | xhigh | max; nil = the model's default. Can change any time.
    public var effort: String?
    public var supportsEffort: Bool?
}

public enum Effort {
    public static let levels = ["low", "medium", "high", "xhigh", "max"]
    public static func label(_ level: String?) -> String {
        switch level {
        case "low": "Low"
        case "medium": "Medium"
        case "high": "High"
        case "xhigh": "X-High"
        case "max": "Max"
        default: "Default"
        }
    }
}

public func contextLabel(_ tokens: Int?) -> String {
    guard let t = tokens, t > 0 else { return "default" }
    let binary = t % 65_536 == 0  // 262,144 = 256K, 1,048,576 = 1M; 256,000 and 128,000 stay decimal
    if t >= 1_000_000 {
        let millions = binary ? Double(t) / 1_048_576 : Double(t) / 1_000_000
        var s = String(format: "%.2f", millions)
        while s.hasSuffix("0") { s.removeLast() }
        if s.hasSuffix(".") { s.removeLast() }
        return s + "M"
    }
    return binary ? "\(t / 1024)K" : "\(Int((Double(t) / 1000).rounded()))K"
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
    /// USD spent in this session (all models) and its tokens; older servers don't send them.
    public var cost: Double?
    public var tokens: Int?

    public var isBuild: Bool { phase == "build" }
}

/// Tokens and cost of one reply.
public struct MessageUsage: Codable, Hashable, Sendable {
    public var inputTokens: Int
    public var outputTokens: Int
    public var cost: Double
    public var estimated: Bool

    public init(inputTokens: Int, outputTokens: Int, cost: Double, estimated: Bool) {
        self.inputTokens = inputTokens
        self.outputTokens = outputTokens
        self.cost = cost
        self.estimated = estimated
    }

    public var tokens: Int { inputTokens + outputTokens }
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
    public var attachments: [AttachmentInfo]
    public var usage: MessageUsage?

    enum CodingKeys: String, CodingKey {
        case id, seq, turnId, role, modelId, phase, content, toolCalls, toolCallId, recipients, status, vote, attachments, usage
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
        attachments = try c.decodeIfPresent([AttachmentInfo].self, forKey: .attachments) ?? []
        usage = try c.decodeIfPresent(MessageUsage.self, forKey: .usage)
    }

    public var isReply: Bool { role == "assistant" }
    /// A model's summary of the older conversation, written when its context window was ~80% full.
    public var isSummary: Bool { role == "summary" }
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
