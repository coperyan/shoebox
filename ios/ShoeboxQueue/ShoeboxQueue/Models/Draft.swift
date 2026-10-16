import Foundation

/// ISO-8601 timestamps as strings (the app never decodes `Date`).
enum Timestamps {
    static func now() -> String {
        ISO8601DateFormatter().string(from: Date())
    }
}

/// What `current.json` holds: the Streamlit session state (`rows`, `draft`, `edit_index`).
struct WorkingState: Codable, Hashable {
    var rows: [QueueRow]
    var draft: QueueRow
    var editIndex: Int?
    var savedAt: String?
    /// A card batch in progress (absent in files written before batches existed).
    var batch: CardBatch?

    init(rows: [QueueRow] = [], draft: QueueRow = QueueRow(), editIndex: Int? = nil, savedAt: String? = nil, batch: CardBatch? = nil) {
        self.rows = rows
        self.draft = draft
        self.editIndex = editIndex
        self.savedAt = savedAt
        self.batch = batch
    }
}

/// Card numbers checked together in the Card # list. The form takes them one at a time: each
/// Save in add mode queues the current card and loads the next one.
struct CardBatch: Codable, Hashable {
    var cards: [String]
    var position: Int = 0

    var current: String? {
        cards.indices.contains(position) ? cards[position] : nil
    }

    /// Cards after the current one.
    var upcoming: [String] {
        cards.indices.contains(position) ? Array(cards[(position + 1)...]) : []
    }

    /// "Card 2 of 5".
    var progress: String {
        "Card \(min(position + 1, cards.count)) of \(cards.count)"
    }
}

/// A named local draft: `drafts/<uuid>.json`.
struct NamedDraft: Codable, Hashable, Identifiable {
    var id: UUID
    var name: String
    var createdAt: String
    var updatedAt: String
    var rows: [QueueRow]

    init(id: UUID = UUID(), name: String, createdAt: String, updatedAt: String, rows: [QueueRow]) {
        self.id = id
        self.name = name
        self.createdAt = createdAt
        self.updatedAt = updatedAt
        self.rows = rows
    }

    var summary: NamedDraftSummary {
        NamedDraftSummary(id: id, name: name, updatedAt: updatedAt, rowCount: rows.count)
    }
}

/// What the Drafts sheet lists.
struct NamedDraftSummary: Hashable, Identifiable {
    let id: UUID
    let name: String
    let updatedAt: String
    let rowCount: Int
}
