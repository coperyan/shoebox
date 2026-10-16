import SwiftUI

/// Pushed destinations of the Preview tab's NavigationStack.
enum ListingRoute: Hashable {
    case runs
    case run(ListingRun)
}

/// State pill: orange while requested, blue while running, green when done, red when failed.
struct ListingRunStateBadge: View {
    let state: String

    static func color(for state: String) -> Color {
        switch state {
        case ListingRun.stateDone:
            return .green
        case ListingRun.stateFailed:
            return .red
        case ListingRun.stateRunning:
            return .blue
        default:
            return .orange
        }
    }

    var body: some View {
        let color = Self.color(for: state)
        Text(state.capitalized)
            .font(.caption.weight(.semibold))
            .padding(.horizontal, 8)
            .padding(.vertical, 3)
            .background(color.opacity(0.18), in: Capsule())
            .foregroundStyle(color)
    }
}

/// `GET /queue/listings`: recent runs, newest first. Tap one to open `ListingRunView`.
@MainActor
struct ListingRunsView: View {
    @Environment(ListingRunViewModel.self) private var listing
    @Environment(AppSettings.self) private var settings

    var body: some View {
        List {
            if listing.runs.isEmpty {
                Section {
                    Text(listing.isLoadingRuns ? "Loading runs…" : "No listing runs yet. Submit the queue, then tap List on eBay.")
                        .foregroundStyle(.secondary)
                }
            }
            ForEach(listing.runs) { run in
                NavigationLink(value: ListingRoute.run(run)) {
                    runRow(run)
                }
            }
        }
        .navigationTitle("Listing runs")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await load() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(listing.isLoadingRuns)
            }
        }
        .refreshable {
            await load()
        }
        .task {
            await load()
        }
    }

    private func runRow(_ run: ListingRun) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 8) {
                ListingRunStateBadge(state: run.state)
                Text(run.requestedAt)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
            }
            HStack(spacing: 12) {
                Text("\(run.listed) listed")
                Text("\(run.failed) failed")
                    .foregroundStyle(run.failed > 0 ? Color.red : Color.secondary)
                Text("\(run.rowsDone)/\(run.rowsTotal) rows")
                    .foregroundStyle(.secondary)
            }
            .font(.subheadline)
            Text("Publish \(run.publish ? "on" : "off") · Schedule \(run.schedule ? "on" : "off")" + (run.scansFolder.map { " · \($0)" } ?? ""))
                .font(.caption)
                .foregroundStyle(.secondary)
        }
        .padding(.vertical, 2)
    }

    private func load() async {
        guard let api = settings.makeClient() else {
            listing.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        await listing.loadRuns(api: api)
    }
}

/// One run: state, progress, counts, per-row results. Polls every 3 s while the run is not
/// finished and stops when the view disappears or the app goes to the background.
@MainActor
struct ListingRunView: View {
    let run: ListingRun

    @Environment(ListingRunViewModel.self) private var listing
    @Environment(AppSettings.self) private var settings
    @Environment(\.scenePhase) private var scenePhase

    /// The freshest copy: the view model's active run when it is this one, else what we were given.
    private var current: ListingRun {
        if let active = listing.activeRun, active.runId == run.runId {
            return active
        }
        return run
    }

    var body: some View {
        let run = current
        List {
            statusSection(run)
            if run.isFailed, let error = run.error, !error.isEmpty {
                Section {
                    Label(error, systemImage: "xmark.octagon")
                        .foregroundStyle(.red)
                } header: {
                    Text("Run failed")
                }
            }
            if let pollError = listing.pollError {
                Section {
                    Label("Last refresh failed: \(pollError)", systemImage: "wifi.exclamationmark")
                        .foregroundStyle(.orange)
                        .font(.callout)
                }
            }
            detailsSection(run)
            if !run.results.isEmpty {
                resultsSection(run)
            } else if run.isFinished {
                Section("Results") {
                    Text("No per-row results were recorded.")
                        .foregroundStyle(.secondary)
                }
            }
        }
        .navigationTitle("Listing run")
        .navigationBarTitleDisplayMode(.inline)
        .toolbar {
            ToolbarItem(placement: .topBarTrailing) {
                Button {
                    Task { await refresh() }
                } label: {
                    Label("Refresh", systemImage: "arrow.clockwise")
                }
                .disabled(listing.isRefreshing)
            }
        }
        .refreshable {
            await refresh()
        }
        .task {
            listing.show(self.run)
            await refresh()
            // The view may have gone while the refresh was in flight; do not poll for nobody.
            if !Task.isCancelled {
                startPollingIfNeeded()
            }
        }
        .onDisappear {
            listing.stopPolling()
        }
        .onChange(of: scenePhase) {
            if scenePhase == .active {
                startPollingIfNeeded()
            } else {
                listing.stopPolling()
            }
        }
    }

    // MARK: Sections

    private func statusSection(_ run: ListingRun) -> some View {
        Section {
            HStack {
                ListingRunStateBadge(state: run.state)
                if listing.isPolling {
                    ProgressView()
                        .controlSize(.small)
                }
                Spacer()
                Text(run.runId)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            ProgressView(value: run.progress) {
                Text("\(run.rowsDone) of \(run.rowsTotal) rows")
                    .font(.caption)
            }
            .tint(ListingRunStateBadge.color(for: run.state))
            HStack(spacing: 16) {
                Label("\(run.listed) listed", systemImage: "checkmark.circle")
                    .foregroundStyle(.green)
                Label("\(run.failed) failed", systemImage: "xmark.circle")
                    .foregroundStyle(run.failed > 0 ? Color.red : Color.secondary)
            }
            .font(.subheadline)
        } header: {
            Text("Status")
        } footer: {
            if !run.isFinished {
                Text("Refreshing every 3 seconds while the job runs in the cloud. You can leave this screen; the run keeps going.")
            }
        }
    }

    private func detailsSection(_ run: ListingRun) -> some View {
        Section("Details") {
            LabeledContent("Publish") {
                Text(run.publish ? "on" : "off")
            }
            LabeledContent("Schedule") {
                Text(run.schedule ? "on" : "off")
            }
            LabeledContent("Scans folder") {
                Text(run.scansFolder ?? "default")
            }
            LabeledContent("Requested") {
                Text(run.requestedAt)
                    .font(.caption)
            }
            if let startedAt = run.startedAt {
                LabeledContent("Started") {
                    Text(startedAt)
                        .font(.caption)
                }
            }
            if let finishedAt = run.finishedAt {
                LabeledContent("Finished") {
                    Text(finishedAt)
                        .font(.caption)
                }
            }
            if let version = run.metadataVersion {
                LabeledContent("Metadata") {
                    Text(version)
                        .font(.caption)
                }
            }
            if !run.queueUri.isEmpty {
                LabeledContent("Queue") {
                    Text(run.queueUri)
                        .font(.caption)
                        .multilineTextAlignment(.trailing)
                        .textSelection(.enabled)
                }
            }
            if let host = run.host {
                LabeledContent("Host") {
                    Text(host)
                        .font(.caption)
                }
            }
        }
    }

    private func resultsSection(_ run: ListingRun) -> some View {
        Section {
            ForEach(Array(run.results.enumerated()), id: \.offset) { _, result in
                resultRow(result)
            }
        } header: {
            Text("Results (\(run.results.count))")
        }
    }

    private func resultRow(_ result: ListingRunResult) -> some View {
        HStack(alignment: .top, spacing: 8) {
            Image(systemName: result.success ? "checkmark.circle.fill" : "xmark.circle.fill")
                .foregroundStyle(result.success ? Color.green : Color.red)
            VStack(alignment: .leading, spacing: 2) {
                Text(result.title.isEmpty ? result.sku : result.title)
                    .lineLimit(2)
                if result.success {
                    Text(resultIds(result))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .textSelection(.enabled)
                } else {
                    Text(result.error ?? "error")
                        .font(.caption)
                        .foregroundStyle(.red)
                }
                if !result.sku.isEmpty && !result.title.isEmpty {
                    Text(result.sku)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.vertical, 2)
    }

    private func resultIds(_ result: ListingRunResult) -> String {
        var parts: [String] = []
        if let listingId = result.listingId, !listingId.isEmpty {
            parts.append("listing \(listingId)")
        }
        if let offerId = result.offerId, !offerId.isEmpty {
            parts.append("offer \(offerId)")
        }
        return parts.isEmpty ? "listed" : parts.joined(separator: " · ")
    }

    // MARK: Actions

    private func refresh() async {
        guard let api = settings.makeClient() else {
            listing.errorMessage = APIError.notConfigured.localizedDescription
            return
        }
        await listing.refresh(api: api)
    }

    private func startPollingIfNeeded() {
        guard !current.isFinished, let api = settings.makeClient() else {
            return
        }
        listing.startPolling(api: api)
    }
}
