import Foundation
import XCTest
@testable import ShoeboxQueue

@MainActor
final class ListingRunViewModelTests: XCTestCase {
    private func makeDefaults() throws -> UserDefaults {
        let suiteName = "ShoeboxQueueTests-" + UUID().uuidString
        let defaults = try XCTUnwrap(UserDefaults(suiteName: suiteName))
        defaults.removePersistentDomain(forName: suiteName)
        addTeardownBlock {
            defaults.removePersistentDomain(forName: suiteName)
        }
        return defaults
    }

    private func makeViewModel(defaults: UserDefaults? = nil, pollInterval: Duration = .milliseconds(10)) throws -> ListingRunViewModel {
        ListingRunViewModel(defaults: try defaults ?? makeDefaults(), pollInterval: pollInterval)
    }

    // MARK: Toggles

    func testTogglesDefaultOnAndPersist() throws {
        let defaults = try makeDefaults()
        let vm = try makeViewModel(defaults: defaults)
        XCTAssertTrue(vm.publish)
        XCTAssertTrue(vm.schedule)

        vm.publish = false
        vm.schedule = false
        XCTAssertFalse(vm.publish)
        XCTAssertFalse(vm.schedule)
        XCTAssertEqual(defaults.object(forKey: ListingRunViewModel.publishDefaultsKey) as? Bool, false)
        XCTAssertEqual(defaults.object(forKey: ListingRunViewModel.scheduleDefaultsKey) as? Bool, false)

        let reloaded = try makeViewModel(defaults: defaults)
        XCTAssertFalse(reloaded.publish)
        XCTAssertFalse(reloaded.schedule)

        reloaded.schedule = true
        let again = try makeViewModel(defaults: defaults)
        XCTAssertFalse(again.publish)
        XCTAssertTrue(again.schedule)
    }

    // MARK: Start

    func testStartSendsTogglesAndSetsActiveRun() async throws {
        let vm = try makeViewModel()
        let fake = FakeAPI()
        fake.startResponse = Fixtures.makeRun(id: "run-1", state: ListingRun.stateRequested, rowsTotal: 3)
        vm.publish = false
        vm.schedule = true

        let run = await vm.start(api: fake)

        XCTAssertEqual(fake.startedRuns, [StartListingRunRequest(publish: false, schedule: true)])
        XCTAssertEqual(run?.runId, "run-1")
        XCTAssertFalse(try XCTUnwrap(run).publish)
        XCTAssertTrue(try XCTUnwrap(run).schedule)
        XCTAssertEqual(vm.activeRun?.runId, "run-1")
        XCTAssertEqual(vm.activeRun?.state, "requested")
        XCTAssertEqual(vm.runs.map { $0.runId }, ["run-1"], "the new run is added to the history")
        XCTAssertNil(vm.errorMessage)
        XCTAssertFalse(vm.isStarting)
    }

    func testStartFailureKeepsStateAndSetsErrorMessage() async throws {
        let vm = try makeViewModel()
        let fake = FakeAPI()
        fake.startError = APIError.http(status: 409, message: "Not possible right now (409). a run is in progress")

        let run = await vm.start(api: fake)

        XCTAssertNil(run)
        XCTAssertNil(vm.activeRun)
        XCTAssertEqual(fake.startedRuns.count, 1)
        XCTAssertTrue((vm.errorMessage ?? "").contains("a run is in progress"))
    }

    // MARK: Polling

    func testPollingStopsWhenTheRunFinishes() async throws {
        let vm = try makeViewModel(pollInterval: .milliseconds(5))
        let fake = FakeAPI()
        fake.startResponse = Fixtures.makeRun(id: "run-2", state: ListingRun.stateRequested, rowsTotal: 2)
        fake.runUpdates = [
            Fixtures.makeRun(id: "run-2", state: ListingRun.stateRunning, rowsTotal: 2, rowsDone: 1, listed: 1),
            Fixtures.makeRun(
                id: "run-2",
                state: ListingRun.stateDone,
                rowsTotal: 2,
                rowsDone: 2,
                listed: 1,
                failed: 1,
                results: [
                    ListingRunResult(sku: "A", title: "Card A", listingId: "1", offerId: "2", success: true, error: nil),
                    ListingRunResult(sku: "B", title: "Card B", listingId: nil, offerId: nil, success: false, error: "boom"),
                ]
            ),
        ]

        await vm.start(api: fake)
        vm.startPolling(api: fake)
        XCTAssertTrue(vm.isPolling)
        await vm.waitForPolling()

        XCTAssertFalse(vm.isPolling)
        XCTAssertEqual(fake.fetchedRunIds, ["run-2", "run-2"], "running, then done, then stop")
        XCTAssertEqual(vm.activeRun?.state, "done")
        XCTAssertEqual(vm.activeRun?.results.count, 2)
        XCTAssertEqual(vm.activeRun?.progress ?? 0, 1, accuracy: 0.0001)
        XCTAssertEqual(vm.runs.first?.state, "done", "history row is updated in place")
        XCTAssertEqual(vm.runs.count, 1)
        XCTAssertNil(vm.pollError)

        // Finished: starting again is a no-op.
        vm.startPolling(api: fake)
        XCTAssertFalse(vm.isPolling)
        XCTAssertEqual(fake.fetchedRunIds.count, 2)
    }

    func testStopPollingCancelsTheLoop() async throws {
        let vm = try makeViewModel(pollInterval: .seconds(30))
        let fake = FakeAPI()
        fake.startResponse = Fixtures.makeRun(id: "run-3", state: ListingRun.stateRunning, rowsTotal: 5)

        await vm.start(api: fake)
        vm.startPolling(api: fake)
        XCTAssertTrue(vm.isPolling)
        vm.stopPolling()
        XCTAssertFalse(vm.isPolling)
        await vm.waitForPolling()
        XCTAssertEqual(fake.fetchedRunIds, [], "cancelled during the first sleep: no fetch")
    }

    func testPollErrorIsKeptInlineAndPollingContinues() async throws {
        let vm = try makeViewModel(pollInterval: .milliseconds(5))
        let fake = FakeAPI()
        fake.startResponse = Fixtures.makeRun(id: "run-4", state: ListingRun.stateRunning, rowsTotal: 1)
        fake.fetchRunError = FakeAPI.FakeError(message: "offline")

        await vm.start(api: fake)
        vm.startPolling(api: fake)
        // Let a couple of polls fail, then let the run finish.
        try await Task.sleep(for: .milliseconds(40))
        XCTAssertTrue(vm.isPolling)
        XCTAssertEqual(vm.pollError, "offline")
        XCTAssertNil(vm.errorMessage, "poll failures do not raise the alert")
        fake.fetchRunError = nil
        fake.runUpdates = [Fixtures.makeRun(id: "run-4", state: ListingRun.stateFailed, rowsTotal: 1, rowsDone: 0, error: "job crashed")]
        await vm.waitForPolling()

        XCTAssertFalse(vm.isPolling)
        XCTAssertNil(vm.pollError)
        XCTAssertEqual(vm.activeRun?.state, "failed")
        XCTAssertEqual(vm.activeRun?.error, "job crashed")
        XCTAssertTrue(vm.activeRun?.isFailed ?? false)
    }

    func testRefreshUpdatesActiveRunAndReportsFinished() async throws {
        let vm = try makeViewModel()
        let fake = FakeAPI()
        fake.startResponse = Fixtures.makeRun(id: "run-5", state: ListingRun.stateRequested, rowsTotal: 1)
        await vm.start(api: fake)

        fake.runUpdates = [Fixtures.makeRun(id: "run-5", state: ListingRun.stateRunning, rowsTotal: 1)]
        let finishedAfterFirst = await vm.refresh(api: fake)
        XCTAssertFalse(finishedAfterFirst)
        XCTAssertEqual(vm.activeRun?.state, "running")

        fake.runUpdates = [Fixtures.makeRun(id: "run-5", state: ListingRun.stateDone, rowsTotal: 1, rowsDone: 1, listed: 1)]
        let finishedAfterSecond = await vm.refresh(api: fake)
        XCTAssertTrue(finishedAfterSecond)
        XCTAssertEqual(vm.activeRun?.state, "done")
        XCTAssertEqual(fake.fetchedRunIds, ["run-5", "run-5"])
    }

    // MARK: History

    func testLoadRunsKeepsDetailedActiveRun() async throws {
        let vm = try makeViewModel()
        let fake = FakeAPI()
        let detailed = Fixtures.makeRun(
            id: "run-6",
            state: ListingRun.stateDone,
            rowsTotal: 1,
            rowsDone: 1,
            listed: 1,
            results: [ListingRunResult(sku: "A", title: "Card A", listingId: "1", offerId: nil, success: true, error: nil)]
        )
        vm.show(detailed)
        fake.runs = [
            Fixtures.makeRun(id: "run-7", state: ListingRun.stateRunning, rowsTotal: 4, rowsDone: 2),
            Fixtures.makeRun(id: "run-6", state: ListingRun.stateDone, rowsTotal: 1, rowsDone: 1, listed: 1),
        ]

        await vm.loadRuns(api: fake)

        XCTAssertEqual(fake.runsFetchCount, 1)
        XCTAssertEqual(vm.runs.map { $0.runId }, ["run-7", "run-6"])
        XCTAssertEqual(vm.runs[1].results.count, 1, "the list omits results; the detailed copy wins")
        XCTAssertFalse(vm.isLoadingRuns)
        XCTAssertNil(vm.errorMessage)
    }

    func testLoadRunsFailureSetsErrorMessage() async throws {
        let vm = try makeViewModel()
        let fake = FakeAPI()
        fake.runsError = FakeAPI.FakeError(message: "nope")

        await vm.loadRuns(api: fake)

        XCTAssertEqual(vm.runs, [])
        XCTAssertEqual(vm.errorMessage, "nope")
    }

    func testShowSwitchesActiveRunAndStopsPolling() async throws {
        let vm = try makeViewModel(pollInterval: .seconds(30))
        let fake = FakeAPI()
        fake.startResponse = Fixtures.makeRun(id: "run-8", state: ListingRun.stateRunning, rowsTotal: 1)
        await vm.start(api: fake)
        vm.startPolling(api: fake)
        XCTAssertTrue(vm.isPolling)

        vm.show(Fixtures.makeRun(id: "run-9", state: ListingRun.stateDone, rowsTotal: 1, rowsDone: 1))
        XCTAssertEqual(vm.activeRun?.runId, "run-9")
        XCTAssertFalse(vm.isPolling)

        // Showing the same run again keeps the copy already loaded (it may be fresher).
        vm.show(Fixtures.makeRun(id: "run-9", state: ListingRun.stateRunning, rowsTotal: 1))
        XCTAssertEqual(vm.activeRun?.state, "done")
    }

    // MARK: Model helpers

    func testProgressAndFinishedHelpers() {
        XCTAssertEqual(Fixtures.makeRun(rowsTotal: 0, rowsDone: 0).progress, 0)
        XCTAssertEqual(Fixtures.makeRun(rowsTotal: 4, rowsDone: 1).progress, 0.25, accuracy: 0.0001)
        XCTAssertEqual(Fixtures.makeRun(rowsTotal: 2, rowsDone: 5).progress, 1, "clamped")
        XCTAssertFalse(Fixtures.makeRun(state: ListingRun.stateRequested).isFinished)
        XCTAssertFalse(Fixtures.makeRun(state: ListingRun.stateRunning).isFinished)
        XCTAssertTrue(Fixtures.makeRun(state: ListingRun.stateDone).isFinished)
        XCTAssertTrue(Fixtures.makeRun(state: ListingRun.stateFailed).isFinished)
        XCTAssertTrue(Fixtures.makeRun(state: ListingRun.stateFailed).isFailed)
    }
}
