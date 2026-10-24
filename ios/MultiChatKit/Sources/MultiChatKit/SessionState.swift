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

/// What a model is waiting for (server `status` events).
public struct ModelStatus: Hashable, Sendable {
    public var state: String  // queued | starting | compacting | waiting_model | thinking | writing | tool_args | tool | approval | agent
    public var text: String
    public var hint: String?
    public var since: Date  // when this state began
    public var last: Date  // last sign of life (status, text, command output)
}

/// A tip about a long wait: "Beta: running `uname -a` on This PC · 12 s".
public struct Wait: Hashable, Identifiable, Sendable {
    public var modelId: Int
    public var name: String
    public var color: String
    public var state: String
    public var text: String
    public var hint: String?
    public var seconds: Double

    public var id: Int { modelId }
    public var line: String { "\(name): \(text) · \(Wait.elapsed(seconds))" }

    public static let showAfter = 3.0  // seconds before a tip appears (approvals show at once)
    public static let stallAfter = 20.0  // seconds without new text while a reply is written
    public static let stallHint = "It may be thinking or writing a long tool call; the connection is still open. Stop cancels the turn."

    public static func elapsed(_ seconds: Double) -> String {
        let s = Int(seconds)
        let sec = s % 60
        return s < 60 ? "\(s) s" : "\(s / 60) min \(sec < 10 ? "0" : "")\(sec) s"
    }
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
    public var status: [Int: ModelStatus] = [:]  // model id -> what it is waiting for

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

    /// Tips for long waits, in participant order (same rules as the desktop app and TUI).
    public func waits(now: Date = Date()) -> [Wait] {
        let order = Dictionary(participants.enumerated().map { ($1.modelId, $0) }, uniquingKeysWith: { a, _ in a })
        return status.sorted { (order[$0.key] ?? 999) < (order[$1.key] ?? 999) }.compactMap { entry -> Wait? in
            let (mid, s) = (entry.key, entry.value)
            if s.state == "writing" {
                let idle = now.timeIntervalSince(s.last)
                return idle >= Wait.stallAfter
                    ? Wait(modelId: mid, name: name(mid), color: color(mid), state: "stalled", text: "no new output", hint: Wait.stallHint, seconds: idle)
                    : nil
            }
            let waited = now.timeIntervalSince(s.since)
            guard waited >= Wait.showAfter || s.state == "approval" else { return nil }
            return Wait(modelId: mid, name: name(mid), color: color(mid), state: s.state, text: s.text, hint: s.hint, seconds: waited)
        }
    }

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
            status.removeAll()
        case "status":
            guard let mid = event["model_id"]?.int else { break }
            if let state = event["state"]?.string, !state.isEmpty {
                let now = Date()
                let since = status[mid].flatMap { $0.state == state ? $0.since : nil } ?? now
                status[mid] = ModelStatus(state: state, text: event["text"]?.string ?? state, hint: event["hint"]?.string,
                                          since: since, last: now)
            } else {
                status[mid] = nil
            }
        case "message", "message_start", "message_end", "tool_result":
            if let m = try? event["message"]?.decode(Message.self) { add(m) }
        case "delta":
            if let id = event["message_id"]?.int, let text = event["text"]?.string { messages[id]?.content += text }
            if let mid = event["model_id"]?.int { status[mid]?.last = Date() }
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
            if let mid = event["model_id"]?.int { status[mid]?.last = Date() }
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
