import SwiftUI

@MainActor
struct RootView: View {
    enum PendingAction: Hashable, Identifiable {
        case clearQueue
        case loadLastSubmitted

        var id: Self { self }

        var title: String {
            switch self {
            case .clearQueue:
                return "Clear the queue?"
            case .loadLastSubmitted:
                return "Replace the current queue?"
            }
        }

        var message: String {
            switch self {
            case .clearQueue:
                return "Removes every row on this phone. Named drafts and the server copy are not touched."
            case .loadLastSubmitted:
                return "Loads the rows last saved on the server (queue or draft) and replaces what is here."
            }
        }

        var buttonTitle: String {
            switch self {
            case .clearQueue:
                return "Clear queue"
            case .loadLastSubmitted:
                return "Replace"
            }
        }
    }

    @Environment(QueueViewModel.self) private var queue
    @Environment(MetadataStore.self) private var metadata
    @Environment(AppSettings.self) private var settings
    @Environment(PreviewViewModel.self) private var preview
    @Environment(\.scenePhase) private var scenePhase

    @State private var showDrafts = false
    @State private var showSaveAsDraft = false
    @State private var draftName = ""
    @State private var pendingAction: PendingAction?
    @State private var isBusy = false

    var body: some View {
        TabView {
            queueTab
                .tabItem { Label("Queue", systemImage: "tray.full") }
            PreviewView()
                .tabItem { Label("Preview", systemImage: "doc.text.magnifyingglass") }
            SettingsView()
                .tabItem { Label("Settings", systemImage: "gearshape") }
        }
        .onChange(of: queue.rows) { queue.scheduleAutosave() }
        .onChange(of: queue.draft) { queue.scheduleAutosave() }
        .onChange(of: queue.editIndex) { queue.scheduleAutosave() }
        .onChange(of: metadata.version) { queue.refreshAutofill() }
        .onChange(of: scenePhase) {
            if scenePhase == .background || scenePhase == .inactive {
                queue.saveNow()
            }
        }
        .alert("Something went wrong", isPresented: errorBinding) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(queue.errorMessage ?? "")
        }
        .alert("Done", isPresented: infoBinding) {
            Button("OK", role: .cancel) {}
        } message: {
            Text(queue.infoMessage ?? "")
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(
            get: { queue.errorMessage != nil },
            set: { presented in
                if !presented {
                    queue.errorMessage = nil
                }
            }
        )
    }

    private var infoBinding: Binding<Bool> {
        Binding(
            get: { queue.infoMessage != nil },
            set: { presented in
                if !presented {
                    queue.infoMessage = nil
                }
            }
        )
    }

    private var pendingActionBinding: Binding<Bool> {
        Binding(
            get: { pendingAction != nil },
            set: { presented in
                if !presented {
                    pendingAction = nil
                }
            }
        )
    }

    // MARK: Queue tab

    private var queueTab: some View {
        NavigationStack {
            List {
                if !metadata.hasMetadata {
                    emptyMetadataSection
                } else if metadata.isStale {
                    Section {
                        Label(
                            "Using cached metadata (\(metadata.lastError ?? "refresh failed")).",
                            systemImage: "exclamationmark.triangle"
                        )
                        .foregroundStyle(.orange)
                        .font(.callout)
                    }
                }
                RowFormView()
                Section {
                    QueueListView()
                } header: {
                    Text("Queue (\(queue.rows.count))")
                } footer: {
                    if let savedAt = queue.lastSavedAt {
                        Text("Autosaved \(savedAt)")
                    }
                }
            }
            .navigationTitle("Shoebox Queue")
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    EditButton()
                }
                ToolbarItem(placement: .topBarTrailing) {
                    queueMenu
                }
            }
            .sheet(isPresented: $showDrafts) {
                DraftsView()
            }
            .alert("Save as draft", isPresented: $showSaveAsDraft) {
                TextField("Draft name", text: $draftName)
                Button("Save") { saveAsDraft() }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Saves a copy of the \(queue.rows.count) queued rows on this phone.")
            }
            .confirmationDialog(
                pendingAction?.title ?? "",
                isPresented: pendingActionBinding,
                titleVisibility: .visible,
                presenting: pendingAction
            ) { action in
                Button(action.buttonTitle, role: .destructive) { perform(action) }
                Button("Cancel", role: .cancel) {}
            } message: { action in
                Text(action.message)
            }
            .overlay {
                if isBusy {
                    ProgressView()
                        .padding()
                        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
            }
        }
    }

    private var emptyMetadataSection: some View {
        Section {
            VStack(alignment: .leading, spacing: 8) {
                Label("No metadata loaded", systemImage: "tray")
                    .font(.headline)
                Text("Set the API base URL and key in Settings, then refresh the metadata to fill the set, subset, parallel and card pickers.")
                    .font(.callout)
                    .foregroundStyle(.secondary)
                if settings.isConfigured {
                    Button {
                        Task { await refreshMetadata() }
                    } label: {
                        Label(metadata.isRefreshing ? "Refreshing…" : "Refresh metadata", systemImage: "arrow.clockwise")
                    }
                    .disabled(metadata.isRefreshing)
                }
                if let error = metadata.lastError {
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                }
            }
            .padding(.vertical, 4)
        }
    }

    private var queueMenu: some View {
        Menu {
            Button {
                draftName = ""
                showSaveAsDraft = true
            } label: {
                Label("Save as draft…", systemImage: "square.and.arrow.down")
            }
            .disabled(queue.rows.isEmpty)

            Button {
                showDrafts = true
            } label: {
                Label("Drafts…", systemImage: "folder")
            }

            Divider()

            Button {
                Task { await saveDraftToServer() }
            } label: {
                Label("Save draft to server", systemImage: "icloud.and.arrow.up")
            }
            .disabled(!settings.isConfigured || queue.rows.isEmpty || isBusy)

            Button {
                pendingAction = .loadLastSubmitted
            } label: {
                Label("Load last submitted queue", systemImage: "icloud.and.arrow.down")
            }
            .disabled(!settings.isConfigured || isBusy)

            Divider()

            Button(role: .destructive) {
                pendingAction = .clearQueue
            } label: {
                Label("Clear queue", systemImage: "trash")
            }
            .disabled(queue.rows.isEmpty)
        } label: {
            Label("More", systemImage: "ellipsis.circle")
        }
    }

    // MARK: Actions

    private func saveAsDraft() {
        do {
            let named = try queue.saveAsDraft(name: draftName)
            queue.infoMessage = "Saved draft \"\(named.name)\" (\(named.rows.count) rows)."
            draftName = ""
        } catch {
            queue.errorMessage = error.localizedDescription
        }
    }

    private func perform(_ action: PendingAction) {
        switch action {
        case .clearQueue:
            queue.clearQueue()
            preview.reset()
        case .loadLastSubmitted:
            Task { await loadLastSubmitted() }
        }
    }

    private func refreshMetadata() async {
        guard let api = settings.makeClient() else {
            queue.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        await metadata.refresh(using: api, force: false)
        queue.refreshAutofill()
    }

    private func saveDraftToServer() async {
        guard let api = settings.makeClient() else {
            queue.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let response = try await queue.saveDraftToServer(api: api)
            queue.infoMessage = "Draft saved to the server (\(response.counts.rows) rows) at \(response.submittedAt)."
        } catch {
            queue.errorMessage = error.localizedDescription
        }
    }

    private func loadLastSubmitted() async {
        guard let api = settings.makeClient() else {
            queue.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let response = try await queue.loadLastSubmitted(api: api)
            preview.reset()
            if response.rows.isEmpty {
                queue.infoMessage = "The server has no saved queue or draft yet."
            } else {
                let kind = response.kind ?? "queue"
                let when = response.submittedAt ?? "an unknown time"
                queue.infoMessage = "Loaded \(response.rows.count) rows (\(kind) saved \(when))."
            }
        } catch {
            queue.errorMessage = error.localizedDescription
        }
    }
}
