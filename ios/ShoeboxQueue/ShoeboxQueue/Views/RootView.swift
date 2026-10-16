import SwiftUI

@MainActor
struct RootView: View {
    enum PendingAction: Hashable, Identifiable {
        case clearQueue

        var id: Self { self }

        var title: String {
            switch self {
            case .clearQueue:
                return "Clear the queue?"
            }
        }

        var message: String {
            switch self {
            case .clearQueue:
                return "Removes every row on this phone. Named drafts and the server copy are not touched."
            }
        }

        var buttonTitle: String {
            switch self {
            case .clearQueue:
                return "Clear queue"
            }
        }
    }

    /// `GET /queue` fetched for the preview sheet; the queue is only replaced on confirm.
    struct ServerQueue: Identifiable {
        let id = UUID()
        let response: QueueGetResponse
        let rows: [QueueRow]

        var description: String {
            let kind = response.kind ?? "queue"
            let when = response.submittedAt ?? "an unknown time"
            return "Last \(kind) saved on the server at \(when)."
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
    @State private var serverQueue: ServerQueue?
    @State private var isBusy = false
    @FocusState private var focusedField: RowFormView.Field?

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
        .onChange(of: queue.batch) { queue.scheduleAutosave() }
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
                RowFormView(focus: $focusedField)
                Section {
                    QueueListView()
                } header: {
                    HStack {
                        Text("Queue (\(queue.rows.count))")
                        Spacer()
                        EditButton()
                            .font(.callout)
                            .textCase(nil)
                            .disabled(queue.rows.isEmpty)
                    }
                } footer: {
                    if let savedAt = queue.lastSavedAt {
                        Text("Autosaved \(savedAt)")
                    }
                }
            }
            .navigationTitle("Shoebox Queue")
            .keyboardDismissToolbar(
                next: focusedField?.next == nil ? nil : { focusNext() },
                save: batchSaveTitle.map { title in (title, { saveDraft() }) }
            )
            .toolbar {
                ToolbarItem(placement: .topBarLeading) {
                    Button("Clear") {
                        queue.cancelDraft()
                    }
                    .accessibilityHint("Resets the Add / Edit form; saved rows are not touched.")
                }
                ToolbarItemGroup(placement: .topBarTrailing) {
                    Button(queue.mode == .add ? "Save" : "Update") {
                        saveDraft()
                    }
                    .fontWeight(.semibold)
                    .disabled(!queue.canSaveDraft)
                    queueMenu
                }
            }
            .sheet(isPresented: $showDrafts) {
                DraftsView()
            }
            .sheet(item: $serverQueue) { fetched in
                QueuePreviewSheet(
                    title: "Last submitted queue",
                    subtitle: fetched.description,
                    rows: fetched.rows,
                    currentCount: queue.rows.count
                ) {
                    applyServerQueue(fetched)
                }
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
                Task { await fetchLastSubmitted() }
            } label: {
                Label("Load last submitted queue…", systemImage: "icloud.and.arrow.down")
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

    /// In a batch the keyboard bar gets its own Save, so a card is scans -> Save without leaving
    /// the keyboard.
    private var batchSaveTitle: String? {
        guard queue.mode == .add, let batch = queue.batch else {
            return nil
        }
        return batch.upcoming.isEmpty ? "Save" : "Save & Next"
    }

    /// Save / Update. When a batch loads its next card, the first scan field takes focus.
    private func saveDraft() {
        let wasAdding = queue.mode == .add
        guard queue.saveDraft() else {
            return
        }
        if wasAdding, queue.batch != nil, let side = queue.firstScanSide {
            focusedField = .image(side)
        } else {
            focusedField = nil
        }
    }

    /// The keyboard's Next: the following field, skipping an image side that holds a photo (it
    /// has no text field to focus). Past Price the keyboard closes.
    private func focusNext() {
        queue.commitStagedScans()
        var next = focusedField?.next
        while let candidate = next, let side = candidate.imageSide, queue.imageRef(for: side).kind == .photo {
            next = candidate.next
        }
        focusedField = next
    }

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

    /// Fetch the server copy and show its first rows; nothing changes until the sheet is confirmed.
    private func fetchLastSubmitted() async {
        guard let api = settings.makeClient() else {
            queue.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        isBusy = true
        defer { isBusy = false }
        do {
            let response = try await queue.fetchLastSubmitted(api: api)
            if response.rows.isEmpty {
                queue.infoMessage = "The server has no saved queue or draft yet."
            } else {
                serverQueue = ServerQueue(response: response, rows: queue.rows(from: response))
            }
        } catch {
            queue.errorMessage = error.localizedDescription
        }
    }

    private func applyServerQueue(_ fetched: ServerQueue) {
        queue.replaceQueue(with: fetched.rows)
        preview.reset()
        let kind = fetched.response.kind ?? "queue"
        let when = fetched.response.submittedAt ?? "an unknown time"
        queue.infoMessage = "Loaded \(fetched.rows.count) rows (\(kind) saved \(when))."
    }
}
