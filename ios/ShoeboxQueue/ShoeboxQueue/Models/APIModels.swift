import Foundation

// MARK: - Generic JSON value (the enriched dict has mixed types)

enum JSONValue: Codable, Hashable {
    case null
    case bool(Bool)
    case int(Int)
    case double(Double)
    case string(String)
    case array([JSONValue])
    case object([String: JSONValue])

    init(from decoder: Decoder) throws {
        let container = try decoder.singleValueContainer()
        if container.decodeNil() {
            self = .null
            return
        }
        if let value = try? container.decode(Bool.self) {
            self = .bool(value)
            return
        }
        if let value = try? container.decode(Int.self) {
            self = .int(value)
            return
        }
        if let value = try? container.decode(Double.self) {
            self = .double(value)
            return
        }
        if let value = try? container.decode(String.self) {
            self = .string(value)
            return
        }
        if let value = try? container.decode([JSONValue].self) {
            self = .array(value)
            return
        }
        if let value = try? container.decode([String: JSONValue].self) {
            self = .object(value)
            return
        }
        throw DecodingError.dataCorruptedError(in: container, debugDescription: "Unsupported JSON value")
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.singleValueContainer()
        switch self {
        case .null:
            try container.encodeNil()
        case .bool(let value):
            try container.encode(value)
        case .int(let value):
            try container.encode(value)
        case .double(let value):
            try container.encode(value)
        case .string(let value):
            try container.encode(value)
        case .array(let value):
            try container.encode(value)
        case .object(let value):
            try container.encode(value)
        }
    }

    var isNull: Bool {
        if case .null = self {
            return true
        }
        return false
    }

    var stringValue: String? {
        if case .string(let value) = self {
            return value
        }
        return nil
    }

    /// Text for a LabeledContent row.
    var displayString: String {
        switch self {
        case .null:
            return ""
        case .bool(let value):
            return value ? "true" : "false"
        case .int(let value):
            return String(value)
        case .double(let value):
            if value == value.rounded() && abs(value) < 1e15 {
                return String(format: "%.2f", value)
            }
            return String(value)
        case .string(let value):
            return value
        case .array(let values):
            return "[" + values.map { $0.displayString }.joined(separator: ", ") + "]"
        case .object(let dict):
            return "{" + dict.keys.sorted().map { "\($0): \(dict[$0]?.displayString ?? "")" }.joined(separator: ", ") + "}"
        }
    }
}

// MARK: - The 21 enriched fields, in build_enriched_json order

enum EnrichedFieldOrder {
    /// Keys exactly as `shoebox/transforms/queue_enrichment.build_enriched_json` emits them.
    static let keys: [String] = [
        "card_id",
        "checklist_id",
        "subset_id",
        "parallel_id",
        "set_year",
        "set_name",
        "subset_name",
        "subset_type",
        "parallel_variety",
        "derived_card_number",
        "card_number",
        "player",
        "team",
        "note",
        "note_check",
        "print_run",
        "parallel_note",
        "quantity",
        "price",
        "image_front",
        "image_back",
    ]

    /// `"card_id"` -> `"cardId"`. Foundation's `.convertFromSnakeCase` is also applied to the keys of
    /// a `[String: JSONValue]` dictionary, so the enriched dict may come back with camelCase keys.
    static func camelKey(_ snakeKey: String) -> String {
        let parts = snakeKey.split(separator: "_", omittingEmptySubsequences: true).map(String.init)
        guard let first = parts.first else {
            return snakeKey
        }
        return ([first] + parts.dropFirst().map { $0.prefix(1).uppercased() + $0.dropFirst() }).joined()
    }
}

// MARK: - Preview / queue responses

struct Counts: Codable, Hashable {
    var rows: Int
    var ok: Int
    var errors: Int
}

struct RowResult: Codable, Hashable {
    var index: Int
    var ok: Bool
    var enriched: [String: JSONValue]?
    var error: String?
    var warnings: [String]?

    var warningMessages: [String] {
        warnings ?? []
    }

    /// Look a field up by its wire (snake_case) key, tolerating camelCase keys produced by the
    /// snake_case decoding strategy.
    func enrichedValue(_ snakeKey: String) -> JSONValue? {
        guard let enriched else {
            return nil
        }
        if let value = enriched[snakeKey] {
            return value
        }
        return enriched[EnrichedFieldOrder.camelKey(snakeKey)]
    }
}

struct PreviewResponse: Codable, Hashable {
    var rows: [RowResult]
    var counts: Counts
    var metadataVersion: String?
}

struct QueueWriteResponse: Codable, Hashable {
    var kind: String
    var submittedAt: String
    var counts: Counts
    var queueUri: String?
    var draftUri: String?
    var metadataVersion: String?
}

struct QueueGetResponse: Codable, Hashable {
    var kind: String?
    var submittedAt: String?
    var rows: [QueueRowInput]
    var counts: Counts?
    var queueUri: String?
    var draftUri: String?
}

struct UploadResponse: Codable, Hashable {
    var uri: String
    var side: String?
    var clientId: String?
    var bytes: Int?
    var contentType: String?
}

struct HealthResponse: Codable, Hashable {
    var status: String
    var version: String?
}

struct MetadataRefreshResponse: Codable, Hashable {
    var version: String
    var generatedAt: String?
}

/// Request body for preview / submit / draft: `{"rows": [...]}`.
struct RowsRequest: Codable, Hashable {
    var rows: [QueueRowInput]
}

// MARK: - Error bodies

/// `PUT /queue` 422: `{"detail": {"message": "...", "rows": [RowResult...]}}`.
struct QueueErrorDetail: Codable, Hashable {
    var message: String
    var rows: [RowResult]?
}

struct QueueErrorEnvelope: Codable, Hashable {
    var detail: QueueErrorDetail
}

/// FastAPI `HTTPException`: `{"detail": "..."}`.
struct StringDetailEnvelope: Codable, Hashable {
    var detail: String
}

/// FastAPI request validation: `{"detail": [{"loc": [...], "msg": "...", "type": "..."}]}`.
struct ValidationErrorItem: Codable, Hashable {
    var loc: [JSONValue]?
    var msg: String?
    var type: String?
}

struct ValidationErrorEnvelope: Codable, Hashable {
    var detail: [ValidationErrorItem]
}
