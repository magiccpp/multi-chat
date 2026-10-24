import Foundation
#if canImport(FoundationNetworking)
import FoundationNetworking
#endif

public struct APIError: Error, LocalizedError, Sendable {
    public var status: Int
    public var detail: String
    public var errorDescription: String? { status == 0 ? detail : "\(detail) (HTTP \(status))" }
}

/// Parses `data: {...}` lines of the server's Server-Sent Events stream (one JSON object per event).
public struct SSEParser {
    public init() {}

    public func event(fromLine line: Substring) -> JSONValue? {
        guard line.hasPrefix("data:") else { return nil }
        let payload = line.dropFirst(5).drop(while: { $0 == " " })
        return try? JSONDecoder().decode(JSONValue.self, from: Data(payload.utf8))
    }

    /// For whole bodies (used where streaming is unavailable, e.g. Linux tests).
    public func events(in body: String) -> [JSONValue] {
        body.split(separator: "\n", omittingEmptySubsequences: true).compactMap { event(fromLine: $0) }
    }
}

/// Async client for the MultiChat server API.
public final class APIClient: Sendable {
    public let baseURL: URL
    let token: String
    let session: URLSession

    public init(baseURL: URL, token: String, session: URLSession = .shared) {
        self.baseURL = baseURL
        self.token = token
        self.session = session
    }

    // MARK: plumbing

    func request(_ method: String, _ path: String, query: [String: String] = [:], body: Data? = nil) -> URLRequest {
        var comps = URLComponents(url: baseURL.appendingPathComponent(path), resolvingAgainstBaseURL: false)!
        if !query.isEmpty { comps.queryItems = query.sorted { $0.key < $1.key }.map { URLQueryItem(name: $0.key, value: $0.value) } }
        var req = URLRequest(url: comps.url!)
        req.httpMethod = method
        req.setValue("Bearer \(token)", forHTTPHeaderField: "Authorization")
        if let body {
            req.httpBody = body
            req.setValue("application/json", forHTTPHeaderField: "Content-Type")
        }
        return req
    }

    func send(_ req: URLRequest) async throws -> Data {
        let data: Data
        let response: URLResponse
        do {
            (data, response) = try await session.data(for: req)
        } catch {
            throw APIError(status: 0, detail: "Cannot reach the server: \(error.localizedDescription)")
        }
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        guard (200..<300).contains(status) else {
            let detail = (try? JSONDecoder().decode(JSONValue.self, from: data))?["detail"]?.string
                ?? String(data: data, encoding: .utf8) ?? "error"
            throw APIError(status: status, detail: status == 401 ? "The server rejected the token" : detail)
        }
        return data
    }

    func get<T: Decodable>(_ path: String, query: [String: String] = [:]) async throws -> T {
        try JSONCoding.decoder.decode(T.self, from: try await send(request("GET", path, query: query)))
    }

    func call<T: Decodable>(_ method: String, _ path: String, json: [String: JSONValue], query: [String: String] = [:]) async throws -> T {
        let body = try JSONEncoder().encode(json)
        return try JSONCoding.decoder.decode(T.self, from: try await send(request(method, path, query: query, body: body)))
    }

    struct Empty: Decodable {}

    // MARK: server & models

    public func health() async throws -> JSONValue { try await get("api/health") }
    public func settings() async throws -> ServerSettings { try await get("api/admin/settings") }
    public func models() async throws -> [ModelInfo] { try await get("api/models") }
    public func endpoints() async throws -> [Endpoint] { try await get("api/endpoints") }
    public func runners() async throws -> [RunnerInfo] { try await get("api/runners") }
    public func sshConnections() async throws -> [SshConnection] { try await get("api/ssh") }
    public func stats(from: String? = nil) async throws -> Stats {
        try await get("api/stats", query: from.map { ["date_from": $0] } ?? [:])
    }

    public func browseRunner(_ id: Int, path: String = "") async throws -> BrowseResult {
        try await get("api/runners/\(id)/browse", query: ["path": path])
    }

    public func browseSSH(_ id: Int, path: String = "") async throws -> BrowseResult {
        try await get("api/ssh/\(id)/browse", query: ["path": path])
    }

    // MARK: sessions

    public func sessions(limit: Int = 100) async throws -> [Session] { try await get("api/sessions", query: ["limit": "\(limit)"]) }
    public func session(_ id: Int) async throws -> Session { try await get("api/sessions/\(id)") }
    public func messages(_ id: Int) async throws -> [Message] { try await get("api/sessions/\(id)/messages") }
    public func usage(_ id: Int) async throws -> [UsageRow] { try await get("api/sessions/\(id)/usage") }
    public func changes(_ id: Int) async throws -> [FileChange] { try await get("api/sessions/\(id)/changes") }

    /// `reuseEmpty`: if the latest session has no messages yet, the server updates and returns it instead.
    public func createSession(title: String, modelIds: [Int], contextMode: String = "isolated", reuseEmpty: Bool = false) async throws -> Session {
        try await call("POST", "api/sessions", json: [
            "title": .string(title),
            "model_ids": .array(modelIds.map { .number(Double($0)) }),
            "context_mode": .string(contextMode),
            "reuse_empty": .bool(reuseEmpty),
        ])
    }

    /// Re-check every endpoint's key and model list now.
    public func checkEndpoints() async throws -> [EndpointStatus] {
        try await call("POST", "api/endpoints/check", json: [:])
    }

    // MARK: attachments

    /// Upload a document or image; send the returned id with `chat(attachmentIds:)`.
    public func uploadAttachment(_ sessionId: Int, filename: String, data: Data, mime: String?) async throws -> AttachmentInfo {
        var json: [String: JSONValue] = ["filename": .string(filename), "data_base64": .string(data.base64EncodedString())]
        if let mime { json["mime"] = .string(mime) }
        return try await call("POST", "api/sessions/\(sessionId)/attachments", json: json)
    }

    public func attachmentData(_ id: Int) async throws -> Data {
        try await send(request("GET", "api/attachments/\(id)"))
    }

    public func deleteSession(_ id: Int) async throws {
        _ = try await send(request("DELETE", "api/sessions/\(id)"))
    }

    public func updateSession(_ id: Int, _ fields: [String: JSONValue]) async throws -> Session {
        try await call("PATCH", "api/sessions/\(id)", json: fields)
    }

    public func setModels(_ id: Int, modelIds: [Int]) async throws -> Session {
        try await call("PUT", "api/sessions/\(id)/models", json: ["model_ids": .array(modelIds.map { .number(Double($0)) })])
    }

    public func setWorkspace(_ id: Int, kind: String, path: String? = nil, runnerId: Int? = nil, sshConnectionId: Int? = nil) async throws -> Session {
        var json: [String: JSONValue] = ["kind": .string(kind)]
        if let path { json["path"] = .string(path) }
        if let runnerId { json["runner_id"] = .number(Double(runnerId)) }
        if let sshConnectionId { json["ssh_connection_id"] = .number(Double(sshConnectionId)) }
        return try await call("PATCH", "api/sessions/\(id)/workspace", json: json)
    }

    public func pickBuilder(_ id: Int, modelId: Int) async throws -> Session {
        try await call("POST", "api/sessions/\(id)/pick-builder", json: ["model_id": .number(Double(modelId)), "switch_to_build": .bool(true)])
    }

    public func vote(messageId: Int, value: Int) async throws {
        let _: JSONValue = try await call("PUT", "api/messages/\(messageId)/vote", json: ["value": .number(Double(value))])
    }

    public func approve(_ id: Int, callId: String, approve: Bool, note: String? = nil) async throws {
        var json: [String: JSONValue] = ["approve": .bool(approve)]
        if let note { json["note"] = .string(note) }
        let _: JSONValue = try await call("POST", "api/sessions/\(id)/approvals/\(callId)", json: json)
    }

    public func stop(_ id: Int) async throws {
        _ = try await send(request("POST", "api/sessions/\(id)/stop"))
    }

    public func revert(_ id: Int, turnId: String? = nil) async throws -> JSONValue {
        try await call("POST", "api/sessions/\(id)/revert", json: turnId.map { ["turn_id": .string($0)] } ?? [:])
    }

    // MARK: streaming

    /// Start a turn. Events also arrive through `events(sessionID:)`; this returns once the turn has ended.
    public func chat(_ id: Int, content: String, recipients: [Int]? = nil, attachmentIds: [Int] = []) async throws {
        var json: [String: JSONValue] = ["content": .string(content)]
        json["recipients"] = recipients.map { .array($0.map { .number(Double($0)) }) } ?? .null
        json["attachment_ids"] = .array(attachmentIds.map { .number(Double($0)) })
        let req = request("POST", "api/sessions/\(id)/chat", body: try JSONEncoder().encode(json))
        #if canImport(FoundationNetworking)
        _ = try await send(req)  // Linux: no byte streaming; the response ends with the turn
        #else
        let (bytes, response) = try await session.bytes(for: req)
        let status = (response as? HTTPURLResponse)?.statusCode ?? 0
        if !(200..<300).contains(status) {
            var body = Data()
            for try await b in bytes { body.append(b) }
            let detail = (try? JSONDecoder().decode(JSONValue.self, from: body))?["detail"]?.string ?? "error"
            throw APIError(status: status, detail: detail)
        }
        for try await _ in bytes.lines {}
        #endif
    }

    /// Follow everything that happens in a session (replays a running turn first).
    public func events(sessionID id: Int) -> AsyncThrowingStream<JSONValue, Error> {
        let req = request("GET", "api/sessions/\(id)/events")
        return AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    #if canImport(FoundationNetworking)
                    continuation.finish(throwing: APIError(status: 0, detail: "event streaming needs Apple platforms"))
                    #else
                    var stream = req
                    stream.timeoutInterval = 24 * 3600
                    let (bytes, response) = try await session.bytes(for: stream)
                    let status = (response as? HTTPURLResponse)?.statusCode ?? 0
                    guard (200..<300).contains(status) else { throw APIError(status: status, detail: "cannot follow session") }
                    let parser = SSEParser()
                    for try await line in bytes.lines {
                        if let event = parser.event(fromLine: Substring(line)) { continuation.yield(event) }
                    }
                    continuation.finish()
                    #endif
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in task.cancel() }
        }
    }
}
