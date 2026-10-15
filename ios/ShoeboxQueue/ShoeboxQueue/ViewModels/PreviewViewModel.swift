import Foundation
import Observation

/// `POST /queue/preview` and `PUT /queue` for the Preview tab.
@MainActor
@Observable
final class PreviewViewModel {
    enum SubmitOutcome {
        case success(QueueWriteResponse)
        /// The server refused the whole queue (422) and wrote nothing.
        case rejected(message: String, rows: [RowResult])
        /// Some photos could not be uploaded; nothing was sent.
        case uploadsFailed([String])
    }

    private(set) var previewResponse: PreviewResponse?
    private(set) var previewedInputs: [QueueRowInput] = []
    private(set) var isPreviewing = false
    private(set) var isSubmitting = false
    private(set) var outcome: SubmitOutcome?
    var errorMessage: String?

    var isBusy: Bool {
        isPreviewing || isSubmitting
    }

    /// False when the queue changed since the last preview (drives the banner).
    func isCurrent(for queue: QueueViewModel) -> Bool {
        previewResponse != nil && previewedInputs == queue.rows.map { $0.toInput() }
    }

    func reset() {
        previewResponse = nil
        previewedInputs = []
        outcome = nil
        errorMessage = nil
    }

    func runPreview(queue: QueueViewModel, api: any QueueAPI) async {
        let inputs = queue.rows.map { $0.toInput() }
        guard !inputs.isEmpty else {
            return
        }
        isPreviewing = true
        defer { isPreviewing = false }
        do {
            let response = try await api.previewQueue(inputs)
            previewResponse = response
            previewedInputs = inputs
            outcome = nil
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    /// Upload pending photos first (abort with the failure list if any remain), then `PUT /queue`.
    func submit(queue: QueueViewModel, api: any QueueAPI) async {
        guard !queue.rows.isEmpty else {
            return
        }
        isSubmitting = true
        defer { isSubmitting = false }

        let failures = await queue.uploadPending(api: api)
        if !failures.isEmpty {
            outcome = .uploadsFailed(failures)
            return
        }

        let inputs = queue.rows.map { $0.toInput() }
        do {
            let response = try await api.submitQueue(inputs)
            outcome = .success(response)
            previewedInputs = inputs
        } catch APIError.queueRejected(let message, let rows) {
            outcome = .rejected(message: message, rows: rows)
        } catch {
            errorMessage = error.localizedDescription
        }
    }
}
