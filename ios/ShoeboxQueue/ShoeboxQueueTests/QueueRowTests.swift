import Foundation
import XCTest
@testable import ShoeboxQueue

final class QueueRowTests: XCTestCase {
    private func sampleRow() -> QueueRow {
        var row = QueueRow()
        row.setName = "2023 Topps Chrome"
        row.subsetName = "Base"
        row.parallelVariety = "Gold Refractor"
        row.cardNumber = "1"
        row.quantity = 2
        row.price = 12.5
        row.imageFront = .scan("123")
        row.imageBack = .photo(localFileName: "abc.jpg", remoteUri: "gs://bucket/uploads/2026-10/abc.jpg")
        row.player = "Player One"
        row.team = "Team A"
        row.setYear = "2023"
        row.subsetType = "Base"
        row.printRun = 50
        row.parallelNote = "/50"
        return row
    }

    private func jsonObject(_ data: Data) throws -> [String: Any] {
        let object = try JSONSerialization.jsonObject(with: data)
        return try XCTUnwrap(object as? [String: Any])
    }

    func testCodableRoundTrip() throws {
        let row = sampleRow()
        let data = try JSONCoding.fileEncoder.encode(row)
        let decoded = try JSONCoding.decoder.decode(QueueRow.self, from: data)
        XCTAssertEqual(decoded, row)

        let object = try jsonObject(data)
        XCTAssertNotNil(object["set_name"])
        XCTAssertNotNil(object["image_front"])
        XCTAssertNotNil(object["print_run"])
    }

    func testToInputEmitsExactlyEightSnakeCaseKeys() throws {
        let data = try JSONCoding.encoder.encode(sampleRow().toInput())
        let object = try jsonObject(data)
        XCTAssertEqual(
            Set(object.keys),
            ["set_name", "subset_name", "parallel_variety", "card_number", "quantity", "price", "image_front", "image_back"]
        )
        XCTAssertEqual(object["set_name"] as? String, "2023 Topps Chrome")
        XCTAssertEqual(object["parallel_variety"] as? String, "Gold Refractor")
        XCTAssertEqual(object["card_number"] as? String, "1")
        XCTAssertEqual(object["quantity"] as? Int, 2)
        XCTAssertEqual(object["price"] as? Double, 12.5)
        XCTAssertEqual(object["image_front"] as? String, "123", "scan numbers go through verbatim")
        XCTAssertEqual(object["image_back"] as? String, "gs://bucket/uploads/2026-10/abc.jpg", "uploaded photos become their gs:// URI")
    }

    func testToInputEmitsNullParallelWhenBlank() throws {
        var row = sampleRow()
        row.parallelVariety = "  "
        let input = row.toInput()
        XCTAssertNil(input.parallelVariety)

        let data = try JSONCoding.encoder.encode(input)
        let object = try jsonObject(data)
        XCTAssertTrue(object.keys.contains("parallel_variety"), "the key must be present")
        XCTAssertTrue(object["parallel_variety"] is NSNull, "and explicitly null")
    }

    func testToInputRoundsPriceToTwoDecimalsAndClampsQuantity() {
        var row = sampleRow()
        row.price = 12.3456
        row.quantity = 5000
        let input = row.toInput()
        XCTAssertEqual(input.price, 12.35, accuracy: 0.0001)
        XCTAssertEqual(input.quantity, 999)

        row.quantity = 0
        row.price = -3
        let clamped = row.toInput()
        XCTAssertEqual(clamped.quantity, 1)
        XCTAssertEqual(clamped.price, 0)
    }

    func testPendingPhotoSendsEmptyString() {
        var row = sampleRow()
        row.imageFront = .photo(localFileName: "pending.jpg")
        XCTAssertTrue(row.imageFront.needsUpload)
        XCTAssertTrue(row.needsUpload)
        XCTAssertEqual(row.toInput().imageFront, "")
    }

    func testFromInputAndFromWire() throws {
        let input = QueueRowInput(
            setName: "2023 Topps Chrome",
            subsetName: "Base",
            parallelVariety: nil,
            cardNumber: "2",
            quantity: 3,
            price: 4.5,
            imageFront: "123",
            imageBack: "gs://bucket/uploads/2026-10/abc.jpg"
        )
        let row = QueueRow.fromInput(input)
        XCTAssertEqual(row.parallelVariety, "")
        XCTAssertEqual(row.imageFront, ImageRef.scan("123"))
        XCTAssertEqual(row.imageBack, ImageRef.photo(localFileName: nil, remoteUri: "gs://bucket/uploads/2026-10/abc.jpg"))
        XCTAssertFalse(row.imageBack.needsUpload)
        XCTAssertTrue(row.imageBack.isUploaded)
        XCTAssertEqual(row.toInput(), input)

        XCTAssertEqual(ImageRef.fromWire(""), ImageRef.empty)
        XCTAssertEqual(ImageRef.fromWire("  "), ImageRef.empty)
        XCTAssertEqual(ImageRef.fromWire(" 42 ").wireValue, "42")
        XCTAssertEqual(ImageRef.fromWire("GS://bucket/x.jpg").kind, .photo)
    }

    func testNewRowStartsAtTheDefaultPrice() {
        XCTAssertEqual(QueueRow.defaultPrice, 0.99)
        XCTAssertEqual(QueueRow().price, 0.99)
        XCTAssertEqual(QueueRow().quantity, 1)
    }

    func testDecodingInputFromServerTolerantOfMissingKeys() throws {
        let json = #"{"set_name":"S","subset_name":"B","card_number":"1","parallel_variety":null}"#
        let input = try JSONCoding.decoder.decode(QueueRowInput.self, from: Data(json.utf8))
        XCTAssertEqual(input.setName, "S")
        XCTAssertNil(input.parallelVariety)
        XCTAssertEqual(input.quantity, 1)
        XCTAssertEqual(input.price, 0)
        XCTAssertEqual(input.imageFront, "")
    }

    func testRowsRequestEnvelope() throws {
        let data = try JSONCoding.encoder.encode(RowsRequest(rows: [sampleRow().toInput()]))
        let object = try jsonObject(data)
        XCTAssertEqual(Set(object.keys), ["rows"])
        let rows = try XCTUnwrap(object["rows"] as? [[String: Any]])
        XCTAssertEqual(rows.count, 1)
    }

    func testAutofillSummaryJoinsTheNonEmptyFields() {
        var row = sampleRow()
        row.parallelNote = nil
        XCTAssertEqual(row.autofillSummary, "Player One · Team A · 2023 · Base · /50")

        row.team = "  "
        row.printRun = nil
        row.note = "RC"
        XCTAssertEqual(row.autofillSummary, "Player One · 2023 · Base · RC")

        XCTAssertEqual(QueueRow().autofillSummary, "")
    }

    func testListLabelMatchesStreamlitCaption() {
        var row = sampleRow()
        XCTAssertEqual(row.listLabel(index: 0), "1. 2023 Topps Chrome | Base | Gold Refractor | 1")
        row.parallelVariety = ""
        XCTAssertEqual(row.listLabel(index: 4), "5. 2023 Topps Chrome | Base |  | 1")
    }

    func testIsComplete() {
        var row = QueueRow()
        XCTAssertFalse(row.isComplete)
        row.setName = "S"
        row.subsetName = "B"
        XCTAssertFalse(row.isComplete)
        row.cardNumber = "1"
        XCTAssertTrue(row.isComplete)
    }

    func testEnrichedFieldOrderHasTwentyOneKeys() {
        XCTAssertEqual(EnrichedFieldOrder.keys.count, 21)
        XCTAssertEqual(EnrichedFieldOrder.keys.first, "card_id")
        XCTAssertEqual(EnrichedFieldOrder.keys.last, "image_back")
        XCTAssertEqual(EnrichedFieldOrder.camelKey("derived_card_number"), "derivedCardNumber")
        XCTAssertEqual(EnrichedFieldOrder.camelKey("player"), "player")
    }
}
