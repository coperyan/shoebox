import Foundation
import Observation

/// The Streamlit session state (`rows`, `draft`, `edit_index`) and the handlers from
/// `shoebox/ui/app.py` (`reset_dependents`, `metadata_update_draft`, `reset_draft_form`, the
/// Save / Cancel / Edit / Delete buttons, "Load last saved JSONL"), plus persistence and images.
@MainActor
@Observable
final class QueueViewModel {
    enum Mode {
        case add
        case edit

        var title: String {
            switch self {
            case .add:
                return "Add"
            case .edit:
                return "Edit"
            }
        }
    }

    enum QueueError: LocalizedError {
        case draftNotFound
        case notConfigured
        case localImageMissing(String)

        var errorDescription: String? {
            switch self {
            case .draftNotFound:
                return "That draft no longer exists."
            case .notConfigured:
                return "Set the API base URL and key in Settings first."
            case .localImageMissing(let name):
                return "The local photo \(name) is missing; remove it and pick it again."
            }
        }
    }

    typealias APIProvider = @MainActor () -> (any QueueAPI)?

    // MARK: State (mirrors st.session_state)

    var rows: [QueueRow] = []
    var draft = QueueRow()
    var editIndex: Int? = nil

    private(set) var lastSavedAt: String?
    var errorMessage: String?
    var infoMessage: String?

    let store: DraftStore
    let metadata: MetadataStore
    let uploader: ImageUploader
    var apiProvider: APIProvider

    @ObservationIgnored
    private var autosaveTask: Task<Void, Never>?

    init(store: DraftStore, metadata: MetadataStore, uploader: ImageUploader, apiProvider: @escaping APIProvider = { nil }) {
        self.store = store
        self.metadata = metadata
        self.uploader = uploader
        self.apiProvider = apiProvider
    }

    var mode: Mode {
        editIndex == nil ? .add : .edit
    }

    var canSaveDraft: Bool {
        draft.isComplete
    }

    // MARK: Option lists (empty until the parents are chosen, like the Streamlit selectboxes)

    var setOptions: [String] {
        metadata.index.setNames
    }

    /// The Set picker's sections: one per year, newest first.
    var setGroups: [PickerGroup] {
        metadata.index.setGroups.map { PickerGroup(title: $0.year, options: $0.setNames) }
    }

    var subsetOptions: [String] {
        guard !draft.setName.isEmpty else {
            return []
        }
        return metadata.index.subsets(for: draft.setName)
    }

    var parallelOptions: [String] {
        guard !draft.setName.isEmpty, !draft.subsetName.isEmpty else {
            return []
        }
        return metadata.index.parallelNames(set: draft.setName, subset: draft.subsetName)
    }

    var cardOptions: [String] {
        guard !draft.setName.isEmpty, !draft.subsetName.isEmpty else {
            return []
        }
        return metadata.index.cardNumbers(set: draft.setName, subset: draft.subsetName)
    }

    // MARK: Cascade (reset_dependents + metadata_update_draft)

    func selectSet(_ value: String) {
        guard value != draft.setName else {
            return
        }
        draft.setName = value
        draft.subsetName = ""
        draft.parallelVariety = ""
        draft.cardNumber = ""
        applyAutofill(to: &draft)
    }

    func selectSubset(_ value: String) {
        guard value != draft.subsetName else {
            return
        }
        draft.subsetName = value
        draft.parallelVariety = ""
        draft.cardNumber = ""
        applyAutofill(to: &draft)
    }

    func selectParallel(_ value: String) {
        guard value != draft.parallelVariety else {
            return
        }
        draft.parallelVariety = value
        applyAutofill(to: &draft)
    }

    func selectCard(_ value: String) {
        guard value != draft.cardNumber else {
            return
        }
        draft.cardNumber = value
        applyAutofill(to: &draft)
    }

    /// Fill the display-only fields from the metadata index (card -> player/team/year/type/note/
    /// note_check, parallel -> print_run/parallel_note) and clear them when nothing matches.
    func applyAutofill(to row: inout QueueRow) {
        let index = metadata.index
        if !row.cardNumber.isEmpty,
           let card = index.card(set: row.setName, subset: row.subsetName, cardNumber: row.cardNumber) {
            row.player = card.player
            row.team = card.team
            row.setYear = card.setYear
            row.subsetType = card.subsetType
            row.note = card.note
            row.noteCheck = card.noteCheck
        } else {
            row.clearCardAutofill()
        }
        if !row.parallelVariety.isEmpty,
           let parallel = index.parallel(set: row.setName, subset: row.subsetName, variety: row.parallelVariety) {
            row.printRun = parallel.printRun
            row.parallelNote = parallel.otherNote
        } else {
            row.clearParallelAutofill()
        }
    }

    /// Copies of `rows` with the autofill applied (for previews that must not touch the queue).
    func autofilled(_ rows: [QueueRow]) -> [QueueRow] {
        rows.map { row in
            var copy = row
            applyAutofill(to: &copy)
            return copy
        }
    }

    /// Re-run autofill on everything after the metadata changed.
    func refreshAutofill() {
        applyAutofill(to: &draft)
        for index in rows.indices {
            applyAutofill(to: &rows[index])
        }
    }

    // MARK: Save / Cancel / Edit / Delete

    /// "Save (Add/Update)": append or replace, then keep set / subset / parallel / price for the
    /// next card.
    @discardableResult
    func saveDraft() -> Bool {
        guard draft.isComplete else {
            return false
        }
        var row = draft
        row.quantity = min(max(row.quantity, 1), 999)
        row.price = max(row.price, 0)
        if let index = editIndex, rows.indices.contains(index) {
            rows[index] = row
        } else {
            rows.append(row)
        }
        resetDraftForm(keepMetadata: true)
        return true
    }

    /// `reset_draft_form(keep_metadata=...)`: a fresh draft, optionally keeping set / subset /
    /// parallel / price; card # -> "", quantity -> 1, images cleared, add mode.
    func resetDraftForm(keepMetadata: Bool) {
        let previous = draft
        var next = QueueRow()
        if keepMetadata {
            next.setName = previous.setName
            next.subsetName = previous.subsetName
            next.parallelVariety = previous.parallelVariety
            next.price = previous.price
        }
        applyAutofill(to: &next)
        draft = next
        editIndex = nil
    }

    /// "Cancel / New": full reset.
    func cancelDraft() {
        resetDraftForm(keepMetadata: false)
    }

    func beginEdit(at index: Int) {
        guard rows.indices.contains(index) else {
            return
        }
        var row = rows[index]
        applyAutofill(to: &row)
        draft = row
        editIndex = index
    }

    func delete(at offsets: IndexSet) {
        for index in offsets.sorted(by: >) where rows.indices.contains(index) {
            rows.remove(at: index)
            guard let editing = editIndex else {
                continue
            }
            if editing == index {
                // The row being edited is gone: back to add mode (keeps set/subset/parallel like the UI).
                resetDraftForm(keepMetadata: true)
            } else if editing > index {
                editIndex = editing - 1
            }
        }
    }

    func delete(at index: Int) {
        delete(at: IndexSet(integer: index))
    }

    /// Same semantics as SwiftUI's `move(fromOffsets:toOffset:)` (`destination` is an offset into
    /// the array *before* removal), implemented here so the view model does not import SwiftUI.
    func move(from source: IndexSet, to destination: Int) {
        let moving = source.sorted().filter { rows.indices.contains($0) }
        guard !moving.isEmpty else {
            return
        }
        let editedId: UUID? = editIndex.flatMap { rows.indices.contains($0) ? rows[$0].id : nil }
        let movingRows = moving.map { rows[$0] }
        var remaining = rows
        for index in moving.reversed() {
            remaining.remove(at: index)
        }
        let removedBefore = moving.filter { $0 < destination }.count
        let insertAt = min(max(destination - removedBefore, 0), remaining.count)
        remaining.insert(contentsOf: movingRows, at: insertAt)
        rows = remaining
        if let editedId {
            editIndex = rows.firstIndex { $0.id == editedId }
        }
    }

    func clearQueue() {
        replaceQueue(with: [])
    }

    /// Replace the working queue (a named draft or the server copy was loaded): full form reset,
    /// autofill re-run against the current metadata, saved right away.
    func replaceQueue(with newRows: [QueueRow]) {
        rows = newRows
        resetDraftForm(keepMetadata: false)
        refreshAutofill()
        saveNow()
    }

    // MARK: Images

    func imageRef(for side: ImageSide) -> ImageRef {
        draft.image(for: side)
    }

    func setImageRef(_ ref: ImageRef, for side: ImageSide) {
        draft.setImage(ref, for: side)
    }

    /// A bare scan number, stored verbatim (the workstation resolves it).
    func setScanNumber(_ number: String, side: ImageSide) {
        setImageRef(.scan(number), for: side)
    }

    func removeImage(side: ImageSide) {
        setImageRef(.empty, for: side)
    }

    func imageData(fileName: String) -> Data? {
        store.loadImage(fileName: fileName)
    }

    /// Downscale off the main actor, save locally, attach to the draft and upload right away when
    /// the API is configured.
    func attachPhoto(data: Data, side: ImageSide) async {
        do {
            let jpeg = try await Task.detached(priority: .userInitiated) { () throws -> Data in
                try ImageProcessor.downscaledJPEG(data)
            }.value
            let fileName = try store.saveImage(jpeg)
            setImageRef(.photo(localFileName: fileName), for: side)
            if let api = apiProvider() {
                startUpload(fileName: fileName, data: jpeg, side: side, api: api)
            }
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    func retryUpload(fileName: String, side: ImageSide) {
        guard let api = apiProvider() else {
            errorMessage = QueueError.notConfigured.localizedDescription
            return
        }
        guard let data = store.loadImage(fileName: fileName) else {
            errorMessage = QueueError.localImageMissing(fileName).localizedDescription
            return
        }
        startUpload(fileName: fileName, data: data, side: side, api: api)
    }

    private func startUpload(fileName: String, data: Data, side: ImageSide, api: any QueueAPI) {
        Task { [weak self] in
            guard let self else {
                return
            }
            await self.performUpload(fileName: fileName, data: data, side: side, api: api)
        }
    }

    private func performUpload(fileName: String, data: Data, side: ImageSide, api: any QueueAPI) async {
        do {
            let uri = try await uploader.upload(fileName: fileName, data: data, side: side, api: api)
            applyUploadedURI(uri, forFile: fileName)
        } catch {
            // The uploader keeps the .failed state; the field shows "Needs upload — Retry".
        }
    }

    /// Patch the draft and every saved row that references `fileName` with the uploaded URI.
    func applyUploadedURI(_ uri: String, forFile fileName: String) {
        func patch(_ ref: inout ImageRef) {
            if ref.kind == .photo, ref.localFileName == fileName {
                ref.remoteUri = uri
            }
        }
        patch(&draft.imageFront)
        patch(&draft.imageBack)
        for index in rows.indices {
            patch(&rows[index].imageFront)
            patch(&rows[index].imageBack)
        }
    }

    /// Photos in saved rows that still have no `gs://` URI.
    func pendingUploads() -> [(fileName: String, side: ImageSide)] {
        var pending: [(fileName: String, side: ImageSide)] = []
        var seen = Set<String>()
        for row in rows {
            for side in ImageSide.allCases {
                let ref = row.image(for: side)
                if ref.needsUpload, let name = ref.localFileName, !seen.contains(name) {
                    seen.insert(name)
                    pending.append((fileName: name, side: side))
                }
            }
        }
        return pending
    }

    var hasPendingUploads: Bool {
        rows.contains { $0.needsUpload }
    }

    /// Upload everything still pending, sequentially. Returns one message per failure.
    func uploadPending(api: any QueueAPI) async -> [String] {
        var failures: [String] = []
        for item in pendingUploads() {
            guard let data = store.loadImage(fileName: item.fileName) else {
                failures.append("\(item.side.title) photo \(item.fileName): local file missing")
                continue
            }
            do {
                let uri = try await uploader.upload(fileName: item.fileName, data: data, side: item.side, api: api)
                applyUploadedURI(uri, forFile: item.fileName)
            } catch {
                failures.append("\(item.side.title) photo \(item.fileName): \(error.localizedDescription)")
            }
        }
        return failures
    }

    // MARK: Persistence (current.json)

    func restore() {
        guard let state = store.loadCurrent() else {
            return
        }
        rows = state.rows
        draft = state.draft
        if let index = state.editIndex, rows.indices.contains(index) {
            editIndex = index
        } else {
            editIndex = nil
        }
        lastSavedAt = state.savedAt
        refreshAutofill()
    }

    func saveNow() {
        autosaveTask?.cancel()
        autosaveTask = nil
        let state = WorkingState(rows: rows, draft: draft, editIndex: editIndex, savedAt: Timestamps.now())
        do {
            try store.saveCurrent(state)
            lastSavedAt = state.savedAt
        } catch {
            errorMessage = "Could not save the queue: \(error.localizedDescription)"
        }
    }

    /// Debounced autosave; RootView calls this from `onChange` of rows / draft / editIndex.
    func scheduleAutosave(after delay: Duration = .milliseconds(600)) {
        autosaveTask?.cancel()
        autosaveTask = Task { [weak self] in
            try? await Task.sleep(for: delay)
            guard !Task.isCancelled, let self else {
                return
            }
            self.saveNow()
        }
    }

    // MARK: Named drafts

    @discardableResult
    func saveAsDraft(name: String) throws -> NamedDraft {
        let now = Timestamps.now()
        let trimmed = name.trimmingCharacters(in: .whitespacesAndNewlines)
        let draftName = trimmed.isEmpty ? "Draft \(now)" : trimmed
        let named = NamedDraft(name: draftName, createdAt: now, updatedAt: now, rows: rows)
        try store.saveDraft(named)
        return named
    }

    func listDrafts() -> [NamedDraftSummary] {
        store.listDrafts()
    }

    /// A named draft in full, for the preview shown before it replaces the queue.
    func namedDraft(id: UUID) -> NamedDraft? {
        store.loadDraft(id: id)
    }

    /// Replace the working queue with a named draft.
    func openDraft(id: UUID) throws {
        guard let named = store.loadDraft(id: id) else {
            throw QueueError.draftNotFound
        }
        replaceQueue(with: named.rows)
    }

    func deleteDraft(id: UUID) {
        try? store.deleteDraft(id: id)
    }

    // MARK: Server round-trips

    /// `PUT /queue/draft` with the raw rows (blanks allowed).
    func saveDraftToServer(api: any QueueAPI) async throws -> QueueWriteResponse {
        try await api.saveDraft(rows.map { $0.toInput() })
    }

    /// `GET /queue` without touching the working queue; the preview sheet shows the result and
    /// `replaceQueue(with: rows(from:))` applies it once confirmed.
    func fetchLastSubmitted(api: any QueueAPI) async throws -> QueueGetResponse {
        try await api.fetchLastQueue()
    }

    /// The server's rows as queue rows with the autofill applied (only the 8 user fields travel).
    func rows(from response: QueueGetResponse) -> [QueueRow] {
        autofilled(response.rows.map { QueueRow.fromInput($0) })
    }

    /// `GET /queue` and replace: like the Streamlit "Load last saved JSONL", only the 8 user fields
    /// come back; autofill is re-run against the current metadata.
    @discardableResult
    func loadLastSubmitted(api: any QueueAPI) async throws -> QueueGetResponse {
        let response = try await fetchLastSubmitted(api: api)
        replaceQueue(with: rows(from: response))
        return response
    }
}
