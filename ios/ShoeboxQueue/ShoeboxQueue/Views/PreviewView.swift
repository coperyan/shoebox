import SwiftUI

/// Preview tab: `POST /queue/preview` per-row results and `PUT /queue`.
@MainActor
struct PreviewView: View {
    @Environment(QueueViewModel.self) private var queue
    @Environment(PreviewViewModel.self) private var preview
    @Environment(AppSettings.self) private var settings

    @State private var showSubmitConfirm = false

    var body: some View {
        NavigationStack {
            List {
                if queue.rows.isEmpty {
                    Section {
                        Text("No rows in the queue yet. Add rows on the Queue tab.")
                            .foregroundStyle(.secondary)
                    }
                } else {
                    controlsSection
                    if let response = preview.previewResponse {
                        resultsSection(response)
                    }
                    outcomeSection
                }
            }
            .navigationTitle("Preview")
            .confirmationDialog(
                "Submit \(queue.rows.count) rows?",
                isPresented: $showSubmitConfirm,
                titleVisibility: .visible
            ) {
                Button("Submit") {
                    Task { await submit() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("PUT /queue overwrites listing_queue_enriched.jsonl on the server, but only if every row enriches.")
            }
            .alert("Error", isPresented: errorBinding) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(preview.errorMessage ?? "")
            }
            .overlay {
                if preview.isBusy {
                    ProgressView(preview.isSubmitting ? "Submitting…" : "Previewing…")
                        .padding()
                        .background(.thinMaterial, in: RoundedRectangle(cornerRadius: 12))
                }
            }
        }
    }

    private var errorBinding: Binding<Bool> {
        Binding(
            get: { preview.errorMessage != nil },
            set: { presented in
                if !presented {
                    preview.errorMessage = nil
                }
            }
        )
    }

    // MARK: Sections

    private var controlsSection: some View {
        Section {
            if !settings.isConfigured {
                Label("Configure the API in Settings to preview or submit.", systemImage: "gearshape")
                    .foregroundStyle(.secondary)
            }
            Button {
                Task { await runPreview() }
            } label: {
                Label("Preview enrichment", systemImage: "doc.text.magnifyingglass")
            }
            .disabled(!settings.isConfigured || preview.isBusy)

            if preview.previewResponse != nil && !preview.isCurrent(for: queue) {
                Label("The queue changed since this preview — run it again.", systemImage: "exclamationmark.triangle")
                    .foregroundStyle(.orange)
                    .font(.callout)
            }
            if queue.hasPendingUploads {
                Label("Some photos are not uploaded yet; Submit uploads them first.", systemImage: "photo.badge.arrow.down")
                    .foregroundStyle(.secondary)
                    .font(.callout)
            }
            Button {
                showSubmitConfirm = true
            } label: {
                Label("Submit queue (\(queue.rows.count) rows)", systemImage: "paperplane")
            }
            .disabled(!settings.isConfigured || preview.isBusy)
        } header: {
            Text("Actions")
        } footer: {
            if let version = preview.previewResponse?.metadataVersion {
                Text("Previewed with metadata \(version)")
            }
        }
    }

    private func resultsSection(_ response: PreviewResponse) -> some View {
        Section {
            ForEach(response.rows, id: \.index) { result in
                DisclosureGroup {
                    rowDetail(result)
                } label: {
                    rowLabel(result)
                }
            }
        } header: {
            Text("Enriched rows — \(response.counts.ok) ok, \(response.counts.errors) errors")
        }
    }

    @ViewBuilder
    private func rowDetail(_ result: RowResult) -> some View {
        if let error = result.error, !error.isEmpty {
            Label(error, systemImage: "xmark.octagon")
                .foregroundStyle(.red)
        }
        if result.enriched != nil {
            ForEach(EnrichedFieldOrder.keys, id: \.self) { key in
                LabeledContent(key) {
                    Text(result.enrichedValue(key)?.displayString ?? "")
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
                .font(.caption)
            }
        }
        ForEach(Array(result.warningMessages.enumerated()), id: \.offset) { _, warning in
            Label(warning, systemImage: "exclamationmark.triangle")
                .foregroundStyle(.orange)
                .font(.caption)
        }
    }

    private func rowLabel(_ result: RowResult) -> some View {
        HStack(spacing: 8) {
            Image(systemName: result.ok ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(result.ok ? Color.green : Color.red)
            Text(title(for: result.index))
                .lineLimit(2)
        }
    }

    @ViewBuilder
    private var outcomeSection: some View {
        if let outcome = preview.outcome {
            switch outcome {
            case .success(let response):
                Section("Submitted") {
                    LabeledContent("Rows") {
                        Text("\(response.counts.rows) (\(response.counts.ok) ok, \(response.counts.errors) errors)")
                    }
                    LabeledContent("Submitted at") {
                        Text(response.submittedAt)
                    }
                    if let uri = response.queueUri {
                        LabeledContent("Queue") {
                            Text(uri)
                                .font(.caption)
                                .textSelection(.enabled)
                        }
                    }
                    if let version = response.metadataVersion {
                        LabeledContent("Metadata") {
                            Text(version)
                                .font(.caption)
                        }
                    }
                    Text("On the workstation: shoebox create-listings --queue <queue uri>")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            case .rejected(let message, let rows):
                Section("Not submitted") {
                    Text(message)
                        .foregroundStyle(.red)
                    ForEach(rows.filter { !$0.ok }, id: \.index) { result in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(title(for: result.index))
                                .font(.caption)
                                .foregroundStyle(.secondary)
                            Text(result.error ?? "error")
                                .foregroundStyle(.red)
                        }
                    }
                    Text("Nothing was written. Fix the rows above and submit again.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            case .uploadsFailed(let failures):
                Section("Not submitted") {
                    Text("Some photos could not be uploaded; nothing was sent.")
                        .foregroundStyle(.red)
                    ForEach(Array(failures.enumerated()), id: \.offset) { _, failure in
                        Text(failure)
                            .font(.caption)
                    }
                }
            }
        }
    }

    // MARK: Helpers

    private func title(for index: Int) -> String {
        if queue.rows.indices.contains(index) {
            return queue.rows[index].listLabel(index: index)
        }
        return "Row \(index + 1)"
    }

    private func runPreview() async {
        guard let api = settings.makeClient() else {
            preview.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        await preview.runPreview(queue: queue, api: api)
    }

    private func submit() async {
        guard let api = settings.makeClient() else {
            preview.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        await preview.submit(queue: queue, api: api)
    }
}
