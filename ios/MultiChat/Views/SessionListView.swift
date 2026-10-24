import MultiChatKit
import SwiftUI

struct SessionListView: View {
    @Environment(AppModel.self) private var app
    @State private var showNew = false
    @State private var showMemory = false
    @State private var path: [Int] = []

    init() {}

    var body: some View {
        NavigationStack(path: $path) { list }
    }

    private var list: some View {
        List {
            if app.sessions.isEmpty && !app.loading {
                ContentUnavailableView("No sessions yet", systemImage: "bubble.left.and.bubble.right",
                                       description: Text("Start one and pick the models that should answer."))
            }
            ForEach(app.sessions) { s in
                NavigationLink(value: s.id) { SessionRow(session: s) }
            }
            .onDelete { offsets in
                let doomed = offsets.map { app.sessions[$0] }
                Task { for s in doomed { await app.delete(s) } }
            }
        }
        .navigationTitle("Sessions")
        .navigationDestination(for: Int.self) { id in SessionScreen(sessionId: id) }
        .refreshable { await app.refresh() }
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { showNew = true } label: { Image(systemName: "square.and.pencil") }
            }
            ToolbarItem(placement: .secondaryAction) {
                Button { showMemory = true } label: { Label("Memory", systemImage: "brain") }
            }
        }
        .sheet(isPresented: $showMemory) { MemorySheet() }
        .sheet(isPresented: $showNew) {
            NewSessionSheet { session in
                showNew = false
                if let session { path.append(session.id) }
            }
        }
        .task { await app.refresh() }
    }
}

struct SessionRow: View {
    let session: Session

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(session.title).font(.headline).lineLimit(1)
                Spacer()
                if let cost = session.cost {
                    Text(money(cost)).font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                }
                PhaseBadge(build: session.isBuild)
            }
            HStack(spacing: 6) {
                ForEach(session.participants, id: \.modelId) { p in
                    Circle().fill(Color(hex: p.color)).frame(width: 8, height: 8)
                }
                Text(session.participants.map(\.displayName).joined(separator: ", "))
                    .font(.caption).foregroundStyle(.secondary).lineLimit(1)
            }
            Text("\(session.contextMode) · \(session.workspaceLabel)")
                .font(.caption2).foregroundStyle(.tertiary).lineLimit(1)
        }
        .padding(.vertical, 2)
    }
}

struct PhaseBadge: View {
    let build: Bool

    var body: some View {
        Text(build ? "BUILD" : "PLAN")
            .font(.caption2.bold())
            .padding(.horizontal, 6).padding(.vertical, 2)
            .background(build ? Color.purple : Color.orange, in: Capsule())
            .foregroundStyle(build ? .white : .black)
    }
}

struct NewSessionSheet: View {
    @Environment(AppModel.self) private var app
    let done: (Session?) -> Void
    @State private var title = ""
    @State private var selected: Set<Int> = []
    @State private var shared = false
    @State private var query = ""
    @State private var largeContext = false  // the model's maximum instead of 256K, for models over 512K

    var body: some View {
        NavigationStack {
            Form {
                Section("Title") { TextField("What are you working on?", text: $title) }
                Section {
                    Toggle("Models see each other's replies", isOn: $shared)
                } footer: {
                    Text(shared ? "Shared: models answer in parallel and see each other's earlier replies (Relay, in the session menu, makes them take turns)."
                                : "Isolated: models answer independently, in parallel.")
                }
                let big = app.models.filter { selected.contains($0.id) && $0.contextChoices.count > 1 }
                if !big.isEmpty {
                    Section {
                        Picker("Context window", selection: $largeContext) {
                            Text("256K").tag(false)
                            Text("Max").tag(true)
                        }
                        .pickerStyle(.segmented)
                        ForEach(big) { m in
                            LabeledContent(m.displayName, value: largeContext
                                ? "\(contextLabel(m.contextLength)) (\((m.contextLength ?? 0).formatted()) tokens)" : "256K")
                                .font(.caption)
                        }
                    } footer: {
                        Text("For models over 512K. Other models use their maximum. Chosen now; it can't be changed later in this session.")
                    }
                }
                ModelPicker(models: app.models, selected: $selected, query: query)
            }
            .searchable(text: $query, prompt: "Search models")
            .refreshable { await app.checkAvailability() }
            .navigationTitle("New session")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { done(nil) } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Start") {
                        Task {
                            let ordered = app.models.map(\.id).filter { selected.contains($0) }
                            var windows: [Int: Int] = [:]
                            for m in app.models where selected.contains(m.id) && m.contextChoices.count > 1 {
                                windows[m.id] = largeContext ? m.contextChoices[1] : m.contextChoices[0]
                            }
                            let s = await app.newSession(title: title.isEmpty ? "New session" : title, modelIds: ordered,
                                                         contextMode: shared ? "shared" : "isolated", contextWindows: windows)
                            done(s)
                        }
                    }
                    .disabled(selected.isEmpty)
                }
            }
        }
    }
}

/// Models grouped by endpoint, with toggles. Models the server can't use right now (no or rejected key, not offered by
/// the vendor) are greyed out and can't be switched on; one that is already selected can still be switched off.
struct ModelPicker: View {
    let models: [ModelInfo]
    @Binding var selected: Set<Int>
    var query: String = ""

    private func matches(_ m: ModelInfo) -> Bool {
        let words = query.lowercased().split(separator: " ")
        let text = "\(m.displayName) \(m.modelName) \(m.endpointName)".lowercased()
        return words.allSatisfy { text.contains($0) }
    }

    var body: some View {
        let shown = models.filter { ($0.enabled || selected.contains($0.id)) && matches($0) }
        let groups = Dictionary(grouping: shown, by: \.endpointName)
        let order = groups.keys.sorted { a, b in
            let ua = groups[a]!.contains(where: \.available), ub = groups[b]!.contains(where: \.available)
            return ua != ub ? ua : a < b
        }
        if shown.isEmpty {
            Section { Text(query.isEmpty ? "No models configured on the server." : "No models match “\(query)”.").foregroundStyle(.secondary) }
        }
        ForEach(order, id: \.self) { endpoint in
            let items = (groups[endpoint] ?? []).sorted { a, b in a.available != b.available ? a.available : a.displayName < b.displayName }
            Section {
                ForEach(items) { m in row(m) }
            } header: {
                Text("\(endpoint) · \(items.filter(\.available).count)/\(items.count) available")
            }
        }
    }

    private func row(_ m: ModelInfo) -> some View {
        let usable = m.available || selected.contains(m.id)
        return Toggle(isOn: Binding(
            get: { selected.contains(m.id) },
            set: { on in if on { selected.insert(m.id) } else { selected.remove(m.id) } }
        )) {
            HStack {
                Circle().fill(m.available ? Color(hex: m.color) : Color.gray.opacity(0.5)).frame(width: 10, height: 10)
                VStack(alignment: .leading) {
                    Text(m.displayName).foregroundStyle(m.available ? .primary : .secondary)
                    if m.available {
                        Text(m.endpointKind == "agent_cli" ? "agent CLI · needs a folder" : "\(money(m.inputPrice)) / \(money(m.outputPrice)) per 1M")
                            .font(.caption).foregroundStyle(.secondary)
                    } else {
                        Text("Unavailable: \(m.availabilityDetail.isEmpty ? "not usable right now" : m.availabilityDetail)")
                            .font(.caption).foregroundStyle(.tertiary).lineLimit(2)
                    }
                }
            }
        }
        .disabled(!usable)
    }
}

/// The global memory every model gets in every session (like Claude Code's CLAUDE.md).
struct MemorySheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    @State private var text = ""
    @State private var loaded = false
    @State private var error: String?

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextEditor(text: $text).frame(minHeight: 220).font(.callout.monospaced())
                } footer: {
                    Text("Every model gets this in every session. In the desktop app and the terminal, a message starting with # is saved here. A CLAUDE.md, AGENTS.md or MULTICHAT.md in a session's folder is read too.")
                }
                if let error { Section { Text(error).foregroundStyle(.red) } }
            }
            .navigationTitle("Memory")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Save") {
                        Task {
                            do {
                                _ = try await app.api?.setMemory(text)
                                dismiss()
                            } catch {
                                self.error = error.localizedDescription
                            }
                        }
                    }
                    .disabled(!loaded)
                }
            }
            .task {
                guard !loaded, let api = app.api else { return }
                text = (try? await api.memory().text) ?? ""
                loaded = true
            }
        }
    }
}
