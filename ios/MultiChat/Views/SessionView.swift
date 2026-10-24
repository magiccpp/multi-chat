import MultiChatKit
import SwiftUI

/// Creates the session model and shows the chat once loaded.
struct SessionScreen: View {
    @Environment(AppModel.self) private var app
    let sessionId: Int
    @State private var model: SessionModel?

    init(sessionId: Int) {
        self.sessionId = sessionId
    }

    var body: some View {
        Group {
            if let model, model.loaded {
                SessionView(model: model)
            } else if let error = model?.error {
                ContentUnavailableView("Could not open the session", systemImage: "exclamationmark.triangle", description: Text(error))
            } else {
                ProgressView()
            }
        }
        .task {
            guard model == nil, let api = app.api else { return }
            let m = SessionModel(api: api, sessionId: sessionId)
            model = m
            await m.load(models: app.models)
        }
        .onDisappear { model?.stopFollowing() }
    }
}

struct SessionView: View {
    @Bindable var model: SessionModel
    @State private var showModels = false
    @State private var showFolder = false
    @State private var reviewing: Approval?
    @State private var renaming = false
    @State private var newTitle = ""

    var body: some View {
        VStack(spacing: 0) {
            ModelTabs(model: model)
            Divider()
            ScrollViewReader { proxy in
                ScrollView {
                    LazyVStack(alignment: .leading, spacing: 10) {
                        ForEach(model.visible) { m in
                            MessageRow(model: model, message: m).id(m.id)
                        }
                        ForEach(model.pendingApprovals) { a in
                            ApprovalCard(model: model, approval: a) { reviewing = a }
                        }
                        Color.clear.frame(height: 1).id("bottom")
                    }
                    .padding(12)
                }
                .scrollDismissesKeyboard(.interactively)
                .onChange(of: model.scrollTick) {
                    withAnimation(.easeOut(duration: 0.15)) { proxy.scrollTo("bottom", anchor: .bottom) }
                }
                .onAppear { proxy.scrollTo("bottom", anchor: .bottom) }
            }
            TokenBar(state: model.state)
            Composer(model: model)
        }
        .navigationTitle(model.session?.title ?? "")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .principal) {
                VStack(spacing: 0) {
                    Text(model.session?.title ?? "").font(.headline).lineLimit(1)
                    Text(model.session?.workspaceLabel ?? "").font(.caption2).foregroundStyle(.secondary).lineLimit(1)
                }
            }
            ToolbarItem(placement: .primaryAction) { sessionMenu }
        }
        .sheet(isPresented: $showModels) { ModelsSheet(model: model) }
        .sheet(isPresented: $showFolder) { FolderSheet(model: model) }
        .sheet(item: $reviewing) { a in DiffSheet(model: model, approval: a) }
        .alert("Rename session", isPresented: $renaming) {
            TextField("Title", text: $newTitle)
            Button("Save") { model.rename(newTitle) }
            Button("Cancel", role: .cancel) {}
        }
        .alert("Error", isPresented: Binding(get: { model.error != nil }, set: { if !$0 { model.error = nil } })) {
            Button("OK") { model.error = nil }
        } message: {
            Text(model.error ?? "")
        }
    }

    private var sessionMenu: some View {
        Menu {
            Picker("Phase", selection: Binding(get: { model.session?.phase ?? "plan" }, set: { model.setPhase($0) })) {
                Label("PLAN — compare models", systemImage: "lightbulb").tag("plan")
                Label("BUILD — builder works", systemImage: "hammer").tag("build")
            }
            .disabled(model.session?.builderModelId == nil)
            Picker("Context", selection: Binding(get: { model.session?.contextMode ?? "isolated" }, set: { model.setMode($0) })) {
                Text("Isolated").tag("isolated")
                Text("Shared").tag("shared")
            }
            Menu("Builder") {
                ForEach(model.state.participants, id: \.modelId) { p in
                    Button {
                        model.pickBuilder(p.modelId)
                    } label: {
                        if model.session?.builderModelId == p.modelId { Label(p.displayName, systemImage: "star.fill") } else { Text(p.displayName) }
                    }
                }
            }
            Menu("Approvals") {
                Picker("Edits", selection: Binding(get: { model.session?.approveEdits ?? "auto" }, set: { model.setApproval(edits: $0) })) {
                    Text("Edits: auto").tag("auto")
                    Text("Edits: ask").tag("ask")
                }
                Picker("Commands", selection: Binding(get: { model.session?.approveCommands ?? "ask" }, set: { model.setApproval(commands: $0) })) {
                    Text("Commands: ask").tag("ask")
                    Text("Commands: auto").tag("auto")
                }
            }
            Divider()
            Button { showModels = true } label: { Label("Models in session", systemImage: "person.3") }
            Button { showFolder = true } label: { Label("Folder", systemImage: "folder") }
            Button { newTitle = model.session?.title ?? ""; renaming = true } label: { Label("Rename", systemImage: "pencil") }
            Button(role: .destructive) { model.revertLastTurn() } label: { Label("Revert last turn's files", systemImage: "arrow.uturn.backward") }
        } label: {
            Image(systemName: "ellipsis.circle")
        }
    }
}

/// "All" plus one tab per participating model.
struct ModelTabs: View {
    @Bindable var model: SessionModel

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                tab("All", color: .primary, selected: model.tabModel == nil) { model.tabModel = nil }
                ForEach(model.state.participants, id: \.modelId) { p in
                    tab(p.displayName + (model.session?.builderModelId == p.modelId ? " ★" : ""),
                        color: Color(hex: p.color), selected: model.tabModel == p.modelId) { model.tabModel = p.modelId }
                }
                PhaseBadge(build: model.isBuild)
            }
            .padding(.horizontal, 12).padding(.vertical, 8)
        }
    }

    private func tab(_ title: String, color: Color, selected: Bool, action: @escaping () -> Void) -> some View {
        Button(action: action) {
            Text(title)
                .font(.subheadline.weight(selected ? .semibold : .regular))
                .padding(.horizontal, 10).padding(.vertical, 5)
                .foregroundStyle(selected ? Color.white : color)
                .background(selected ? color : Color.secondary.opacity(0.12), in: Capsule())
        }
        .buttonStyle(.plain)
    }
}

struct TokenBar: View {
    let state: SessionState

    var body: some View {
        let rows = state.usage.sorted { $0.value.cost > $1.value.cost }
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 12) {
                if rows.isEmpty {
                    Text("No tokens used yet").foregroundStyle(.secondary)
                }
                ForEach(rows, id: \.key) { key, u in
                    HStack(spacing: 4) {
                        Circle().fill(Color(hex: state.color(key))).frame(width: 7, height: 7)
                        Text("\(state.name(key)) \(u.estimated ? "~" : "")\(tokenCount(u.tokens)) · \(money(u.cost))")
                    }
                }
                if !rows.isEmpty { Text("Σ \(money(state.totalCost))").bold() }
                if state.busy { ProgressView().controlSize(.mini) }
            }
            .font(.caption2.monospacedDigit())
            .padding(.horizontal, 12).padding(.vertical, 6)
        }
        .background(.bar)
    }
}

struct Composer: View {
    @Bindable var model: SessionModel
    @FocusState private var focused: Bool

    var body: some View {
        VStack(spacing: 6) {
            if !model.isBuild && model.state.participants.count > 1 {
                ScrollView(.horizontal, showsIndicators: false) {
                    HStack(spacing: 6) {
                        Text("To").font(.caption).foregroundStyle(.secondary)
                        ForEach(model.state.participants, id: \.modelId) { p in
                            let on = model.recipients.isEmpty || model.recipients.contains(p.modelId)
                            Button {
                                var set = model.recipients.isEmpty ? Set(model.state.participants.map(\.modelId)) : model.recipients
                                if set.contains(p.modelId) { set.remove(p.modelId) } else { set.insert(p.modelId) }
                                model.recipients = set.count == model.state.participants.count ? [] : set
                            } label: {
                                Text(p.displayName).font(.caption)
                                    .padding(.horizontal, 8).padding(.vertical, 3)
                                    .background(on ? Color(hex: p.color).opacity(0.85) : Color.secondary.opacity(0.15), in: Capsule())
                                    .foregroundStyle(on ? .white : .secondary)
                            }
                            .buttonStyle(.plain)
                        }
                    }
                }
            }
            HStack(alignment: .bottom, spacing: 8) {
                TextField(model.isBuild ? "Tell the builder what to do…" : "Message the models…", text: $model.draft, axis: .vertical)
                    .lineLimit(1...6)
                    .focused($focused)
                    .padding(8)
                    .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 10))
                if model.state.busy {
                    Button { model.stop() } label: { Image(systemName: "stop.circle.fill").font(.title2) }
                        .tint(.red)
                } else {
                    Button { model.send() } label: { Image(systemName: "arrow.up.circle.fill").font(.title2) }
                        .disabled(model.draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)
                }
            }
        }
        .padding(.horizontal, 12).padding(.vertical, 8)
        .background(.bar)
    }
}
