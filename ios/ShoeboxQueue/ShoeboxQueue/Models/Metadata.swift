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
    var subsets: [MetadataSubset]
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

/// Fast lookups for the pickers. Everything stays in **server order**: sets, subsets and parallels
/// arrive sorted, and cards arrive in `build_indexes` order (numeric derived numbers first, then
/// text, blanks last). The client never re-sorts; `CardSort` exists only as a parity check.
struct MetadataIndex {
    struct Key: Hashable {
        let set: String
        let subset: String
    }

    let setNames: [String]
    private let subsetsBySet: [String: [String]]
    private let cardsByKey: [Key: [MetadataCard]]
    private let parallelsByKey: [Key: [MetadataParallel]]

    static let empty = MetadataIndex(setNames: [], subsetsBySet: [:], cardsByKey: [:], parallelsByKey: [:])

    private init(
        setNames: [String],
        subsetsBySet: [String: [String]],
        cardsByKey: [Key: [MetadataCard]],
        parallelsByKey: [Key: [MetadataParallel]]
    ) {
        self.setNames = setNames
        self.subsetsBySet = subsetsBySet
        self.cardsByKey = cardsByKey
        self.parallelsByKey = parallelsByKey
    }

    init(metadata: Metadata) {
        var setNames: [String] = []
        var seenSets = Set<String>()
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
            subsetsBySet[setName] = subsetNames
        }

        self.init(setNames: setNames, subsetsBySet: subsetsBySet, cardsByKey: cardsByKey, parallelsByKey: parallelsByKey)
    }

    var isEmpty: Bool {
        setNames.isEmpty
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
