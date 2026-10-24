import SwiftUI

struct ConnectView: View {
    @Environment(AppModel.self) private var app
    @State private var url = ""
    @State private var token = ""
    @State private var busy = false

    init() {}

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("https://multichat.example.com", text: $url)
                        .textContentType(.URL)
                        .keyboardType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API token", text: $token)
                        .textInputAutocapitalization(.never)
                } header: {
                    Text("Server")
                } footer: {
                    Text("The token is MULTICHAT_API_TOKEN in the server's .env. It is stored in the iPhone Keychain.")
                }
                if let error = app.error {
                    Section { Text(error).foregroundStyle(.red) }
                }
                Section {
                    Button {
                        busy = true
                        Task {
                            _ = await app.connect(url: url, token: token)
                            busy = false
                        }
                    } label: {
                        HStack {
                            Text("Connect")
                            if busy { Spacer(); ProgressView() }
                        }
                    }
                    .disabled(url.isEmpty || token.isEmpty || busy)
                }
            }
            .navigationTitle("MultiChat")
            .onAppear { if url.isEmpty { url = app.serverURL } }
        }
    }
}
