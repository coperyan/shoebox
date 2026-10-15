import Foundation
import XCTest
@testable import ShoeboxQueue

final class CardNumberSortTests: XCTestCase {
    func testKeyReproducesPythonBuckets() {
        XCTAssertEqual(CardSort.key("1"), CardSort.Key(bucket: 0, number: 1, text: ""))
        XCTAssertEqual(CardSort.key(" 10 "), CardSort.Key(bucket: 0, number: 10, text: ""))
        XCTAssertEqual(CardSort.key("007"), CardSort.Key(bucket: 0, number: 7, text: ""))
        XCTAssertEqual(CardSort.key("1A"), CardSort.Key(bucket: 1, number: CardSort.infinity, text: "1A"))
        XCTAssertEqual(CardSort.key("a"), CardSort.Key(bucket: 1, number: CardSort.infinity, text: "A"))
        XCTAssertEqual(CardSort.key("RA-1"), CardSort.Key(bucket: 1, number: CardSort.infinity, text: "RA-1"))
        XCTAssertEqual(CardSort.key(nil), CardSort.Key(bucket: 2, number: CardSort.infinity, text: ""))
        XCTAssertEqual(CardSort.key(""), CardSort.Key(bucket: 2, number: CardSort.infinity, text: ""))
        XCTAssertEqual(CardSort.key("   "), CardSort.Key(bucket: 2, number: CardSort.infinity, text: ""))
        // Python's int() is unbounded; Swift falls back to the text bucket instead of crashing.
        XCTAssertEqual(CardSort.key("99999999999999999999").bucket, 1)
    }

    func testOrderingNumericThenTextThenBlank() {
        let derived: [String?] = ["A", "10", "", "1A", "2", nil, "1", "b"]
        let sorted = derived.enumerated()
            .sorted { lhs, rhs in
                let lk = CardSort.key(lhs.element)
                let rk = CardSort.key(rhs.element)
                return lk != rk ? lk < rk : lhs.offset < rhs.offset
            }
            .map { $0.element }
        XCTAssertEqual(sorted, ["1", "2", "10", "1A", "A", "b", "", nil])
    }

    func testFixtureOrderIsFixedPointOfSort() throws {
        let metadata = try Fixtures.metadata()
        var checked = 0
        for set in metadata.sets {
            for subset in set.subsets {
                XCTAssertEqual(CardSort.sorted(subset.cards), subset.cards, "\(set.setName) / \(subset.subsetName)")
                checked += 1
            }
        }
        XCTAssertEqual(checked, 3)
    }

    func testSortIsStable() {
        let cards = [
            MetadataCard(cardNumber: "x", derivedCardNumber: "7", player: "first"),
            MetadataCard(cardNumber: "y", derivedCardNumber: "7", player: "second"),
            MetadataCard(cardNumber: "z", derivedCardNumber: "3", player: "third"),
        ]
        let sorted = CardSort.sorted(cards)
        XCTAssertEqual(sorted.map { $0.cardNumber }, ["z", "x", "y"])
    }

    func testIndexPreservesServerOrder() throws {
        let index = MetadataIndex(metadata: try Fixtures.metadata())
        XCTAssertEqual(index.setNames, ["2023 Topps Chrome", "2024 Bowman"])
        XCTAssertEqual(index.subsets(for: "2023 Topps Chrome"), ["Base", "Rookie Autographs"])
        XCTAssertEqual(
            index.cardNumbers(set: Fixtures.setName, subset: Fixtures.subsetName),
            ["1", "2", "10", "1A", "A", "NNO"]
        )
        XCTAssertEqual(index.parallelNames(set: Fixtures.setName, subset: Fixtures.subsetName), ["Gold Refractor", "Refractor"])
        XCTAssertEqual(index.parallelNames(set: "2023 Topps Chrome", subset: "Rookie Autographs"), [])
        XCTAssertEqual(index.cardNumbers(set: "nope", subset: "nope"), [])

        let card = try XCTUnwrap(index.card(set: Fixtures.setName, subset: Fixtures.subsetName, cardNumber: "NNO"))
        XCTAssertNil(card.derivedCardNumber)
        XCTAssertEqual(card.player, "Checklist")

        let parallel = try XCTUnwrap(index.parallel(set: Fixtures.setName, subset: Fixtures.subsetName, variety: "Refractor"))
        XCTAssertNil(parallel.printRun)
        XCTAssertEqual(index.parallel(set: Fixtures.setName, subset: Fixtures.subsetName, variety: "Gold Refractor")?.printRun, 50)
    }

    func testIndexDeduplicatesCardsKeepingFirst() {
        let subset = MetadataSubset(
            subsetName: "Base",
            cards: [
                MetadataCard(cardNumber: "1", derivedCardNumber: "1", player: "first"),
                MetadataCard(cardNumber: "1", derivedCardNumber: "1", player: "duplicate"),
                MetadataCard(cardNumber: "2", derivedCardNumber: "2", player: "second"),
            ],
            parallels: [
                MetadataParallel(parallelVariety: "Gold", printRun: 50),
                MetadataParallel(parallelVariety: "Gold", printRun: 99),
            ]
        )
        let metadata = Metadata(
            version: "v",
            generatedAt: "2026-10-15T12:00:00+00:00",
            ttlS: 3600,
            counts: MetadataCounts(checklistRows: 3, parallelRows: 2, sets: 1),
            sets: [MetadataSet(setName: "S", subsets: [subset])]
        )
        let index = MetadataIndex(metadata: metadata)
        XCTAssertEqual(index.cardNumbers(set: "S", subset: "Base"), ["1", "2"])
        XCTAssertEqual(index.card(set: "S", subset: "Base", cardNumber: "1")?.player, "first")
        XCTAssertEqual(index.parallels(set: "S", subset: "Base").count, 1)
        XCTAssertEqual(index.parallel(set: "S", subset: "Base", variety: "Gold")?.printRun, 50)
    }
}
