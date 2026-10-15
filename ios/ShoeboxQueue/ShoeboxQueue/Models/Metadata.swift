import Foundation

// MARK: - Wire shapes for GET /metadata (MetadataResponse)

struct Metadata: Codable, Hashable {
    var version: String
    var generatedAt: String
    var ttlS: Int
    var counts: MetadataCounts
    var sets: [MetadataSet]
}

struct MetadataCounts: Codable, Hashable {
    var checklistRows: Int?
    var parallelRows: Int?
    var sets: Int?
}

struct MetadataSet: Codable, Hashable {
    var setName: String
    /// The year the set is filed under (`set_year_for` on the server). Older cached payloads have
    /// none; `MetadataIndex` then falls back to the cards' `set_year` and the name's leading digits.
    var setYear: String?
    var subsets: [MetadataSubset]

    init(setName: String, setYear: String? = nil, subsets: [MetadataSubset]) {
        self.setName = setName
        self.setYear = setYear
        self.subsets = subsets
    }
}

struct MetadataSubset: Codable, Hashable {
    var subsetName: String
    var cards: [MetadataCard]
    var parallels: [MetadataParallel]
}

/// One checklist card. Every leaf except `cardNumber` may be null on the wire.
struct MetadataCard: Codable, Hashable {
    var cardNumber: String
    var derivedCardNumber: String?
    var cardId: String?
    var subsetId: String?
    var setYear: String?
    var subsetType: String?
    var player: String?
    var team: String?
    var note: String?
    var noteCheck: String?

    init(
        cardNumber: String,
        derivedCardNumber: String? = nil,
        cardId: String? = nil,
        subsetId: String? = nil,
        setYear: String? = nil,
        subsetType: String? = nil,
        player: String? = nil,
        team: String? = nil,
        note: String? = nil,
        noteCheck: String? = nil
    ) {
        self.cardNumber = cardNumber
        self.derivedCardNumber = derivedCardNumber
        self.cardId = cardId
        self.subsetId = subsetId
        self.setYear = setYear
        self.subsetType = subsetType
        self.player = player
        self.team = team
        self.note = note
        self.noteCheck = noteCheck
    }
}

/// One row of the parallels view for a (set, subset).
struct MetadataParallel: Codable, Hashable {
    var parallelVariety: String
    var parallelId: String?
    var printRun: Int?
    var otherNote: String?

    init(parallelVariety: String, parallelId: String? = nil, printRun: Int? = nil, otherNote: String? = nil) {
        self.parallelVariety = parallelVariety
        self.parallelId = parallelId
        self.printRun = printRun
        self.otherNote = otherNote
    }
}

// MARK: - In-memory index (the Swift twin of build_indexes)

/// The Set picker's sections: one per year, newest first, sets in server order within a year.
struct SetYearGroup: Hashable {
    /// Section title for sets with no year at all.
    static let unknownYear = "Other"

    let year: String
    let setNames: [String]
}

/// Fast lookups for the pickers. Everything stays in **server order**: sets and parallels arrive
/// sorted, subsets arrive `Base` first then sorted, and cards arrive in `build_indexes` order
/// (numeric derived numbers first, then text, blanks last). The client never re-sorts, with one
/// exception mirrored from `subset_sort_key`: a `Base` subset is hoisted to the front so a cached
/// payload from before that rule reads the same. `CardSort` exists only as a parity check.
struct MetadataIndex {
    struct Key: Hashable {
        let set: String
        let subset: String
    }

    let setNames: [String]
    /// `setNames` segmented by year for the Set picker (`SetYearGroup`), newest year first.
    let setGroups: [SetYearGroup]
    private let subsetsBySet: [String: [String]]
    private let cardsByKey: [Key: [MetadataCard]]
    private let parallelsByKey: [Key: [MetadataParallel]]

    static let empty = MetadataIndex(setNames: [], setGroups: [], subsetsBySet: [:], cardsByKey: [:], parallelsByKey: [:])

    private init(
        setNames: [String],
        setGroups: [SetYearGroup],
        subsetsBySet: [String: [String]],
        cardsByKey: [Key: [MetadataCard]],
        parallelsByKey: [Key: [MetadataParallel]]
    ) {
        self.setNames = setNames
        self.setGroups = setGroups
        self.subsetsBySet = subsetsBySet
        self.cardsByKey = cardsByKey
        self.parallelsByKey = parallelsByKey
    }

    init(metadata: Metadata) {
        var setNames: [String] = []
        var seenSets = Set<String>()
        var yearBySet: [String: String?] = [:]
        var subsetsBySet: [String: [String]] = [:]
        var cardsByKey: [Key: [MetadataCard]] = [:]
        var parallelsByKey: [Key: [MetadataParallel]] = [:]

        for set in metadata.sets {
            let setName = set.setName
            if setName.isEmpty || seenSets.contains(setName) {
                continue
            }
            seenSets.insert(setName)
            setNames.append(setName)
            yearBySet[setName] = Self.year(of: set)

            var subsetNames: [String] = []
            var seenSubsets = Set<String>()
            for subset in set.subsets {
                let subsetName = subset.subsetName
                if subsetName.isEmpty || seenSubsets.contains(subsetName) {
                    continue
                }
                seenSubsets.insert(subsetName)
                subsetNames.append(subsetName)

                let key = Key(set: setName, subset: subsetName)

                // drop_duplicates(subset=["card_number"]) keeps the first occurrence.
                var cards: [MetadataCard] = []
                var seenCards = Set<String>()
                for card in subset.cards where !card.cardNumber.isEmpty {
                    if seenCards.contains(card.cardNumber) {
                        continue
                    }
                    seenCards.insert(card.cardNumber)
                    cards.append(card)
                }
                cardsByKey[key] = cards

                var parallels: [MetadataParallel] = []
                var seenParallels = Set<String>()
                for parallel in subset.parallels where !parallel.parallelVariety.isEmpty {
                    if seenParallels.contains(parallel.parallelVariety) {
                        continue
                    }
                    seenParallels.insert(parallel.parallelVariety)
                    parallels.append(parallel)
                }
                parallelsByKey[key] = parallels
            }
            subsetsBySet[setName] = Self.baseFirst(subsetNames)
        }

        self.init(
            setNames: setNames,
            setGroups: Self.groupByYear(setNames, year: { yearBySet[$0] ?? nil }),
            subsetsBySet: subsetsBySet,
            cardsByKey: cardsByKey,
            parallelsByKey: parallelsByKey
        )
    }

    var isEmpty: Bool {
        setNames.isEmpty
    }

    // MARK: Ordering rules shared with the server

    /// `subset_sort_key` (shoebox/transforms/queue_indexes.py): `Base` (any case) first, the rest
    /// untouched. Stable, so a payload that already honours the rule is a fixed point.
    static func baseFirst(_ subsets: [String]) -> [String] {
        let isBase: (String) -> Bool = { $0.trimmingCharacters(in: .whitespaces).lowercased() == "base" }
        return subsets.filter(isBase) + subsets.filter { !isBase($0) }
    }

    /// `set_year_for`: the server's `set_year`, else the first card year in the set, else the
    /// four digits the name starts with.
    static func year(of set: MetadataSet) -> String? {
        if let year = set.setYear?.trimmingCharacters(in: .whitespaces), !year.isEmpty {
            return year
        }
        for subset in set.subsets {
            for card in subset.cards {
                if let year = card.setYear?.trimmingCharacters(in: .whitespaces), !year.isEmpty {
                    return year
                }
            }
        }
        return yearPrefix(of: set.setName)
    }

    /// `re.match(r"\s*(\d{4})\b", name)`: exactly four leading digits followed by a non-word
    /// character or the end.
    static func yearPrefix(of name: String) -> String? {
        let trimmed = name.drop { $0.isWhitespace }
        let digits = trimmed.prefix { $0.isASCII && $0.isNumber }
        guard digits.count == 4 else {
            return nil
        }
        let rest = trimmed.dropFirst(4)
        if let next = rest.first, next.isLetter || next.isNumber || next == "_" {
            return nil
        }
        return String(digits)
    }

    /// Sections for the Set picker: years descending (numeric years first, then any other text,
    /// then `SetYearGroup.unknownYear`), set names in their incoming order within each.
    static func groupByYear(_ setNames: [String], year: (String) -> String?) -> [SetYearGroup] {
        var order: [String] = []
        var members: [String: [String]] = [:]
        for name in setNames {
            let key = year(name) ?? SetYearGroup.unknownYear
            if members[key] == nil {
                order.append(key)
                members[key] = []
            }
            members[key, default: []].append(name)
        }
        let sortedYears = order.sorted { lhs, rhs in
            switch (Int(lhs), Int(rhs)) {
            case (let l?, let r?):
                return l > r
            case (_?, nil):
                return true
            case (nil, _?):
                return false
            case (nil, nil):
                if lhs == SetYearGroup.unknownYear {
                    return false
                }
                if rhs == SetYearGroup.unknownYear {
                    return true
                }
                return lhs > rhs
            }
        }
        return sortedYears.map { SetYearGroup(year: $0, setNames: members[$0] ?? []) }
    }

    func subsets(for set: String) -> [String] {
        subsetsBySet[set] ?? []
    }

    func cards(set: String, subset: String) -> [MetadataCard] {
        cardsByKey[Key(set: set, subset: subset)] ?? []
    }

    func cardNumbers(set: String, subset: String) -> [String] {
        cards(set: set, subset: subset).map { $0.cardNumber }
    }

    func parallels(set: String, subset: String) -> [MetadataParallel] {
        parallelsByKey[Key(set: set, subset: subset)] ?? []
    }

    func parallelNames(set: String, subset: String) -> [String] {
        parallels(set: set, subset: subset).map { $0.parallelVariety }
    }

    /// First checklist row for (set, subset, card_number), like `cg.iloc[0]` in the UI.
    func card(set: String, subset: String, cardNumber: String) -> MetadataCard? {
        cards(set: set, subset: subset).first { $0.cardNumber == cardNumber }
    }

    /// First parallels-view row for (set, subset, parallel_variety), like `pg.iloc[0]` in the UI.
    func parallel(set: String, subset: String, variety: String) -> MetadataParallel? {
        parallels(set: set, subset: subset).first { $0.parallelVariety == variety }
    }
}

// MARK: - Port of _cardno_sort_key (shoebox/ui/app.py)

/// Swift twin of the Python sort key used by `build_indexes`:
///
/// ```python
/// def _cardno_sort_key(derived):
///     if derived is None or NaN: return (2, inf, "")
///     s = str(derived).strip()
///     if not s: return (2, inf, "")
///     if re.fullmatch(r"\d+", s): return (0, int(s), "")
///     return (1, inf, s.upper())
/// ```
///
/// Used by the parity test and as a fallback if the server order is ever lost. The sort is stable.
enum CardSort {
    struct Key: Hashable, Comparable {
        let bucket: Int
        let number: Int
        let text: String

        static func < (lhs: Key, rhs: Key) -> Bool {
            if lhs.bucket != rhs.bucket {
                return lhs.bucket < rhs.bucket
            }
            if lhs.number != rhs.number {
                return lhs.number < rhs.number
            }
            return lhs.text < rhs.text
        }
    }

    /// `float("inf")` stand-in for the non-numeric buckets.
    static let infinity = Int.max

    static func key(_ derived: String?) -> Key {
        guard let derived else {
            return Key(bucket: 2, number: infinity, text: "")
        }
        let s = derived.trimmingCharacters(in: .whitespacesAndNewlines)
        if s.isEmpty {
            return Key(bucket: 2, number: infinity, text: "")
        }
        if isAllDigits(s) {
            if let n = Int(s) {
                return Key(bucket: 0, number: n, text: "")
            }
            // Python's int() is unbounded; a value that overflows Int sorts with the text bucket.
            return Key(bucket: 1, number: infinity, text: s.uppercased())
        }
        return Key(bucket: 1, number: infinity, text: s.uppercased())
    }

    /// `re.fullmatch(r"\d+", s)` — Unicode decimal digits (category Nd), one or more.
    static func isAllDigits(_ s: String) -> Bool {
        !s.isEmpty && s.unicodeScalars.allSatisfy { CharacterSet.decimalDigits.contains($0) }
    }

    /// Stable sort by `derivedCardNumber`; ties keep their input order.
    static func sorted(_ cards: [MetadataCard]) -> [MetadataCard] {
        cards.enumerated()
            .sorted { lhs, rhs in
                let lk = key(lhs.element.derivedCardNumber)
                let rk = key(rhs.element.derivedCardNumber)
                if lk != rk {
                    return lk < rk
                }
                return lhs.offset < rhs.offset
            }
            .map { $0.element }
    }
}
