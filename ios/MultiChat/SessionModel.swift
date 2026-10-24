import Foundation
import MultiChatKit
import Observation

/// One open session: loads history from the server, follows live events, and performs actions.
@MainActor
@Observable
final class SessionModel {
    let api: APIClient
    let sessionId: Int
    var state = SessionState()
    var tabModel: Int?  // nil = All
    var recipients: Set<Int> = []  // empty = everyone
    var draft = ""
    var error: String?
    var loaded = false
    var scrollTick = 0  // bumped on new content so the chat can follow the bottom
    private var followTask: Task<Void, Never>?

    init(api: APIClient, sessionId: Int) {
        self.api = api
        self.sessionId = sessionId
    }

    var session: Session? { state.session }
    var isBuild: Bool { session?.isBuild ?? false }
    var visible: [Message] { state.visible(for: tabModel) }
    var pendingApprovals: [Approval] { state.approvals.values.sorted { $0.callId < $1.callId } }

    func load(models: [ModelInfo]) async {
        do {
            async let s = api.session(sessionId)
            async let m = api.messages(sessionId)
            async let u = api.usage(sessionId)
            let (session, messages, usage) = try await (s, m, u)
            let allModels = models.isEmpty ? try await api.models() : models
            state = SessionState(session: session, messages: messages, usage: usage, models: allModels)
            loaded = true
            scrollTick += 1
            follow()
        } catch {
            self.error = error.localizedDescription
        }
    }

    func follow() {
        followTask?.cancel()
        followTask = Task { [weak self] in
            while !Task.isCancelled {
                guard let self else { return }
                do {
                    for try await event in self.api.events(sessionID: self.sessionId) {
                        let type = self.state.apply(event)
                        if ["message_start", "message_end", "delta", "tool_result", "tool_call", "approval_required"].contains(type) {
                            self.scrollTick += 1
                        }
                    }
                } catch {
                    // network hiccup or app returning from background: reconnect
                }
                try? await Task.sleep(for: .seconds(2))
            }
        }
    }

    func stopFollowing() {
        followTask?.cancel()
        followTask = nil
    }

    private func run(_ work: @escaping () async throws -> Void) {
        Task {
            do { try await work() } catch { self.error = error.localizedDescription }
        }
    }

    private func updateSession(_ s: Session) {
        state.session = s
    }

    // MARK: actions

    func send() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !text.isEmpty else { return }
        draft = ""
        let to = recipients.isEmpty || isBuild ? nil : Array(recipients)
        run { try await self.api.chat(self.sessionId, content: text, recipients: to) }
    }

    func vote(_ message: Message, _ value: Int) {
        let newValue = message.vote == value ? 0 : value
        state.messages[message.id]?.vote = newValue
        run { try await self.api.vote(messageId: message.id, value: newValue) }
    }

    func pickBuilder(_ modelId: Int) {
        run { self.updateSession(try await self.api.pickBuilder(self.sessionId, modelId: modelId)) }
    }

    func setPhase(_ phase: String) {
        run { self.updateSession(try await self.api.updateSession(self.sessionId, ["phase": .string(phase)])) }
    }

    func setMode(_ mode: String) {
        run { self.updateSession(try await self.api.updateSession(self.sessionId, ["context_mode": .string(mode)])) }
    }

    func setApproval(edits: String? = nil, commands: String? = nil) {
        var fields: [String: JSONValue] = [:]
        if let edits { fields["approve_edits"] = .string(edits) }
        if let commands { fields["approve_commands"] = .string(commands) }
        run { self.updateSession(try await self.api.updateSession(self.sessionId, fields)) }
    }

    func rename(_ title: String) {
        run { self.updateSession(try await self.api.updateSession(self.sessionId, ["title": .string(title)])) }
    }

    func setModels(_ ids: [Int]) {
        run { self.updateSession(try await self.api.setModels(self.sessionId, modelIds: ids)) }
    }

    func setWorkspace(kind: String, path: String?, runnerId: Int?, sshId: Int?) {
        run {
            self.updateSession(try await self.api.setWorkspace(self.sessionId, kind: kind, path: path, runnerId: runnerId, sshConnectionId: sshId))
        }
    }

    func approve(_ approval: Approval, _ ok: Bool) {
        state.approvals[approval.callId] = nil
        run { try await self.api.approve(self.sessionId, callId: approval.callId, approve: ok) }
    }

    func stop() {
        run { try await self.api.stop(self.sessionId) }
    }

    func revertLastTurn() {
        run { _ = try await self.api.revert(self.sessionId) }
    }
}
