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

    init(rows: [QueueRow] = [], draft: QueueRow = QueueRow(), editIndex: Int? = nil, savedAt: String? = nil) {
        self.rows = rows
        self.draft = draft
        self.editIndex = editIndex
        self.savedAt = savedAt
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
