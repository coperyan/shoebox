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

// MARK: - Listing runs (POST /queue/listings, GET /queue/listings, GET /queue/listings/{run_id})

/// One queue row's outcome inside a `ListingRun`.
struct ListingRunResult: Codable, Hashable {
    var sku: String
    var title: String
    var listingId: String?
    var offerId: String?
    var success: Bool
    var error: String?
}

/// A `create-listings` run record written by the API and updated by the Cloud Run job.
/// `state` is `requested`, `running`, `done` or `failed`; timestamps are ISO-8601 strings exactly
/// as the server emits them. `GET /queue/listings` omits `results`, so decoding defaults it to `[]`.
struct ListingRun: Codable, Hashable, Identifiable {
    static let stateRequested = "requested"
    static let stateRunning = "running"
    static let stateDone = "done"
    static let stateFailed = "failed"

    var runId: String
    var state: String
    var requestedAt: String
    var startedAt: String?
    var finishedAt: String?
    var publish: Bool
    var schedule: Bool
    var queueUri: String
    var metadataVersion: String?
    var rowsTotal: Int
    var rowsDone: Int
    var listed: Int
    var failed: Int
    var results: [ListingRunResult]
    var error: String?
    var execution: String?
    var host: String?

    var id: String {
        runId
    }

    /// `done` or `failed`: nothing more will change, polling can stop.
    var isFinished: Bool {
        state == Self.stateDone || state == Self.stateFailed
    }

    var isFailed: Bool {
        state == Self.stateFailed
    }

    /// `rows_done / rows_total` clamped to 0...1; 0 while the total is unknown.
    var progress: Double {
        guard rowsTotal > 0 else {
            return 0
        }
        return min(1, max(0, Double(rowsDone) / Double(rowsTotal)))
    }

    // Case names only: `.convertFromSnakeCase` maps `run_id` -> `runId` and so on.
    enum CodingKeys: String, CodingKey {
        case runId
        case state
        case requestedAt
        case startedAt
        case finishedAt
        case publish
        case schedule
        case queueUri
        case metadataVersion
        case rowsTotal
        case rowsDone
        case listed
        case failed
        case results
        case error
        case execution
        case host
    }

    init(
        runId: String,
        state: String,
        requestedAt: String,
        startedAt: String? = nil,
        finishedAt: String? = nil,
        publish: Bool,
        schedule: Bool,
        queueUri: String,
        metadataVersion: String? = nil,
        rowsTotal: Int,
        rowsDone: Int = 0,
        listed: Int = 0,
        failed: Int = 0,
        results: [ListingRunResult] = [],
        error: String? = nil,
        execution: String? = nil,
        host: String? = nil
    ) {
        self.runId = runId
        self.state = state
        self.requestedAt = requestedAt
        self.startedAt = startedAt
        self.finishedAt = finishedAt
        self.publish = publish
        self.schedule = schedule
        self.queueUri = queueUri
        self.metadataVersion = metadataVersion
        self.rowsTotal = rowsTotal
        self.rowsDone = rowsDone
        self.listed = listed
        self.failed = failed
        self.results = results
        self.error = error
        self.execution = execution
        self.host = host
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        runId = try container.decode(String.self, forKey: .runId)
        state = try container.decode(String.self, forKey: .state)
        requestedAt = try container.decode(String.self, forKey: .requestedAt)
        startedAt = try container.decodeIfPresent(String.self, forKey: .startedAt)
        finishedAt = try container.decodeIfPresent(String.self, forKey: .finishedAt)
        publish = try container.decode(Bool.self, forKey: .publish)
        schedule = try container.decode(Bool.self, forKey: .schedule)
        queueUri = try container.decodeIfPresent(String.self, forKey: .queueUri) ?? ""
        metadataVersion = try container.decodeIfPresent(String.self, forKey: .metadataVersion)
        rowsTotal = try container.decodeIfPresent(Int.self, forKey: .rowsTotal) ?? 0
        rowsDone = try container.decodeIfPresent(Int.self, forKey: .rowsDone) ?? 0
        listed = try container.decodeIfPresent(Int.self, forKey: .listed) ?? 0
        failed = try container.decodeIfPresent(Int.self, forKey: .failed) ?? 0
        results = try container.decodeIfPresent([ListingRunResult].self, forKey: .results) ?? []
        error = try container.decodeIfPresent(String.self, forKey: .error)
        execution = try container.decodeIfPresent(String.self, forKey: .execution)
        host = try container.decodeIfPresent(String.self, forKey: .host)
    }
}

/// `GET /queue/listings`: `{"runs": [...]}`, newest first.
struct ListingRunsResponse: Codable, Hashable {
    var runs: [ListingRun]
}

/// Request body for `POST /queue/listings`. The job never scrapes prices, so there is no flag for it.
struct StartListingRunRequest: Codable, Hashable {
    var publish: Bool
    var schedule: Bool
}
