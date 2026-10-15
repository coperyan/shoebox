import SwiftUI

@MainActor
struct SettingsView: View {
    @Environment(AppSettings.self) private var settings
    @Environment(MetadataStore.self) private var metadata
    @Environment(QueueViewModel.self) private var queue

    @State private var baseURL = ""
    @State private var apiKey = ""
    @State private var statusMessage: String?
    @State private var isTesting = false

    var body: some View {
        NavigationStack {
            Form {
                Section {
                    TextField("http://localhost:8080", text: $baseURL)
                        .keyboardType(.URL)
                        .textContentType(.URL)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    SecureField("API key (SHOEBOX_API_KEY)", text: $apiKey)
                        .textInputAutocapitalization(.never)
                        .autocorrectionDisabled()
                    Button("Save") { save() }
                        .disabled(baseURL.trimmingCharacters(in: .whitespaces).isEmpty)
                    Button {
                        Task { await testConnection() }
                    } label: {
                        if isTesting {
                            ProgressView()
                        } else {
                            Text("Test connection")
                        }
                    }
                    .disabled(isTesting || AppSettings.normalizedURL(baseURL) == nil)
                    if let statusMessage {
                        Text(statusMessage)
                            .font(.callout)
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text("API")
                } footer: {
                    Text("Use http://localhost:8080 for `shoebox api` in the simulator, or the https Cloud Run URL. Test connection calls /healthz (no key); Refresh metadata checks the key.")
                }

                Section("Metadata") {
                    LabeledContent("Version") {
                        Text(metadata.version ?? "not loaded")
                            .font(.caption)
                    }
                    LabeledContent("Generated at") {
                        Text(metadata.generatedAt ?? "—")
                            .font(.caption)
                    }
                    LabeledContent("Loaded at") {
                        Text(metadata.fetchedAt ?? "—")
                            .font(.caption)
                    }
                    LabeledContent("Sets") {
                        Text("\(metadata.setCount)")
                    }
                    if let counts = metadata.metadata?.counts {
                        LabeledContent("Checklist rows") {
                            Text("\(counts.checklistRows ?? 0)")
                        }
                        LabeledContent("Parallel rows") {
                            Text("\(counts.parallelRows ?? 0)")
                        }
                    }
                    Button {
                        Task { await refreshMetadata() }
                    } label: {
                        Label(metadata.isRefreshing ? "Refreshing…" : "Refresh metadata", systemImage: "arrow.clockwise")
                    }
                    .disabled(metadata.isRefreshing || !settings.isConfigured)
                    if metadata.isStale {
                        Label("Using the cached copy; the last refresh failed.", systemImage: "exclamationmark.triangle")
                            .foregroundStyle(.orange)
                            .font(.callout)
                    }
                    if let error = metadata.lastError {
                        Text(error)
                            .font(.caption)
                            .foregroundStyle(.red)
                    }
                }

                Section("About") {
                    LabeledContent("App version") {
                        Text(Self.appVersion)
                    }
                    LabeledContent("Data folder") {
                        Text(queue.store.baseURL.lastPathComponent)
                            .font(.caption)
                    }
                }
            }
            .navigationTitle("Settings")
            .onAppear {
                baseURL = settings.baseURLString
                apiKey = settings.apiKey
            }
        }
    }

    static var appVersion: String {
        let info = Bundle.main.infoDictionary ?? [:]
        let version = info["CFBundleShortVersionString"] as? String ?? "?"
        let build = info["CFBundleVersion"] as? String ?? "?"
        return "\(version) (\(build))"
    }

    private func save() {
        settings.baseURLString = baseURL
        settings.apiKey = apiKey
        do {
            try settings.save()
            baseURL = settings.baseURLString
            apiKey = settings.apiKey
            statusMessage = settings.isConfigured ? "Saved." : "Saved. Enter a valid http(s) URL and an API key to enable the API."
        } catch {
            statusMessage = "Saved the URL, but the key could not be stored: \(error.localizedDescription)"
        }
    }

    private func testConnection() async {
        guard let url = AppSettings.normalizedURL(baseURL) else {
            statusMessage = "Enter a valid http(s) base URL."
            return
        }
        isTesting = true
        defer { isTesting = false }
        let client = APIClient(config: APIConfig(baseURL: url, apiKey: apiKey.trimmingCharacters(in: .whitespacesAndNewlines)))
        do {
            let health = try await client.health()
            statusMessage = "OK — status \(health.status), API version \(health.version ?? "?")."
        } catch {
            statusMessage = "Failed: \(error.localizedDescription)"
        }
    }

    private func refreshMetadata() async {
        guard let api = settings.makeClient() else {
            statusMessage = APIError.notConfigured.localizedDescription
            return
        }
        await metadata.refresh(using: api, force: true)
        queue.refreshAutofill()
        if let error = metadata.lastError {
            statusMessage = "Metadata refresh failed: \(error)"
        } else {
            statusMessage = "Metadata \(metadata.version ?? "?") loaded (\(metadata.setCount) sets)."
        }
    }
}
