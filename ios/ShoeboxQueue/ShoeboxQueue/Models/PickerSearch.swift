import Foundation

/// One section of a `SearchablePicker`: an optional header and its options, in display order.
struct PickerGroup: Hashable {
    var title: String?
    var options: [String]

    init(title: String? = nil, options: [String]) {
        self.title = title
        self.options = options
    }
}

/// The search behaviour of the pickers, kept out of SwiftUI so it can be unit-tested.
///
/// Matching is case-insensitive "contains". Results are ranked so that pressing Search picks the
/// option a person most likely meant: an exact match first, then options that start with the
/// query, then the rest, each bucket keeping the list's own order (the metadata's order).
enum PickerSearch {
    enum Tier: Int, Comparable {
        case exact = 0
        case prefix = 1
        case contains = 2

        static func < (lhs: Tier, rhs: Tier) -> Bool {
            lhs.rawValue < rhs.rawValue
        }
    }

    static func normalized(_ query: String) -> String {
        query.trimmingCharacters(in: .whitespacesAndNewlines)
    }

    /// How well `option` matches the (already trimmed, non-empty) query; nil when it does not.
    static func tier(of option: String, query: String) -> Tier? {
        let folded = option.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
        let needle = query.folding(options: [.caseInsensitive, .diacriticInsensitive], locale: .current)
        if folded == needle {
            return .exact
        }
        if folded.hasPrefix(needle) {
            return .prefix
        }
        if folded.contains(needle) {
            return .contains
        }
        return nil
    }

    /// The options that match `query`, best matches first; every option when the query is blank.
    static func rank(_ options: [String], query: String) -> [String] {
        let needle = normalized(query)
        if needle.isEmpty {
            return options
        }
        let scored: [(offset: Int, tier: Tier, option: String)] = options.enumerated().compactMap { offset, option in
            guard let matchTier = Self.tier(of: option, query: needle) else {
                return nil
            }
            return (offset: offset, tier: matchTier, option: option)
        }
        return scored
            .sorted { lhs, rhs in
                lhs.tier != rhs.tier ? lhs.tier < rhs.tier : lhs.offset < rhs.offset
            }
            .map { $0.option }
    }

    /// `rank` applied per group; groups left with nothing are dropped.
    static func filter(_ groups: [PickerGroup], query: String) -> [PickerGroup] {
        groups.compactMap { group in
            let ranked = rank(group.options, query: query)
            return ranked.isEmpty ? nil : PickerGroup(title: group.title, options: ranked)
        }
    }

    /// What the Search key selects: the best match across every group, or nil for a blank query
    /// or no match. An exact match anywhere beats a prefix match in an earlier group.
    static func firstMatch(in groups: [PickerGroup], query: String) -> String? {
        let needle = normalized(query)
        guard !needle.isEmpty else {
            return nil
        }
        return rank(groups.flatMap { $0.options }, query: needle).first
    }
}
