import XCTest
@testable import ShoeboxQueue

final class PickerSearchTests: XCTestCase {
    private let cards = ["1", "2", "10", "11", "12", "1A", "RA-1"]

    func testBlankQueryKeepsEverythingInOrder() {
        XCTAssertEqual(PickerSearch.rank(cards, query: "   "), cards)
        XCTAssertNil(PickerSearch.firstMatch(in: [PickerGroup(options: cards)], query: " "))
    }

    func testRankIsExactThenPrefixThenContainsInListOrder() {
        XCTAssertEqual(PickerSearch.rank(cards, query: "1"), ["1", "10", "11", "12", "1A", "RA-1"])
        XCTAssertEqual(PickerSearch.rank(cards, query: "a"), ["1A", "RA-1"])
        XCTAssertEqual(PickerSearch.rank(["Gold Refractor", "Gold", "Rose Gold"], query: "gold"), ["Gold", "Gold Refractor", "Rose Gold"])
        XCTAssertEqual(PickerSearch.rank(cards, query: "xyz"), [])
    }

    func testSearchKeyPicksTheBestMatchAcrossGroups() {
        let groups = [
            PickerGroup(title: "2024", options: ["2024 Topps Chrome", "2024 Bowman"]),
            PickerGroup(title: "2023", options: ["2023 Topps Chrome"]),
        ]
        XCTAssertEqual(PickerSearch.firstMatch(in: groups, query: "chrome"), "2024 Topps Chrome")
        XCTAssertEqual(PickerSearch.firstMatch(in: groups, query: "2023 topps chrome"), "2023 Topps Chrome", "exact beats earlier groups")
        XCTAssertEqual(PickerSearch.firstMatch(in: groups, query: "bow"), "2024 Bowman")
        XCTAssertNil(PickerSearch.firstMatch(in: groups, query: "panini"))
        XCTAssertEqual(PickerSearch.firstMatch(in: [PickerGroup(options: cards)], query: "1"), "1")
        XCTAssertEqual(PickerSearch.firstMatch(in: [PickerGroup(options: cards)], query: "ra"), "RA-1")
    }

    func testFilterDropsEmptyGroupsAndRanksWithinEach() {
        let groups = [
            PickerGroup(title: "2024", options: ["2024 Topps Chrome", "2024 Bowman"]),
            PickerGroup(title: "2023", options: ["2023 Topps Chrome"]),
        ]
        let filtered = PickerSearch.filter(groups, query: "bowman")
        XCTAssertEqual(filtered, [PickerGroup(title: "2024", options: ["2024 Bowman"])])
        XCTAssertEqual(PickerSearch.filter(groups, query: ""), groups)
    }
}
