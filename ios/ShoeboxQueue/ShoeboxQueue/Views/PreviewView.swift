import SwiftUI

/// Preview tab: `POST /queue/preview` per-row results, `PUT /queue`, and the "List on eBay"
/// section that starts a `create-listings` run in the cloud (`POST /queue/listings`).
@MainActor
struct PreviewView: View {
    @Environment(QueueViewModel.self) private var queue
    @Environment(PreviewViewModel.self) private var preview
    @Environment(ListingRunViewModel.self) private var listing
    @Environment(AppSettings.self) private var settings

    @State private var showSubmitConfirm = false
    @State private var showListingConfirm = false
    @State private var path: [ListingRoute] = []

    var body: some View {
        NavigationStack(path: $path) {
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
                if showsListingSection {
                    listingSection
                }
            }
            .navigationTitle("Preview")
            .navigationDestination(for: ListingRoute.self) { route in
                switch route {
                case .runs:
                    ListingRunsView()
                case .run(let run):
                    ListingRunView(run: run)
                }
            }
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button {
                        path.append(.runs)
                    } label: {
                        Label("Runs", systemImage: "list.bullet.rectangle")
                    }
                    .disabled(!settings.isConfigured)
                }
            }
            .confirmationDialog(
                "Create \(listingRowCount) listings on eBay?",
                isPresented: $showListingConfirm,
                titleVisibility: .visible
            ) {
                Button("List on eBay") {
                    Task { await startListing() }
                }
                Button("Cancel", role: .cancel) {}
            } message: {
                Text("Publish: \(listing.publish ? "on" : "off") · Schedule: \(listing.schedule ? "on" : "off") · Scans: \(listing.requestedScansFolder ?? "default folder"). Runs create-listings in the cloud on the last submitted queue; no prices are scraped.")
            }
            .alert("Listing error", isPresented: listingErrorBinding) {
                Button("OK", role: .cancel) {}
            } message: {
                Text(listing.errorMessage ?? "")
            }
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

    private var listingErrorBinding: Binding<Bool> {
        Binding(
            get: { listing.errorMessage != nil },
            set: { presented in
                if !presented {
                    listing.errorMessage = nil
                }
            }
        )
    }

    /// The last successful `PUT /queue` of this session, if any.
    private var submittedResponse: QueueWriteResponse? {
        if case .success(let response) = preview.outcome {
            return response
        }
        return nil
    }

    /// Shown after a successful submit, or whenever there are rows and the API is configured
    /// (the server lists whatever was last submitted with PUT /queue).
    private var showsListingSection: Bool {
        settings.isConfigured && (submittedResponse != nil || !queue.rows.isEmpty)
    }

    /// Rows the run will list: the submitted count when we have it, else the local queue size.
    private var listingRowCount: Int {
        submittedResponse?.counts.rows ?? queue.rows.count
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
                    Text("Next: List on eBay below, or on the workstation: shoebox create-listings --queue <queue uri>")
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

    private var listingSection: some View {
        @Bindable var listing = listing
        return Section {
            Toggle("Publish offers", isOn: $listing.publish)
            Toggle("Schedule start time", isOn: $listing.schedule)
            LabeledContent("Scans folder") {
                TextField("Default (app.yaml)", text: $listing.scansFolder)
                    .multilineTextAlignment(.trailing)
                    .textInputAutocapitalization(.never)
                    .autocorrectionDisabled()
                    .submitLabel(.done)
            }
            Button {
                showListingConfirm = true
            } label: {
                Label(
                    listing.isStarting ? "Starting…" : "List on eBay (\(listingRowCount) rows)",
                    systemImage: "cart.badge.plus"
                )
            }
            .disabled(listing.isStarting || preview.isBusy)
            if submittedResponse == nil {
                Label("Lists the queue last submitted with PUT /queue. Submit first if the rows here changed.", systemImage: "info.circle")
                    .font(.callout)
                    .foregroundStyle(.secondary)
            } else if !preview.isCurrent(for: queue) {
                Label("The queue changed since it was submitted — submit again before listing.", systemImage: "exclamationmark.triangle")
                    .font(.callout)
                    .foregroundStyle(.orange)
            }
        } header: {
            Text("List on eBay")
        } footer: {
            Text("Runs shoebox create-listings in the cloud: scans are fetched from OneDrive and no prices are scraped, so the PC is not needed. Scans folder is a path under your OneDrive root (e.g. Pictures/Card Scans); leave it blank for the one in app.yaml. With Publish off the offers are created unpublished (check them in Seller Hub first).")
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

    private func startListing() async {
        guard let api = settings.makeClient() else {
            listing.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        if let run = await listing.start(api: api) {
            path.append(.run(run))
        }
    }
}
