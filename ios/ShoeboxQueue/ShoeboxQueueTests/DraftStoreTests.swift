import Foundation
import XCTest
@testable import ShoeboxQueue

final class DraftStoreTests: XCTestCase {
    private func makeStore() throws -> DraftStore {
        let directory = try TempDirectory.make()
        addTeardownBlock {
            try? FileManager.default.removeItem(at: directory)
        }
        return DraftStore(baseURL: directory)
    }

    private func row(_ card: String, front: ImageRef = .empty) -> QueueRow {
        var row = QueueRow()
        row.setName = "S"
        row.subsetName = "B"
        row.cardNumber = card
        row.imageFront = front
        return row
    }

    func testCurrentStateRoundTrip() throws {
        let store = try makeStore()
        XCTAssertNil(store.loadCurrent())

        let state = WorkingState(rows: [row("1"), row("2")], draft: row("3"), editIndex: 1, savedAt: "2026-10-15T12:00:00Z")
        try store.saveCurrent(state)
        XCTAssertTrue(FileManager.default.fileExists(atPath: store.currentURL.path))
        XCTAssertEqual(store.loadCurrent(), state)

        let json = try XCTUnwrap(String(data: Data(contentsOf: store.currentURL), encoding: .utf8))
        XCTAssertTrue(json.contains("\"edit_index\""))
        XCTAssertTrue(json.contains("\"saved_at\""))

        store.clearCurrent()
        XCTAssertNil(store.loadCurrent())
    }

    func testNamedDraftsAreListedNewestFirst() throws {
        let store = try makeStore()
        XCTAssertTrue(store.listDrafts().isEmpty)

        let older = NamedDraft(name: "older", createdAt: "2026-01-01T00:00:00Z", updatedAt: "2026-01-01T00:00:00Z", rows: [row("1")])
        let newer = NamedDraft(name: "newer", createdAt: "2026-02-01T00:00:00Z", updatedAt: "2026-02-01T00:00:00Z", rows: [row("1"), row("2")])
        try store.saveDraft(older)
        try store.saveDraft(newer)

        let listed = store.listDrafts()
        XCTAssertEqual(listed.map { $0.name }, ["newer", "older"])
        XCTAssertEqual(listed.map { $0.rowCount }, [2, 1])
        XCTAssertEqual(store.loadDraft(id: older.id), older)
        XCTAssertNil(store.loadDraft(id: UUID()))

        try store.deleteDraft(id: older.id)
        XCTAssertEqual(store.listDrafts().map { $0.id }, [newer.id])
        XCTAssertThrowsError(try store.deleteDraft(id: older.id))
    }

    func testImagesAndOrphanPrune() throws {
        let store = try makeStore()
        let payload = Data("jpeg-bytes".utf8)
        let name = try store.saveImage(payload)
        XCTAssertTrue(name.hasSuffix(".jpg"))
        XCTAssertTrue(store.imageExists(fileName: name))
        XCTAssertEqual(store.loadImage(fileName: name), payload)
        XCTAssertEqual(store.imageURL(fileName: name).lastPathComponent, name)

        let named = try store.saveImage(payload, fileName: "explicit.jpg")
        XCTAssertEqual(named, "explicit.jpg")
        let orphan = try store.saveImage(payload, fileName: "orphan.jpg")

        try store.saveCurrent(WorkingState(rows: [row("1", front: .photo(localFileName: name))], draft: QueueRow()))
        try store.saveDraft(NamedDraft(name: "d", createdAt: "t", updatedAt: "t", rows: [row("2", front: .photo(localFileName: named))]))

        let removed = store.pruneOrphanImages(keeping: [])
        XCTAssertEqual(removed, 1)
        XCTAssertTrue(store.imageExists(fileName: name))
        XCTAssertTrue(store.imageExists(fileName: named))
        XCTAssertFalse(store.imageExists(fileName: orphan))

        store.deleteImage(fileName: name)
        XCTAssertFalse(store.imageExists(fileName: name))
        XCTAssertNil(store.loadImage(fileName: name))
    }
}
