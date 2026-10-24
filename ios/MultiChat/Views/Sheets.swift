import MultiChatKit
import SwiftUI

/// Which models take part in this session (join/leave any time).
struct ModelsSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let model: SessionModel
    @State private var selected: Set<Int> = []
    @State private var query = ""

    init(model: SessionModel) {
        self.model = model
    }

    var body: some View {
        NavigationStack {
            Form { ModelPicker(models: app.models, selected: $selected, query: query) }
                .searchable(text: $query, prompt: "Search models")
                .refreshable { await app.checkAvailability() }
                .navigationTitle("Models in session")
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                    ToolbarItem(placement: .confirmationAction) {
                        Button("Save") {
                            let current = model.state.participants.map(\.modelId)
                            let kept = current.filter { selected.contains($0) }
                            let added = app.models.map(\.id).filter { selected.contains($0) && !current.contains($0) }
                            model.setModels(kept + added)
                            dismiss()
                        }
                    }
                }
                .onAppear { selected = Set(model.state.participants.map(\.modelId)) }
                .task { await app.reloadModels() }
        }
    }
}

/// Attach a folder: none, a runner (desktop app / TUI on a computer), or an SSH host. The phone itself runs nothing.
struct FolderSheet: View {
    @Environment(AppModel.self) private var app
    @Environment(\.dismiss) private var dismiss
    let model: SessionModel
    @State private var kind = "none"
    @State private var runners: [RunnerInfo] = []
    @State private var ssh: [SshConnection] = []
    @State private var targetId: Int?
    @State private var browse: BrowseResult?
    @State private var path = ""
    @State private var error: String?

    init(model: SessionModel) {
        self.model = model
    }

    var body: some View {
        NavigationStack {
            Form {
                Picker("Where", selection: $kind) {
                    Text("No folder").tag("none")
                    Text("Runner").tag("runner")
                    Text("SSH host").tag("ssh")
                }
                .pickerStyle(.segmented)

                if kind == "runner" {
                    Section("Runner") {
                        if runners.isEmpty { Text("No runners. Turn on the runner in the desktop app or run `multichat runner`.").foregroundStyle(.secondary) }
                        ForEach(runners) { r in
                            targetRow(id: r.id, title: r.name, subtitle: "\(r.os) · \(r.online ? "online" : "offline")", enabled: r.online)
                        }
                    }
                } else if kind == "ssh" {
                    Section("SSH host") {
                        if ssh.isEmpty { Text("Add SSH connections in config.yaml or the desktop app.").foregroundStyle(.secondary) }
                        ForEach(ssh) { c in
                            targetRow(id: c.id, title: c.name, subtitle: "\(c.username)@\(c.host)" + (c.hostKeyFingerprint == nil ? " · host key not trusted yet" : ""),
                                      enabled: c.hostKeyFingerprint != nil)
                        }
                    }
                }

                if kind != "none", let browse {
                    Section(browse.path.isEmpty ? "Allowed folders" : browse.path) {
                        if !browse.path.isEmpty {
                            Button { open(parent(of: browse.path)) } label: { Label("..", systemImage: "arrow.up") }
                        }
                        ForEach(browse.entries, id: \.name) { e in
                            Button { open(join(browse.path, e.name)) } label: { Label(e.name, systemImage: "folder") }
                        }
                    }
                    Section("Folder") { TextField("path", text: $path).textInputAutocapitalization(.never).autocorrectionDisabled() }
                }
                if let error { Section { Text(error).foregroundStyle(.red) } }
            }
            .navigationTitle("Folder")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() } }
                ToolbarItem(placement: .confirmationAction) {
                    Button("Use") {
                        model.setWorkspace(kind: kind, path: kind == "none" ? nil : path,
                                           runnerId: kind == "runner" ? targetId : nil, sshId: kind == "ssh" ? targetId : nil)
                        dismiss()
                    }
                    .disabled(kind != "none" && (targetId == nil || path.isEmpty))
                }
            }
            .task { await load() }
            .onChange(of: kind) { targetId = nil; browse = nil; path = "" }
        }
    }

    private func targetRow(id: Int, title: String, subtitle: String, enabled: Bool) -> some View {
        Button {
            targetId = id
            open("")
        } label: {
            HStack {
                VStack(alignment: .leading) {
                    Text(title)
                    Text(subtitle).font(.caption).foregroundStyle(.secondary)
                }
                Spacer()
                if targetId == id { Image(systemName: "checkmark") }
            }
        }
        .disabled(!enabled)
    }

    private func load() async {
        guard let api = app.api else { return }
        let s = model.session
        kind = s?.workspaceKind ?? "none"
        targetId = s?.runnerId ?? s?.sshConnectionId
        path = s?.workspacePath ?? ""
        runners = (try? await api.runners()) ?? []
        ssh = (try? await api.sshConnections()) ?? []
    }

    private func open(_ newPath: String) {
        guard let api = app.api, let id = targetId else { return }
        Task {
            do {
                browse = kind == "runner" ? try await api.browseRunner(id, path: newPath) : try await api.browseSSH(id, path: newPath)
                if let b = browse, !b.path.isEmpty { path = b.path }
                error = nil
            } catch {
                self.error = error.localizedDescription
            }
        }
    }

    private func join(_ base: String, _ name: String) -> String {
        if base.isEmpty || name.hasPrefix("/") || name.dropFirst().hasPrefix(":") { return name }
        let sep = base.contains("\\") ? "\\" : "/"
        return base.hasSuffix(sep) ? base + name : base + sep + name
    }

    private func parent(of path: String) -> String {
        let sep: Character = path.contains("\\") ? "\\" : "/"
        let trimmed = path.hasSuffix(String(sep)) ? String(path.dropLast()) : path
        guard let idx = trimmed.lastIndex(of: sep) else { return "" }
        let p = String(trimmed[..<idx])
        return p.isEmpty ? String(sep) : p
    }
}

struct SettingsView: View {
    @Environment(AppModel.self) private var app

    init() {}

    var body: some View {
        Form {
            Section("Server") {
                LabeledContent("URL", value: app.serverURL)
                NavigationLink("Endpoints and models") { EndpointsView() }
            }
            Section {
                Button("Sign out", role: .destructive) { app.signOut() }
            } footer: {
                Text("Endpoints, API keys and prices are configured on the server (config.yaml or the desktop app). "
                     + "Sessions and chats are stored on the server and shared by all your devices.")
            }
        }
        .navigationTitle("Settings")
    }
}

struct EndpointsView: View {
    @Environment(AppModel.self) private var app
    @State private var endpoints: [Endpoint] = []
    @State private var webSearch: String?

    var body: some View {
        List {
            ForEach(endpoints) { e in
                Section {
                    LabeledContent("Kind", value: e.kind + (e.driver.map { " · \($0)" } ?? ""))
                    if let url = e.baseUrl { LabeledContent("Base URL", value: url) }
                    LabeledContent("Key", value: e.apiKeyMasked ?? "–")
                    ForEach(app.models.filter { $0.endpointId == e.id }) { m in
                        HStack {
                            Circle().fill(Color(hex: m.color)).frame(width: 9, height: 9)
                            Text(m.displayName)
                            Spacer()
                            Text("\(money(m.inputPrice)) / \(money(m.outputPrice))").font(.caption.monospacedDigit()).foregroundStyle(.secondary)
                        }
                        .opacity(m.enabled ? 1 : 0.4)
                    }
                } header: {
                    HStack {
                        Text(e.name)
                        if e.source == "config" { Text("config.yaml").font(.caption2) }
                        if !e.enabled { Text("disabled").font(.caption2) }
                    }
                }
            }
            Section { LabeledContent("Web search", value: webSearch ?? "not configured") } footer: {
                Text("Prices are USD per 1M tokens (input / output).")
            }
        }
        .navigationTitle("Endpoints")
        .task {
            guard let api = app.api else { return }
            endpoints = (try? await api.endpoints()) ?? []
            webSearch = (try? await api.settings())?.webSearch.provider
            await app.refresh()
        }
    }
}
