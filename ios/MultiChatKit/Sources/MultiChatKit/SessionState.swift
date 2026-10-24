import Foundation

public struct UsageTotals: Hashable, Sendable {
    public var inputTokens = 0
    public var outputTokens = 0
    public var cost = 0.0
    public var estimated = false

    public var tokens: Int { inputTokens + outputTokens }
}

/// A pending approval request for the builder's next edit or command.
public struct Approval: Hashable, Identifiable, Sendable {
    public var callId: String
    public var kind: String  // edit | command | tool
    public var modelId: Int?
    public var path: String?
    public var diff: String?
    public var command: String?
    public var detail: String?

    public var id: String { callId }
}

/// Client-side view of one session, updated from server events (same rules as the desktop app and TUI).
public struct SessionState: Sendable {
    public var session: Session?
    public var models: [Int: ModelInfo] = [:]
    public var messages: [Int: Message] = [:]
    public var usage: [Int: UsageTotals] = [:]  // key: model id (0 = unknown)
    public var approvals: [String: Approval] = [:]
    public var progress: [String: String] = [:]  // tool call id -> live command output
    public var results: [String: Message] = [:]  // tool call id -> tool result message
    public var fileChanges: [JSONValue] = []
    public var busy = false
    public var notices: [String] = []

    public init() {}

    public init(session: Session, messages: [Message], usage: [UsageRow], models: [ModelInfo]) {
        self.session = session
        self.models = Dictionary(uniqueKeysWithValues: models.map { ($0.id, $0) })
        for m in messages { add(m) }
        for u in usage {
            self.usage[u.modelId ?? 0] = UsageTotals(inputTokens: u.inputTokens, outputTokens: u.outputTokens, cost: u.cost, estimated: u.estimated)
        }
        busy = session.busy
    }

    // MARK: queries

    public var participants: [Participant] { session?.participants ?? [] }

    public func name(_ modelId: Int?) -> String {
        guard let modelId else { return "You" }
        return models[modelId]?.displayName ?? "model \(modelId)"
    }

    public func color(_ modelId: Int?) -> String {
        guard let modelId, let m = models[modelId] else { return "#9ca3af" }
        return m.color
    }

    /// Messages in order; with a model id, that model's thread (prompts it received and its own replies).
    public func ordered(for modelId: Int? = nil) -> [Message] {
        let all = messages.values.sorted { $0.id < $1.id }
        guard let modelId else { return all }
        return all.filter { m in
            m.modelId == modelId || (m.role == "user" && (m.recipients?.isEmpty ?? true || m.recipients!.contains(modelId)))
        }
    }

    /// What the chat list shows: user prompts and model replies (tool results render inside their reply).
    public func visible(for modelId: Int? = nil) -> [Message] {
        ordered(for: modelId).filter { $0.role != "tool" }
    }

    public var totalCost: Double { usage.values.reduce(0) { $0 + $1.cost } }

    // MARK: events

    mutating func add(_ m: Message) {
        var m = m
        if let old = messages[m.id], m.vote == 0 { m.vote = old.vote }
        if let old = messages[m.id], m.usage == nil { m.usage = old.usage }
        messages[m.id] = m
        if m.role == "tool", let callId = m.toolCallId { results[callId] = m }
    }

    /// Apply one server event; returns its type.
    @discardableResult
    public mutating func apply(_ event: JSONValue) -> String {
        let type = event["type"]?.string ?? ""
        switch type {
        case "turn_start":
            busy = true
        case "turn_end":
            busy = false
            approvals.removeAll()
        case "message", "message_start", "message_end", "tool_result":
            if let m = try? event["message"]?.decode(Message.self) { add(m) }
        case "delta":
            if let id = event["message_id"]?.int, let text = event["text"]?.string { messages[id]?.content += text }
        case "tool_call":
            if let id = event["message_id"]?.int, let call = try? event["call"]?.decode(ToolCall.self) {
                var calls = messages[id]?.toolCalls ?? []
                if !calls.contains(where: { $0.id == call.id }) {
                    calls.append(call)
                    messages[id]?.toolCalls = calls
                }
            }
        case "tool_progress":
            if let id = event["call_id"]?.string, let text = event["text"]?.string { progress[id, default: ""] += text }
        case "usage":
            let key = event["model_id"]?.int ?? 0
            var u = usage[key] ?? UsageTotals()
            u.inputTokens += event["input_tokens"]?.int ?? 0
            u.outputTokens += event["output_tokens"]?.int ?? 0
            u.cost += event["cost"]?.double ?? 0
            u.estimated = u.estimated || (event["estimated"]?.bool ?? false)
            usage[key] = u
            if let mid = event["message_id"]?.int, var m = messages[mid] {
                var mu = m.usage ?? MessageUsage(inputTokens: 0, outputTokens: 0, cost: 0, estimated: false)
                mu.inputTokens += event["input_tokens"]?.int ?? 0
                mu.outputTokens += event["output_tokens"]?.int ?? 0
                mu.cost += event["cost"]?.double ?? 0
                mu.estimated = mu.estimated || (event["estimated"]?.bool ?? false)
                m.usage = mu
                messages[mid] = m
            }
        case "approval_required":
            if let callId = event["call_id"]?.string {
                approvals[callId] = Approval(
                    callId: callId,
                    kind: event["kind"]?.string ?? "tool",
                    modelId: event["model_id"]?.int,
                    path: event["path"]?.string,
                    diff: event["diff"]?.string,
                    command: event["command"]?.string,
                    detail: event["detail"]?.string
                )
            }
        case "approval_resolved":
            if let callId = event["call_id"]?.string { approvals[callId] = nil }
        case "vote":
            if let id = event["message_id"]?.int, let value = event["value"]?.int { messages[id]?.vote = value }
        case "session":
            if let s = try? event["session"]?.decode(Session.self) { session = s }
        case "file_changed":
            fileChanges.append(event)
        case "error", "notice":
            notices.append(event["message"]?.string ?? type)
        default:
            break
        }
        return type
    }
}
