import Foundation
import XCTest
@testable import MultiChatKit

final class MultiChatKitTests: XCTestCase {
    let sessionJSON = """
    {"id": 3, "title": "Fix login", "workspace_kind": "runner", "runner_id": 1, "ssh_connection_id": null,
     "workspace_path": "/w", "workspace_label": "pc:/w", "phase": "plan", "context_mode": "shared",
     "builder_model_id": null, "approve_edits": "auto", "approve_commands": "ask",
     "created_at": "2026-10-24T00:44:47.463000+00:00", "updated_at": "2026-10-24T00:44:47.463000+00:00",
     "participants": [{"model_id": 7, "display_name": "Alpha", "color": "#2563eb", "endpoint_kind": "mock", "position": 0},
                      {"model_id": 8, "display_name": "Beta", "color": "#d97706", "endpoint_kind": "mock", "position": 1}],
     "busy": false}
    """

    let modelsJSON = """
    [{"id": 7, "endpoint_id": 1, "endpoint_name": "demo", "endpoint_kind": "mock", "model_name": "mock-alpha",
      "display_name": "Alpha", "color": "#2563eb", "input_price": 1.0, "output_price": 2.0, "enabled": true,
      "supports_tools": true, "source": "config"},
     {"id": 8, "endpoint_id": 1, "endpoint_name": "demo", "endpoint_kind": "mock", "model_name": "mock-beta",
      "display_name": "Beta", "color": "#d97706", "input_price": 3.0, "output_price": 15.0, "enabled": true,
      "supports_tools": true, "source": "config"}]
    """

    func decode<T: Decodable>(_ type: T.Type, _ json: String) throws -> T {
        try JSONCoding.decoder.decode(T.self, from: Data(json.utf8))
    }

    func event(_ json: String) throws -> JSONValue {
        try JSONDecoder().decode(JSONValue.self, from: Data(json.utf8))
    }

    func testDecodesServerModels() throws {
        let s = try decode(Session.self, sessionJSON)
        XCTAssertEqual(s.participants.map(\.displayName), ["Alpha", "Beta"])
        XCTAssertEqual(s.workspaceLabel, "pc:/w")
        let models = try decode([ModelInfo].self, modelsJSON)
        XCTAssertEqual(models[1].outputPrice, 15.0)
        let msg = try decode(Message.self, """
        {"id": 5, "session_id": 3, "seq": 2, "turn_id": "t1", "role": "assistant", "model_id": 7, "phase": "plan",
         "content": "hi", "tool_calls": [{"id": "c1", "name": "read_file", "arguments": {"path": "a.py"}, "external": true}],
         "tool_call_id": null, "recipients": null, "status": "complete", "created_at": "x", "vote": 1}
        """)
        XCTAssertEqual(msg.toolCalls?.first?.arguments?.argumentSummary, "a.py")
        XCTAssertEqual(msg.vote, 1)
    }

    func testDecodesAvailabilityAndAttachments() throws {
        let models = try decode([ModelInfo].self, """
        [{"id": 9, "endpoint_id": 2, "endpoint_name": "openrouter", "endpoint_kind": "openai_compatible", "model_name": "x/y",
          "display_name": "Y", "color": "#000000", "input_price": 0, "output_price": 0, "enabled": true, "supports_tools": true,
          "source": "catalog", "available": false, "availability_detail": "openrouter: OpenRouter rejected the API key",
          "supports_images": false}]
        """)
        XCTAssertFalse(models[0].available)
        XCTAssertEqual(models[0].availabilityDetail, "openrouter: OpenRouter rejected the API key")
        XCTAssertTrue(try decode([ModelInfo].self, modelsJSON)[0].available, "older servers: available by default")
        let msg = try decode(Message.self, """
        {"id": 1, "seq": 1, "turn_id": "t", "role": "user", "content": "look",
         "attachments": [{"id": 4, "filename": "shot.png", "mime": "image/png", "size": 1200, "kind": "image"},
                         {"id": 5, "filename": "spec.pdf", "mime": "application/pdf", "size": 9000, "kind": "document"}]}
        """)
        XCTAssertEqual(msg.attachments.map(\.isImage), [true, false])
        let ep = try decode(Endpoint.self, """
        {"id": 2, "name": "openrouter", "kind": "openai_compatible", "base_url": "https://openrouter.ai/api/v1",
         "api_key_masked": "sk-…f3", "source": "config", "enabled": true, "status": "ok", "status_detail": "312 models",
         "checked_at": "2026-10-24T10:00:00Z", "auto_models": true}
        """)
        XCTAssertEqual(ep.status, "ok")

        let big = try decode([ModelInfo].self, """
        [{"id": 1, "endpoint_id": 1, "endpoint_name": "or", "endpoint_kind": "openai_compatible", "model_name": "x",
          "display_name": "X", "color": "#000000", "input_price": 1, "output_price": 2, "enabled": true,
          "supports_tools": true, "source": "catalog", "context_length": 1000000, "supports_effort": true}]
        """)[0]
        XCTAssertEqual(big.contextChoices, [256_000, 1_000_000])
        XCTAssertTrue(big.supportsEffort)
        XCTAssertEqual(contextLabel(256_000), "256K")
        XCTAssertEqual(contextLabel(1_048_576), "1M")
        XCTAssertEqual(contextLabel(262_144), "256K")
        XCTAssertEqual(contextLabel(2_000_000), "2M")
        XCTAssertEqual(contextLabel(128_000), "128K")
        XCTAssertEqual(contextLabel(256_000), "256K")
        let p = try decode(Participant.self, """
        {"model_id": 1, "display_name": "X", "color": "#000", "endpoint_kind": "mock", "position": 0,
         "context_window": 256000, "context_length": 1000000, "effort": "high", "supports_effort": true}
        """)
        XCTAssertEqual(p.effort, "high")
        XCTAssertEqual(p.contextWindow, 256_000)
        XCTAssertEqual(Effort.label("xhigh"), "X-High")
    }

    func testSSEParser() {
        let body = ": connected\n\ndata: {\"type\": \"turn_start\", \"seq\": 1}\n\ndata: {\"type\": \"delta\", \"text\": \"a b\"}\n\n"
        let events = SSEParser().events(in: body)
        XCTAssertEqual(events.map { $0["type"]?.string }, ["turn_start", "delta"])
        XCTAssertEqual(events[1]["text"]?.string, "a b")
    }

    func testReducerStreamsVotesUsageApprovals() throws {
        var st = SessionState(
            session: try decode(Session.self, sessionJSON),
            messages: [try decode(Message.self, #"{"id": 1, "seq": 1, "turn_id": "t", "role": "user", "content": "q", "recipients": [7]}"#)],
            usage: [],
            models: try decode([ModelInfo].self, modelsJSON)
        )
        st.apply(try event(#"{"type": "turn_start"}"#))
        st.apply(try event(#"{"type": "message_start", "message": {"id": 2, "seq": 2, "turn_id": "t", "role": "assistant", "model_id": 7, "content": "", "status": "streaming"}}"#))
        st.apply(try event(#"{"type": "delta", "message_id": 2, "text": "Hel"}"#))
        st.apply(try event(#"{"type": "delta", "message_id": 2, "text": "lo"}"#))
        st.apply(try event(#"{"type": "tool_call", "message_id": 2, "call": {"id": "c1", "name": "run_command", "arguments": {"command": "ls"}}}"#))
        st.apply(try event(#"{"type": "tool_progress", "call_id": "c1", "text": "a.txt\n"}"#))
        st.apply(try event(#"{"type": "approval_required", "call_id": "c2", "kind": "edit", "model_id": 7, "path": "a.py", "diff": "+x"}"#))
        XCTAssertEqual(st.messages[2]?.content, "Hello")
        XCTAssertTrue(st.busy)
        XCTAssertEqual(st.approvals["c2"]?.path, "a.py")
        XCTAssertEqual(st.progress["c1"], "a.txt\n")
        st.apply(try event(#"{"type": "vote", "message_id": 2, "value": 1}"#))
        st.apply(try event(#"{"type": "message_end", "message": {"id": 2, "seq": 2, "turn_id": "t", "role": "assistant", "model_id": 7, "content": "Hello", "status": "complete"}}"#))
        st.apply(try event(#"{"type": "tool_result", "call_id": "c1", "message": {"id": 3, "seq": 3, "turn_id": "t", "role": "tool", "model_id": 7, "content": "a.txt", "tool_call_id": "c1"}}"#))
        st.apply(try event(#"{"type": "usage", "message_id": 2, "model_id": 7, "input_tokens": 10, "output_tokens": 5, "cost": 0.25, "estimated": false}"#))
        st.apply(try event(#"{"type": "approval_resolved", "call_id": "c2"}"#))
        st.apply(try event(#"{"type": "turn_end", "status": "complete"}"#))
        XCTAssertEqual(st.messages[2]?.vote, 1, "vote survives message_end")
        XCTAssertEqual(st.results["c1"]?.content, "a.txt")
        XCTAssertEqual(st.usage[7]?.tokens, 15)
        XCTAssertEqual(st.messages[2]?.usage?.cost, 0.25, "per-reply cost")
        XCTAssertEqual(st.messages[2]?.usage?.tokens, 15)
        XCTAssertEqual(st.totalCost, 0.25)
        XCTAssertTrue(st.approvals.isEmpty)
        XCTAssertFalse(st.busy)
        XCTAssertEqual(st.visible().map(\.id), [1, 2], "tool results are shown inside replies")
        XCTAssertEqual(st.visible(for: 8).map(\.id), [], "Beta's tab: the prompt was not addressed to it")
        XCTAssertEqual(st.name(7), "Alpha")
    }

    func testCursorFamilyChoices() throws {
        let m = try decode(ModelInfo.self, """
        {"id": 9, "endpoint_id": 2, "endpoint_name": "cursor", "endpoint_kind": "agent_cli", "model_name": "claude-opus-4-8",
         "display_name": "Cursor · Opus 4.8", "color": "#111827", "input_price": 0, "output_price": 0, "enabled": true,
         "supports_tools": true, "source": "catalog", "context_length": 1000000, "supports_effort": true,
         "effort_levels": ["medium", "high"], "context_choices": [200000, 1000000]}
        """)
        XCTAssertEqual(m.contextChoices, [200_000, 1_000_000], "Cursor's own sizes, not 256K / max")
        XCTAssertEqual(m.effortLevels, ["medium", "high"])
        let p = try decode(Participant.self, """
        {"model_id": 9, "display_name": "Cursor · Opus 4.8", "color": "#111827", "endpoint_kind": "agent_cli", "position": 0,
         "supports_effort": true, "effort_levels": ["medium", "high"], "context_choices": [200000, 1000000]}
        """)
        XCTAssertEqual(p.efforts, ["medium", "high"])
        let plain = try decode(Participant.self, ##"{"model_id": 1, "display_name": "A", "color": "#000", "endpoint_kind": "mock", "position": 0}"##)
        XCTAssertEqual(plain.efforts, Effort.levels)
    }

    func testWaitTips() throws {
        var st = SessionState(session: try decode(Session.self, sessionJSON), messages: [], usage: [],
                              models: try decode([ModelInfo].self, modelsJSON))
        st.apply(try event(#"{"type": "turn_start"}"#))
        st.apply(try event(#"{"type": "status", "model_id": 8, "state": "tool", "text": "running `ls` on pc", "hint": "Stop cancels it."}"#))
        st.apply(try event(#"{"type": "status", "model_id": 7, "state": "approval", "text": "waiting for your approval to run `rm x`"}"#))
        let start = st.status[8]!.since
        XCTAssertEqual(st.waits(now: start).map(\.name), ["Alpha"], "approvals show at once")
        let later = st.waits(now: start.addingTimeInterval(12))
        XCTAssertEqual(later.map(\.name), ["Alpha", "Beta"], "participant order")
        XCTAssertEqual(later[1].line, "Beta: running `ls` on pc · 12 s")
        XCTAssertEqual(later[1].hint, "Stop cancels it.")
        st.apply(try event(#"{"type": "status", "model_id": 8, "state": "tool", "text": "running `ls -la` on pc"}"#))
        XCTAssertEqual(st.status[8]?.since, start, "same state keeps its start time")
        st.apply(try event(#"{"type": "status", "model_id": 7, "state": "writing", "text": "writing its answer"}"#))
        let last = st.status[7]!.last
        XCTAssertTrue(st.waits(now: last.addingTimeInterval(5)).allSatisfy { $0.modelId != 7 })
        XCTAssertEqual(st.waits(now: last.addingTimeInterval(25)).first?.line, "Alpha: no new output · 25 s")
        st.apply(try event(#"{"type": "status", "model_id": 8, "state": null}"#))
        XCTAssertNil(st.status[8])
        st.apply(try event(#"{"type": "turn_end", "status": "complete"}"#))
        XCTAssertTrue(st.status.isEmpty)
        XCTAssertEqual(Wait.elapsed(125), "2 min 05 s")
    }

    /// Talks to a real server when MULTICHAT_TEST_URL and MULTICHAT_TEST_TOKEN are set.
    func testLiveServerRoundTrip() async throws {
        let env = ProcessInfo.processInfo.environment
        guard let url = env["MULTICHAT_TEST_URL"].flatMap(URL.init(string:)), let token = env["MULTICHAT_TEST_TOKEN"] else {
            throw XCTSkip("set MULTICHAT_TEST_URL and MULTICHAT_TEST_TOKEN")
        }
        let api = APIClient(baseURL: url, token: token)
        let models = try await api.models()
        let wanted = (env["MULTICHAT_TEST_MODELS"] ?? "").split(separator: ",").map(String.init)
        let ids = models.filter { wanted.isEmpty ? $0.endpointKind == "mock" : wanted.contains($0.modelName) }.prefix(3).map(\.id)
        XCTAssertFalse(ids.isEmpty)
        let s = try await api.createSession(title: "Swift client test", modelIds: Array(ids), contextMode: "shared")
        try await api.chat(s.id, content: "Reply with one short sentence about Swift.")
        let messages = try await api.messages(s.id)
        let replies = messages.filter(\.isReply)
        XCTAssertEqual(Set(replies.compactMap(\.modelId)), Set(ids))
        try await api.vote(messageId: replies[0].id, value: 1)
        let after = try await api.messages(s.id)
        XCTAssertEqual(after.first { $0.id == replies[0].id }?.vote, 1)
        let built = try await api.pickBuilder(s.id, modelId: ids[0])
        XCTAssertTrue(built.isBuild)
        let usage = try await api.usage(s.id)
        XCTAssertEqual(usage.count, ids.count)
        let costed = try await api.session(s.id)
        XCTAssertNotNil(costed.cost)  // the test server's mock models may be free
        XCTAssertGreaterThan(costed.tokens ?? 0, 0)
        XCTAssertNotNil(after.first(where: \.isReply)?.usage)
        let stats = try await api.stats()
        XCTAssertGreaterThan(stats.totals.inputTokens, 0)
        let state = SessionState(session: built, messages: after, usage: usage, models: models)
        XCTAssertEqual(state.visible().count, replies.count + 1)
        print("live replies:", replies.map { "\(state.name($0.modelId)): \($0.content.prefix(80))" })

        // attachments: upload a text document and send it with a message
        let doc = try await api.uploadAttachment(s.id, filename: "notes.md", data: Data("# Notes\nSwift 6\n".utf8), mime: "text/markdown")
        XCTAssertEqual(doc.kind, "document")
        try await api.chat(s.id, content: "Summarise the notes", attachmentIds: [doc.id])
        let withAttachment = try await api.messages(s.id)
        XCTAssertEqual(withAttachment.last { $0.role == "user" }?.attachments.map(\.filename), ["notes.md"])
        let downloaded = try await api.attachmentData(doc.id)
        XCTAssertEqual(downloaded, Data("# Notes\nSwift 6\n".utf8))

        // memory and manual compaction
        _ = try await api.setMemory("- answer briefly")
        let mem = try await api.memory()
        XCTAssertEqual(mem.text, "- answer briefly")
        _ = try await api.setMemory("")
        try await api.compact(s.id)
        let afterCompact = try await api.messages(s.id)
        XCTAssertTrue(afterCompact.contains { $0.isSummary })

        // effort per model, any time
        if let id = ids.first, models.first(where: { $0.id == id })?.supportsEffort == true {
            let updated = try await api.setEffort(s.id, modelId: id, effort: "high")
            XCTAssertEqual(updated.participants.first { $0.modelId == id }?.effort, "high")
        }

        // reuse_empty: a fresh session is reused instead of piling up blank ones
        let blank = try await api.createSession(title: "blank", modelIds: Array(ids), reuseEmpty: true)
        let again = try await api.createSession(title: "blank again", modelIds: Array(ids), reuseEmpty: true)
        XCTAssertEqual(blank.id, again.id)
        XCTAssertEqual(again.title, "blank again")
        try await api.deleteSession(blank.id)
        try await api.deleteSession(s.id)
    }
}
