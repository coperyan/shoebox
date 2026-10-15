import Foundation
import XCTest
@testable import ShoeboxQueue

@MainActor
final class QueueViewModelTests: XCTestCase {
    private struct Harness {
        let viewModel: QueueViewModel
        let store: DraftStore
        let metadata: MetadataStore
        let directory: URL
    }

    private func makeHarness(apiProvider: @escaping QueueViewModel.APIProvider = { nil }) throws -> Harness {
        let directory = try TempDirectory.make()
        addTeardownBlock {
            try? FileManager.default.removeItem(at: directory)
        }
        let store = DraftStore(baseURL: directory)
        let metadata = MetadataStore(directory: directory)
        let fixture = try Fixtures.metadata()
        metadata.apply(fixture, etag: "W/\"abc123def4567890\"")
        let viewModel = QueueViewModel(store: store, metadata: metadata, uploader: ImageUploader(), apiProvider: apiProvider)
        return Harness(viewModel: viewModel, store: store, metadata: metadata, directory: directory)
    }

    /// Fill the draft with a complete row from the fixture.
    private func fillDraft(_ vm: QueueViewModel, card: String = "1", parallel: String = "Gold Refractor") {
        vm.selectSet(Fixtures.setName)
        vm.selectSubset(Fixtures.subsetName)
        vm.selectParallel(parallel)
        vm.selectCard(card)
        vm.draft.quantity = 3
        vm.draft.price = 12.5
        vm.setScanNumber("123", side: .front)
        vm.setImageRef(.photo(localFileName: "abc.jpg", remoteUri: "gs://bucket/uploads/2026-10/abc.jpg"), for: .back)
    }

    func testOptionListsAreEmptyUntilParentsChosen() throws {
        let vm = try makeHarness().viewModel
        XCTAssertEqual(vm.setOptions, ["2023 Topps Chrome", "2024 Bowman"])
        XCTAssertEqual(vm.subsetOptions, [])
        XCTAssertEqual(vm.parallelOptions, [])
        XCTAssertEqual(vm.cardOptions, [])

        vm.selectSet(Fixtures.setName)
        XCTAssertEqual(vm.subsetOptions, ["Base", "Rookie Autographs"])
        XCTAssertEqual(vm.cardOptions, [])

        vm.selectSubset(Fixtures.subsetName)
        XCTAssertEqual(vm.cardOptions, ["1", "2", "10", "1A", "A", "NNO"])
        XCTAssertEqual(vm.parallelOptions, ["Gold Refractor", "Refractor"])
    }

    func testCascadeClearsDependents() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm)
        XCTAssertEqual(vm.draft.player, "Player One")
        XCTAssertEqual(vm.draft.printRun, 50)

        vm.selectSubset("Rookie Autographs")
        XCTAssertEqual(vm.draft.subsetName, "Rookie Autographs")
        XCTAssertEqual(vm.draft.parallelVariety, "")
        XCTAssertEqual(vm.draft.cardNumber, "")
        XCTAssertNil(vm.draft.player)
        XCTAssertNil(vm.draft.printRun)
        XCTAssertNil(vm.draft.parallelNote)

        vm.selectCard("RA-1")
        XCTAssertEqual(vm.draft.player, "Rookie One")

        vm.selectSet("2024 Bowman")
        XCTAssertEqual(vm.draft.subsetName, "")
        XCTAssertEqual(vm.draft.cardNumber, "")
        XCTAssertNil(vm.draft.player)
        // Non-cascading fields survive.
        XCTAssertEqual(vm.draft.quantity, 3)
        XCTAssertEqual(vm.draft.price, 12.5)
        XCTAssertEqual(vm.draft.imageFront, ImageRef.scan("123"))
    }

    func testAutofillFillsAndClears() throws {
        let vm = try makeHarness().viewModel
        vm.selectSet(Fixtures.setName)
        vm.selectSubset(Fixtures.subsetName)
        vm.selectCard("2")
        XCTAssertEqual(vm.draft.player, "Player Two")
        XCTAssertEqual(vm.draft.team, "Team B")
        XCTAssertEqual(vm.draft.setYear, "2023")
        XCTAssertEqual(vm.draft.subsetType, "Base")
        XCTAssertEqual(vm.draft.note, "RC")
        XCTAssertEqual(vm.draft.noteCheck, "ok")

        vm.selectCard("")
        XCTAssertNil(vm.draft.player)
        XCTAssertNil(vm.draft.noteCheck)
        XCTAssertFalse(vm.draft.hasCardAutofill)

        vm.selectParallel("Gold Refractor")
        XCTAssertEqual(vm.draft.printRun, 50)
        XCTAssertEqual(vm.draft.parallelNote, "/50")

        vm.selectParallel("Refractor")
        XCTAssertNil(vm.draft.printRun, "print_run is null for this parallel")
        XCTAssertNil(vm.draft.parallelNote)

        vm.selectParallel("")
        XCTAssertFalse(vm.draft.hasParallelAutofill)
    }

    func testSaveKeepsSetSubsetParallelAndResetsTheRest() throws {
        let vm = try makeHarness().viewModel
        XCTAssertFalse(vm.saveDraft(), "an incomplete draft is not saved")
        fillDraft(vm)
        XCTAssertEqual(vm.mode, .add)
        XCTAssertTrue(vm.saveDraft())

        XCTAssertEqual(vm.rows.count, 1)
        XCTAssertEqual(vm.rows[0].cardNumber, "1")
        XCTAssertEqual(vm.rows[0].quantity, 3)
        XCTAssertEqual(vm.rows[0].imageFront, ImageRef.scan("123"))

        XCTAssertEqual(vm.draft.setName, Fixtures.setName)
        XCTAssertEqual(vm.draft.subsetName, Fixtures.subsetName)
        XCTAssertEqual(vm.draft.parallelVariety, "Gold Refractor")
        XCTAssertEqual(vm.draft.price, QueueRow.defaultPrice, "price is not carried over")
        XCTAssertEqual(vm.draft.cardNumber, "")
        XCTAssertEqual(vm.draft.quantity, 1)
        XCTAssertEqual(vm.draft.imageFront, ImageRef.empty)
        XCTAssertEqual(vm.draft.imageBack, ImageRef.empty)
        XCTAssertNil(vm.draft.player)
        XCTAssertEqual(vm.draft.printRun, 50, "parallel autofill is re-applied for the kept parallel")
        XCTAssertNil(vm.editIndex)
        XCTAssertNotEqual(vm.draft.id, vm.rows[0].id)
    }

    func testEditThenSaveReplaces() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm, card: "1")
        vm.saveDraft()
        vm.selectCard("2")
        vm.saveDraft()
        XCTAssertEqual(vm.rows.map { $0.cardNumber }, ["1", "2"])

        vm.beginEdit(at: 0)
        XCTAssertEqual(vm.mode, .edit)
        XCTAssertEqual(vm.editIndex, 0)
        XCTAssertEqual(vm.draft.cardNumber, "1")
        XCTAssertEqual(vm.draft.id, vm.rows[0].id)

        vm.selectCard("10")
        vm.draft.quantity = 7
        vm.saveDraft()
        XCTAssertEqual(vm.rows.count, 2)
        XCTAssertEqual(vm.rows[0].cardNumber, "10")
        XCTAssertEqual(vm.rows[0].quantity, 7)
        XCTAssertEqual(vm.rows[0].player, "Player Ten")
        XCTAssertNil(vm.editIndex)
        XCTAssertEqual(vm.mode, .add)
    }

    func testCancelIsAFullReset() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm)
        vm.saveDraft()
        vm.beginEdit(at: 0)
        vm.cancelDraft()
        XCTAssertEqual(vm.draft.setName, "")
        XCTAssertEqual(vm.draft.subsetName, "")
        XCTAssertEqual(vm.draft.parallelVariety, "")
        XCTAssertEqual(vm.draft.cardNumber, "")
        XCTAssertEqual(vm.draft.quantity, 1)
        XCTAssertEqual(vm.draft.price, QueueRow.defaultPrice)
        XCTAssertEqual(vm.draft.imageFront, ImageRef.empty)
        XCTAssertNil(vm.draft.player)
        XCTAssertNil(vm.draft.printRun)
        XCTAssertNil(vm.editIndex)
        XCTAssertEqual(vm.rows.count, 1, "cancel never touches saved rows")
    }

    func testDeleteSemantics() throws {
        let vm = try makeHarness().viewModel
        for card in ["1", "2", "10"] {
            fillDraft(vm, card: card)
            vm.saveDraft()
        }
        vm.beginEdit(at: 2)
        XCTAssertEqual(vm.editIndex, 2)

        vm.delete(at: 0)
        XCTAssertEqual(vm.rows.map { $0.cardNumber }, ["2", "10"])
        XCTAssertEqual(vm.editIndex, 1, "an earlier delete shifts the edit index down")
        XCTAssertEqual(vm.draft.cardNumber, "10")

        vm.delete(at: IndexSet(integer: 1))
        XCTAssertEqual(vm.rows.map { $0.cardNumber }, ["2"])
        XCTAssertNil(vm.editIndex, "deleting the edited row resets the form")
        XCTAssertEqual(vm.draft.cardNumber, "")
        XCTAssertEqual(vm.draft.setName, Fixtures.setName, "reset keeps set/subset/parallel like reset_draft_form()")

        vm.beginEdit(at: 0)
        vm.delete(at: IndexSet(integer: 5))
        XCTAssertEqual(vm.rows.count, 1, "out-of-range offsets are ignored")
        XCTAssertEqual(vm.editIndex, 0)
    }

    func testMoveKeepsEditIndexOnTheSameRow() throws {
        let vm = try makeHarness().viewModel
        for card in ["1", "2", "10"] {
            fillDraft(vm, card: card)
            vm.saveDraft()
        }
        vm.beginEdit(at: 0)
        vm.move(from: IndexSet(integer: 0), to: 3)
        XCTAssertEqual(vm.rows.map { $0.cardNumber }, ["2", "10", "1"])
        XCTAssertEqual(vm.editIndex, 2)
    }

    func testApplyUploadedURIPatchesDraftAndRows() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm)
        vm.setImageRef(.photo(localFileName: "abc.jpg"), for: .front)
        vm.saveDraft()
        vm.setImageRef(.photo(localFileName: "abc.jpg"), for: .back)
        XCTAssertTrue(vm.rows[0].needsUpload)
        XCTAssertTrue(vm.hasPendingUploads)
        XCTAssertEqual(vm.pendingUploads().map { $0.fileName }, ["abc.jpg"])

        vm.applyUploadedURI("gs://bucket/uploads/2026-10/abc.jpg", forFile: "abc.jpg")
        XCTAssertEqual(vm.rows[0].imageFront.remoteUri, "gs://bucket/uploads/2026-10/abc.jpg")
        XCTAssertEqual(vm.draft.imageBack.remoteUri, "gs://bucket/uploads/2026-10/abc.jpg")
        XCTAssertFalse(vm.rows[0].needsUpload)
        XCTAssertFalse(vm.hasPendingUploads)
        XCTAssertEqual(vm.rows[0].toInput().imageFront, "gs://bucket/uploads/2026-10/abc.jpg")

        vm.applyUploadedURI("gs://other", forFile: "other.jpg")
        XCTAssertEqual(vm.rows[0].imageFront.remoteUri, "gs://bucket/uploads/2026-10/abc.jpg", "unrelated files are untouched")
    }

    func testUploadPendingUsesFileStemAsClientIdAndPatchesRows() async throws {
        let harness = try makeHarness()
        let vm = harness.viewModel
        let fake = FakeAPI()
        try harness.store.saveImage(Data("not-really-a-jpeg".utf8), fileName: "pendingphoto.jpg")

        fillDraft(vm)
        vm.setImageRef(.photo(localFileName: "pendingphoto.jpg"), for: .front)
        vm.setImageRef(.photo(localFileName: "missing-file.jpg"), for: .back)
        vm.saveDraft()

        let failures = await vm.uploadPending(api: fake)
        XCTAssertEqual(failures.count, 1)
        XCTAssertTrue(failures[0].contains("missing-file.jpg"))
        XCTAssertEqual(fake.uploadedClientIds, ["pendingphoto"])
        XCTAssertEqual(fake.uploadedSides, [.front])
        XCTAssertEqual(vm.rows[0].imageFront.remoteUri, "gs://bucket/uploads/2026-10/pendingphoto.jpg")
        XCTAssertEqual(vm.uploader.state(for: "pendingphoto.jpg"), .done("gs://bucket/uploads/2026-10/pendingphoto.jpg"))
        XCTAssertTrue(vm.rows[0].imageBack.needsUpload)
    }

    func testSaveNowAndRestoreRoundTrip() throws {
        let harness = try makeHarness()
        let vm = harness.viewModel
        fillDraft(vm, card: "1")
        vm.saveDraft()
        fillDraft(vm, card: "2")
        vm.saveDraft()
        vm.beginEdit(at: 1)
        vm.saveNow()
        XCTAssertNotNil(vm.lastSavedAt)

        let restored = QueueViewModel(store: harness.store, metadata: harness.metadata, uploader: ImageUploader())
        restored.restore()
        XCTAssertEqual(restored.rows, vm.rows)
        XCTAssertEqual(restored.draft, vm.draft)
        XCTAssertEqual(restored.editIndex, 1)
        XCTAssertEqual(restored.mode, .edit)
        XCTAssertEqual(restored.lastSavedAt, vm.lastSavedAt)
    }

    func testNamedDraftsRoundTrip() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm)
        vm.saveDraft()
        let named = try vm.saveAsDraft(name: "  Monday batch ")
        XCTAssertEqual(named.name, "Monday batch")
        XCTAssertEqual(vm.listDrafts().map { $0.id }, [named.id])

        vm.clearQueue()
        XCTAssertEqual(vm.rows.count, 0)
        try vm.openDraft(id: named.id)
        XCTAssertEqual(vm.rows.count, 1)
        XCTAssertEqual(vm.rows[0].player, "Player One", "autofill is re-run after opening")
        XCTAssertNil(vm.editIndex)

        vm.deleteDraft(id: named.id)
        XCTAssertTrue(vm.listDrafts().isEmpty)
        XCTAssertThrowsError(try vm.openDraft(id: named.id))
    }

    func testLoadLastSubmittedRestoresEightFieldsAndReappliesAutofill() async throws {
        let vm = try makeHarness().viewModel
        let fake = FakeAPI()
        fake.lastQueue = QueueGetResponse(
            kind: "queue",
            submittedAt: "2026-10-15T12:00:00+00:00",
            rows: [
                QueueRowInput(
                    setName: Fixtures.setName,
                    subsetName: Fixtures.subsetName,
                    parallelVariety: "Gold Refractor",
                    cardNumber: "2",
                    quantity: 4,
                    price: 9.99,
                    imageFront: "123",
                    imageBack: "gs://bucket/uploads/2026-10/abc.jpg"
                ),
                QueueRowInput(
                    setName: "Unknown Set",
                    subsetName: "Base",
                    parallelVariety: nil,
                    cardNumber: "1",
                    quantity: 1,
                    price: 0,
                    imageFront: "",
                    imageBack: ""
                ),
            ],
            counts: Counts(rows: 2, ok: 2, errors: 0),
            queueUri: "gs://bucket/queue/listing_queue_enriched.jsonl",
            draftUri: "gs://bucket/queue/listing_queue_draft.json"
        )
        fillDraft(vm)
        vm.saveDraft()

        let response = try await vm.loadLastSubmitted(api: fake)
        XCTAssertEqual(response.kind, "queue")
        XCTAssertEqual(vm.rows.count, 2)
        XCTAssertEqual(vm.rows[0].cardNumber, "2")
        XCTAssertEqual(vm.rows[0].player, "Player Two")
        XCTAssertEqual(vm.rows[0].printRun, 50)
        XCTAssertEqual(vm.rows[0].imageFront, ImageRef.scan("123"))
        XCTAssertTrue(vm.rows[0].imageBack.isUploaded)
        XCTAssertEqual(vm.rows[1].parallelVariety, "")
        XCTAssertNil(vm.rows[1].player, "no match in the metadata -> no autofill")
        XCTAssertEqual(vm.draft.setName, "", "the form is fully reset")
        XCTAssertNil(vm.editIndex)
    }

    func testFetchLastSubmittedPreviewsWithoutTouchingTheQueue() async throws {
        let vm = try makeHarness().viewModel
        let fake = FakeAPI()
        fake.lastQueue = QueueGetResponse(
            kind: "draft",
            submittedAt: "2026-10-15T12:00:00+00:00",
            rows: [
                QueueRowInput(
                    setName: Fixtures.setName,
                    subsetName: Fixtures.subsetName,
                    parallelVariety: nil,
                    cardNumber: "2",
                    quantity: 1,
                    price: 5,
                    imageFront: "",
                    imageBack: ""
                ),
            ],
            counts: Counts(rows: 1, ok: 1, errors: 0),
            queueUri: nil,
            draftUri: nil
        )
        fillDraft(vm)
        vm.saveDraft()
        let before = vm.rows

        let response = try await vm.fetchLastSubmitted(api: fake)
        XCTAssertEqual(vm.rows, before, "a preview must not replace the queue")
        let preview = vm.rows(from: response)
        XCTAssertEqual(preview.count, 1)
        XCTAssertEqual(preview[0].player, "Player Two", "the preview rows are autofilled")
        XCTAssertEqual(vm.rows, before)

        vm.replaceQueue(with: preview)
        XCTAssertEqual(vm.rows.map { $0.cardNumber }, ["2"])
        XCTAssertEqual(vm.draft.setName, "")
        XCTAssertNil(vm.editIndex)
    }

    func testNamedDraftCanBePreviewedInFull() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm)
        vm.saveDraft()
        let named = try vm.saveAsDraft(name: "Preview me")
        XCTAssertEqual(vm.namedDraft(id: named.id)?.rows.count, 1)
        XCTAssertNil(vm.namedDraft(id: UUID()))
        XCTAssertEqual(vm.autofilled(named.rows).first?.player, "Player One")
    }

    func testStagedScanNumberLeavesTheDraftAloneUntilCommitted() throws {
        let vm = try makeHarness().viewModel
        vm.stageScanNumber("12", side: .front)
        XCTAssertEqual(vm.draft.imageFront, .empty)
        XCTAssertEqual(vm.scanText(for: .front), "12")

        vm.commitStagedScans()
        XCTAssertEqual(vm.draft.imageFront, .scan("12"))

        vm.stageScanNumber("", side: .front)
        vm.commitStagedScans()
        XCTAssertEqual(vm.draft.imageFront, .empty)
    }

    func testSaveCommitsStagedScansAndResetDiscardsThem() throws {
        let vm = try makeHarness().viewModel
        fillDraft(vm)
        vm.stageScanNumber("456", side: .front)
        XCTAssertTrue(vm.saveDraft())
        XCTAssertEqual(vm.rows.last?.imageFront, .scan("456"))

        vm.stageScanNumber("789", side: .front)
        vm.cancelDraft()
        XCTAssertEqual(vm.scanText(for: .front), "")
        vm.commitStagedScans()
        XCTAssertEqual(vm.draft.imageFront, .empty)
    }

    func testClearingAStagedScanKeepsAPhoto() throws {
        let vm = try makeHarness().viewModel
        let photo = ImageRef.photo(localFileName: "abc.jpg")
        vm.setImageRef(photo, for: .back)
        vm.stageScanNumber("", side: .back)
        vm.commitStagedScans()
        XCTAssertEqual(vm.draft.imageBack, photo)
    }

    func testSetGroupsFollowTheIndex() throws {
        let vm = try makeHarness().viewModel
        XCTAssertEqual(vm.setGroups.map { $0.title }, ["2024", "2023"])
        XCTAssertEqual(vm.setGroups.flatMap { $0.options }.sorted(), vm.setOptions.sorted())
    }

    func testSaveDraftToServerSendsRawRows() async throws {
        let vm = try makeHarness().viewModel
        let fake = FakeAPI()
        fillDraft(vm)
        vm.saveDraft()
        let response = try await vm.saveDraftToServer(api: fake)
        XCTAssertEqual(response.kind, "draft")
        XCTAssertNil(response.queueUri)
        XCTAssertEqual(fake.draftRows.count, 1)
        XCTAssertEqual(fake.draftRows[0], vm.rows.map { $0.toInput() })
    }

    func testPreviewViewModelTracksStalenessAndRejection() async throws {
        let vm = try makeHarness().viewModel
        let fake = FakeAPI()
        fake.previewResponse = try Fixtures.preview()
        fillDraft(vm)
        vm.saveDraft()

        let preview = PreviewViewModel()
        XCTAssertFalse(preview.isCurrent(for: vm))
        await preview.runPreview(queue: vm, api: fake)
        XCTAssertEqual(preview.previewResponse?.counts.ok, 1)
        XCTAssertTrue(preview.isCurrent(for: vm))

        fillDraft(vm, card: "2")
        vm.saveDraft()
        XCTAssertFalse(preview.isCurrent(for: vm), "a changed queue invalidates the preview")

        let rejectedRow = RowResult(index: 1, ok: false, enriched: nil, error: "No checklist match", warnings: [])
        fake.submitError = APIError.queueRejected(message: "1 rows failed enrichment", rows: [rejectedRow])
        await preview.submit(queue: vm, api: fake)
        guard case .rejected(let message, let rows)? = preview.outcome else {
            return XCTFail("expected a rejected outcome, got \(String(describing: preview.outcome))")
        }
        XCTAssertEqual(message, "1 rows failed enrichment")
        XCTAssertEqual(rows.map { $0.index }, [1])

        fake.submitError = nil
        await preview.submit(queue: vm, api: fake)
        guard case .success(let response)? = preview.outcome else {
            return XCTFail("expected a success outcome")
        }
        XCTAssertEqual(response.counts.rows, 2)
        XCTAssertEqual(fake.submittedRows.last, vm.rows.map { $0.toInput() })
        XCTAssertTrue(preview.isCurrent(for: vm))
    }
}
