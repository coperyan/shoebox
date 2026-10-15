import Foundation
import Observation

/// `POST /queue/listings` plus the 3-second polling of `GET /queue/listings/{run_id}` behind the
/// "List on eBay" button and the run views. The job runs in the cloud; the phone only watches.
@MainActor
@Observable
final class ListingRunViewModel {
    static let publishDefaultsKey = "listingPublish"
    static let scheduleDefaultsKey = "listingSchedule"
    static let defaultPollInterval: Duration = .seconds(3)

    private let defaults: UserDefaults
    private let pollInterval: Duration
    @ObservationIgnored
    private var pollingTask: Task<Void, Never>?
    @ObservationIgnored
    private var pollGeneration = 0
    @ObservationIgnored
    private var publishStorage: Bool
    @ObservationIgnored
    private var scheduleStorage: Bool

    /// The run being started, shown or polled.
    private(set) var activeRun: ListingRun?
    /// `GET /queue/listings`, newest first; `activeRun` updates are merged in by `run_id`.
    private(set) var runs: [ListingRun] = []
    private(set) var isStarting = false
    private(set) var isLoadingRuns = false
    private(set) var isRefreshing = false
    private(set) var isPolling = false
    /// Start / load / manual refresh failures (the views show these in an alert).
    var errorMessage: String?
    /// The last failed poll, shown inline; polling keeps going.
    private(set) var pollError: String?

    init(defaults: UserDefaults = .standard, pollInterval: Duration = ListingRunViewModel.defaultPollInterval) {
        self.defaults = defaults
        self.pollInterval = pollInterval
        self.publishStorage = Self.flag(Self.publishDefaultsKey, in: defaults, default: true)
        self.scheduleStorage = Self.flag(Self.scheduleDefaultsKey, in: defaults, default: true)
    }

    // MARK: Toggles (persisted; observable through the generated access / withMutation hooks)

    /// `publish: true` publishes the offers; off creates them unpublished (visible in Seller Hub).
    var publish: Bool {
        get {
            access(keyPath: \.publish)
            return publishStorage
        }
        set {
            withMutation(keyPath: \.publish) {
                publishStorage = newValue
            }
            defaults.set(newValue, forKey: Self.publishDefaultsKey)
        }
    }

    /// `schedule: true` sets the listing start time the way the workstation command does.
    var schedule: Bool {
        get {
            access(keyPath: \.schedule)
            return scheduleStorage
        }
        set {
            withMutation(keyPath: \.schedule) {
                scheduleStorage = newValue
            }
            defaults.set(newValue, forKey: Self.scheduleDefaultsKey)
        }
    }

    private static func flag(_ key: String, in defaults: UserDefaults, default fallback: Bool) -> Bool {
        if defaults.object(forKey: key) == nil {
            return fallback
        }
        return defaults.bool(forKey: key)
    }

    // MARK: Starting

    /// `POST /queue/listings` with the current toggles. Returns the new run (also `activeRun`) or
    /// `nil` after setting `errorMessage` (409 "submit the queue first" / "a run is in progress",
    /// 502 when the job could not be started).
    @discardableResult
    func start(api: any QueueAPI) async -> ListingRun? {
        guard !isStarting else {
            return nil
        }
        isStarting = true
        defer { isStarting = false }
        stopPolling()
        do {
            let run = try await api.startListingRun(publish: publish, schedule: schedule)
            activeRun = run
            pollError = nil
            upsert(run)
            return run
        } catch {
            errorMessage = error.localizedDescription
            return nil
        }
    }

    /// Make `run` the active one (from the runs list). A fresher copy of the same run is kept.
    func show(_ run: ListingRun) {
        if activeRun?.runId != run.runId {
            stopPolling()
            activeRun = run
            pollError = nil
        }
    }

    // MARK: Polling

    /// Fetch the active run once. Returns true when it is finished (or there is nothing to poll).
    @discardableResult
    func refresh(api: any QueueAPI) async -> Bool {
        guard let runId = activeRun?.runId else {
            return true
        }
        isRefreshing = true
        defer { isRefreshing = false }
        do {
            let run = try await api.fetchListingRun(id: runId)
            if activeRun?.runId == runId {
                activeRun = run
            }
            upsert(run)
            pollError = nil
            return run.isFinished
        } catch {
            errorMessage = error.localizedDescription
            return activeRun?.isFinished ?? true
        }
    }

    /// Poll `GET /queue/listings/{run_id}` every `pollInterval` until the run is `done` / `failed`
    /// or `stopPolling()` is called. Fetch errors are kept in `pollError` and polling continues.
    func startPolling(api: any QueueAPI) {
        stopPolling()
        guard let run = activeRun, !run.isFinished else {
            return
        }
        let runId = run.runId
        let interval = pollInterval
        pollGeneration += 1
        let generation = pollGeneration
        isPolling = true
        // Created on the main actor, so the body runs there too (`self` is @MainActor).
        pollingTask = Task { [weak self] in
            await self?.pollLoop(api: api, runId: runId, interval: interval)
            guard let self, self.pollGeneration == generation else {
                return
            }
            self.isPolling = false
            self.pollingTask = nil
        }
    }

    private func pollLoop(api: any QueueAPI, runId: String, interval: Duration) async {
        while !Task.isCancelled {
            do {
                try await Task.sleep(for: interval)
            } catch {
                return
            }
            guard !Task.isCancelled, activeRun?.runId == runId else {
                return
            }
            do {
                let latest = try await api.fetchListingRun(id: runId)
                if Task.isCancelled {
                    return
                }
                if activeRun?.runId == runId {
                    activeRun = latest
                }
                upsert(latest)
                pollError = nil
                if latest.isFinished {
                    return
                }
            } catch {
                if Task.isCancelled {
                    return
                }
                pollError = error.localizedDescription
            }
        }
    }

    func stopPolling() {
        pollingTask?.cancel()
        pollingTask = nil
        isPolling = false
    }

    /// Wait for the polling loop to end (tests).
    func waitForPolling() async {
        await pollingTask?.value
    }

    // MARK: History

    func loadRuns(api: any QueueAPI) async {
        isLoadingRuns = true
        defer { isLoadingRuns = false }
        do {
            var fetched = try await api.fetchListingRuns()
            // The list omits `results`; keep the detailed copy of the active run if we have one.
            if let activeRun, let index = fetched.firstIndex(where: { $0.runId == activeRun.runId }),
               activeRun.state == fetched[index].state, !activeRun.results.isEmpty {
                fetched[index] = activeRun
            }
            runs = fetched
        } catch {
            errorMessage = error.localizedDescription
        }
    }

    private func upsert(_ run: ListingRun) {
        if let index = runs.firstIndex(where: { $0.runId == run.runId }) {
            runs[index] = run
        } else {
            runs.insert(run, at: 0)
        }
    }
}
