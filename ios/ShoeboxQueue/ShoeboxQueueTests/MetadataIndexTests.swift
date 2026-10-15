import XCTest
@testable import ShoeboxQueue

final class MetadataIndexTests: XCTestCase {
    private func set(_ name: String, year: String? = nil, subsets: [String] = ["Base"], cardYear: String? = nil) -> MetadataSet {
        MetadataSet(
            setName: name,
            setYear: year,
            subsets: subsets.map {
                MetadataSubset(subsetName: $0, cards: [MetadataCard(cardNumber: "1", setYear: cardYear)], parallels: [])
            }
        )
    }

    private func metadata(_ sets: [MetadataSet]) -> Metadata {
        Metadata(version: "v", generatedAt: "now", ttlS: 3600, counts: MetadataCounts(), sets: sets)
    }

    func testSetsAreGroupedByYearNewestFirstKeepingServerOrderWithinAYear() {
        let index = MetadataIndex(metadata: metadata([
            set("2023 Bowman", year: "2023"),
            set("2023 Topps", year: "2023"),
            set("2024 Topps Chrome", year: "2024"),
            set("Vintage Mix", year: nil),
            set("2022 Panini", year: "2022"),
        ]))
        XCTAssertEqual(index.setNames, ["2023 Bowman", "2023 Topps", "2024 Topps Chrome", "Vintage Mix", "2022 Panini"])
        XCTAssertEqual(index.setGroups, [
            SetYearGroup(year: "2024", setNames: ["2024 Topps Chrome"]),
            SetYearGroup(year: "2023", setNames: ["2023 Bowman", "2023 Topps"]),
            SetYearGroup(year: "2022", setNames: ["2022 Panini"]),
            SetYearGroup(year: SetYearGroup.unknownYear, setNames: ["Vintage Mix"]),
        ])
    }

    func testYearFallsBackToCardsThenNamePrefix() {
        XCTAssertEqual(MetadataIndex.year(of: set("Whatever", year: " 2021 ")), "2021")
        XCTAssertEqual(MetadataIndex.year(of: set("Whatever", year: nil, cardYear: "2020")), "2020")
        XCTAssertEqual(MetadataIndex.year(of: set("2019 Topps", year: "", cardYear: "")), "2019")
        XCTAssertNil(MetadataIndex.year(of: set("Topps Heritage")))
        XCTAssertEqual(MetadataIndex.yearPrefix(of: "  1999 Vintage"), "1999")
        XCTAssertEqual(MetadataIndex.yearPrefix(of: "2024-25 Hoops"), "2024")
        XCTAssertNil(MetadataIndex.yearPrefix(of: "20245 Not a year"))
        XCTAssertNil(MetadataIndex.yearPrefix(of: "2024x"))
        XCTAssertNil(MetadataIndex.yearPrefix(of: "202 Short"))
    }

    func testFixtureGroupsUseTheServerYearOrTheCards() throws {
        let index = MetadataIndex(metadata: try Fixtures.metadata())
        XCTAssertEqual(index.setGroups.map { $0.year }, ["2024", "2023"])
        XCTAssertEqual(index.setGroups.flatMap { $0.setNames }.sorted(), index.setNames.sorted())
    }

    func testBaseIsHoistedToTheFrontOfTheSubsets() {
        XCTAssertEqual(MetadataIndex.baseFirst(["Autos", "base", "Stars", "Baseball Legends"]), ["base", "Autos", "Stars", "Baseball Legends"])
        XCTAssertEqual(MetadataIndex.baseFirst(["Base", "Autos"]), ["Base", "Autos"], "already in order: fixed point")
        let index = MetadataIndex(metadata: metadata([set("2024 Topps", year: "2024", subsets: ["Autographs", "Base", "Stars"])]))
        XCTAssertEqual(index.subsets(for: "2024 Topps"), ["Base", "Autographs", "Stars"])
    }
}
