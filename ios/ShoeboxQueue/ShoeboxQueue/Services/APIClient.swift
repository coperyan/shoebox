import Foundation

// This is the only file that knows the HTTP side of the contract: paths, headers, multipart
// bodies and error envelopes. Wire shapes live in Models/APIModels.swift, Models/Metadata.swift
// and Models/QueueRow.swift.

struct APIConfig: Hashable {
    var baseURL: URL
    var apiKey: String
}

enum APIError: Error, LocalizedError {
    case notConfigured
    case invalidResponse
    case http(status: Int, message: String)
    /// `PUT /queue` refused the queue: nothing was written, and `rows` carries the per-row results.
    case queueRejected(message: String, rows: [RowResult])
    case decoding(String)
    case transport(String)

    var errorDescription: String? {
        switch self {
        case .notConfigured:
            return "Set the API base URL and key in Settings first."
        case .invalidResponse:
            return "The server returned something that is not an HTTP response."
        case .http(_, let message):
            return message
        case .queueRejected(let message, let rows):
            let failures = rows.filter { !$0.ok }
            if failures.isEmpty {
                return message
            }
            let lines = failures.map { "Row \($0.index + 1): \($0.error ?? "error")" }
            return ([message] + lines).joined(separator: "\n")
        case .decoding(let detail):
            return "Could not read the server response: \(detail)"
        case .transport(let detail):
            return detail
        }
    }

    var status: Int? {
        switch self {
        case .http(let status, _):
            return status
        case .queueRejected:
            return 422
        default:
            return nil
        }
    }
}

enum MetadataFetchResult {
    case notModified
    case updated(Metadata, etag: String?)
}

typealias UploadProgressHandler = @Sendable (Double) -> Void

/// Everything the view models need from the server. `APIClient` is the real implementation; the
/// tests use a fake.
protocol QueueAPI {
    func health() async throws -> HealthResponse
    func fetchMetadata(ifNoneMatch etag: String?) async throws -> MetadataFetchResult
    func refreshMetadata() async throws -> MetadataRefreshResponse
    func previewQueue(_ rows: [QueueRowInput]) async throws -> PreviewResponse
    func submitQueue(_ rows: [QueueRowInput]) async throws -> QueueWriteResponse
    func saveDraft(_ rows: [QueueRowInput]) async throws -> QueueWriteResponse
    func fetchLastQueue() async throws -> QueueGetResponse
    func uploadImage(jpeg: Data, side: ImageSide, clientId: String?, progress: UploadProgressHandler?) async throws -> UploadResponse
}

// MARK: - Shared JSON coding (wire and disk files use the same snake_case convention)

enum JSONCoding {
    static var encoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        return encoder
    }

    /// Deterministic output for files on disk.
    static var fileEncoder: JSONEncoder {
        let encoder = JSONEncoder()
        encoder.keyEncodingStrategy = .convertToSnakeCase
        encoder.outputFormatting = [.sortedKeys, .prettyPrinted]
        return encoder
    }

    static var decoder: JSONDecoder {
        let decoder = JSONDecoder()
        decoder.keyDecodingStrategy = .convertFromSnakeCase
        return decoder
    }
}

// MARK: - Multipart

struct MultipartBody {
    let boundary: String
    private var body = Data()

    init(boundary: String = "Boundary-" + UUID().uuidString) {
        self.boundary = boundary
    }

    var contentType: String {
        "multipart/form-data; boundary=\(boundary)"
    }

    mutating func addField(name: String, value: String) {
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"\(name)\"\r\n\r\n")
        append(value)
        append("\r\n")
    }

    mutating func addFile(name: String, filename: String, contentType: String, data: Data) {
        append("--\(boundary)\r\n")
        append("Content-Disposition: form-data; name=\"\(name)\"; filename=\"\(filename)\"\r\n")
        append("Content-Type: \(contentType)\r\n\r\n")
        body.append(data)
        append("\r\n")
    }

    /// The complete body including the closing boundary.
    func finalized() -> Data {
        var data = body
        data.append(Data("--\(boundary)--\r\n".utf8))
        return data
    }

    private mutating func append(_ string: String) {
        body.append(Data(string.utf8))
    }
}

// MARK: - Upload progress

final class UploadProgressDelegate: NSObject, URLSessionTaskDelegate {
    private let onProgress: UploadProgressHandler

    init(onProgress: @escaping UploadProgressHandler) {
        self.onProgress = onProgress
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        didSendBodyData bytesSent: Int64,
        totalBytesSent: Int64,
        totalBytesExpectedToSend: Int64
    ) {
        guard totalBytesExpectedToSend > 0 else {
            return
        }
        let fraction = min(1, max(0, Double(totalBytesSent) / Double(totalBytesExpectedToSend)))
        onProgress(fraction)
    }
}

// MARK: - Client

final class APIClient: QueueAPI {
    static let apiKeyHeader = "X-API-Key"

    let config: APIConfig
    let session: URLSession

    init(config: APIConfig, session: URLSession = .shared) {
        self.config = config
        self.session = session
    }

    // MARK: QueueAPI

    func health() async throws -> HealthResponse {
        let request = buildRequest(path: "healthz", method: "GET")
        let (data, response) = try await perform(request)
        try Self.ensureSuccess(response, data: data)
        return try decode(HealthResponse.self, from: data)
    }

    func fetchMetadata(ifNoneMatch etag: String?) async throws -> MetadataFetchResult {
        var headers: [String: String] = [:]
        if let etag, !etag.isEmpty {
            headers["If-None-Match"] = etag
        }
        let request = buildRequest(path: "metadata", method: "GET", headers: headers)
        let (data, response) = try await perform(request)
        if response.statusCode == 304 {
            return .notModified
        }
        try Self.ensureSuccess(response, data: data)
        let metadata = try decode(Metadata.self, from: data)
        return .updated(metadata, etag: response.value(forHTTPHeaderField: "ETag"))
    }

    func refreshMetadata() async throws -> MetadataRefreshResponse {
        let request = buildRequest(path: "metadata/refresh", method: "POST")
        let (data, response) = try await perform(request)
        try Self.ensureSuccess(response, data: data)
        return try decode(MetadataRefreshResponse.self, from: data)
    }

    func previewQueue(_ rows: [QueueRowInput]) async throws -> PreviewResponse {
        let body = try encodeRows(rows)
        let request = buildRequest(path: "queue/preview", method: "POST", body: body, contentType: "application/json")
        let (data, response) = try await perform(request)
        try Self.ensureSuccess(response, data: data)
        return try decode(PreviewResponse.self, from: data)
    }

    func submitQueue(_ rows: [QueueRowInput]) async throws -> QueueWriteResponse {
        let body = try encodeRows(rows)
        let request = buildRequest(path: "queue", method: "PUT", body: body, contentType: "application/json")
        let (data, response) = try await perform(request)
        try Self.ensureSuccess(response, data: data)
        return try decode(QueueWriteResponse.self, from: data)
    }

    func saveDraft(_ rows: [QueueRowInput]) async throws -> QueueWriteResponse {
        let body = try encodeRows(rows)
        let request = buildRequest(path: "queue/draft", method: "PUT", body: body, contentType: "application/json")
        let (data, response) = try await perform(request)
        try Self.ensureSuccess(response, data: data)
        return try decode(QueueWriteResponse.self, from: data)
    }

    func fetchLastQueue() async throws -> QueueGetResponse {
        let request = buildRequest(path: "queue", method: "GET")
        let (data, response) = try await perform(request)
        try Self.ensureSuccess(response, data: data)
        return try decode(QueueGetResponse.self, from: data)
    }

    func uploadImage(jpeg: Data, side: ImageSide, clientId: String?, progress: UploadProgressHandler?) async throws -> UploadResponse {
        var multipart = MultipartBody()
        let filename = (clientId ?? "image") + ".jpg"
        multipart.addFile(name: "file", filename: filename, contentType: "image/jpeg", data: jpeg)
        multipart.addField(name: "side", value: side.rawValue)
        if let clientId, !clientId.isEmpty {
            multipart.addField(name: "client_id", value: clientId)
        }
        let body = multipart.finalized()
        let request = buildRequest(path: "images", method: "POST", body: nil, contentType: multipart.contentType)
        let delegate: UploadProgressDelegate? = progress.map { UploadProgressDelegate(onProgress: $0) }

        let result: (Data, URLResponse)
        do {
            result = try await session.upload(for: request, from: body, delegate: delegate)
        } catch {
            throw APIError.transport(Self.describeTransportError(error))
        }
        guard let http = result.1 as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        try Self.ensureSuccess(http, data: result.0)
        return try decode(UploadResponse.self, from: result.0)
    }

    // MARK: Request building (internal so the tests can inspect it)

    func buildRequest(
        path: String,
        method: String,
        body: Data? = nil,
        contentType: String? = nil,
        headers: [String: String] = [:]
    ) -> URLRequest {
        var request = URLRequest(url: Self.url(base: config.baseURL, path: path))
        request.httpMethod = method
        request.cachePolicy = .reloadIgnoringLocalCacheData
        request.timeoutInterval = 60
        request.setValue(config.apiKey, forHTTPHeaderField: Self.apiKeyHeader)
        request.setValue("application/json", forHTTPHeaderField: "Accept")
        if let contentType {
            request.setValue(contentType, forHTTPHeaderField: "Content-Type")
        }
        for (name, value) in headers {
            request.setValue(value, forHTTPHeaderField: name)
        }
        request.httpBody = body
        return request
    }

    static func url(base: URL, path: String) -> URL {
        let trimmedPath = path.hasPrefix("/") ? String(path.dropFirst()) : path
        var baseString = base.absoluteString
        while baseString.hasSuffix("/") {
            baseString.removeLast()
        }
        return URL(string: baseString + "/" + trimmedPath) ?? base.appendingPathComponent(trimmedPath)
    }

    func encodeRows(_ rows: [QueueRowInput]) throws -> Data {
        do {
            return try JSONCoding.encoder.encode(RowsRequest(rows: rows))
        } catch {
            throw APIError.decoding("could not encode the request: \(error.localizedDescription)")
        }
    }

    // MARK: Transport

    private func perform(_ request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let result: (Data, URLResponse)
        do {
            result = try await session.data(for: request)
        } catch {
            throw APIError.transport(Self.describeTransportError(error))
        }
        guard let http = result.1 as? HTTPURLResponse else {
            throw APIError.invalidResponse
        }
        return (result.0, http)
    }

    private func decode<T: Decodable>(_ type: T.Type, from data: Data) throws -> T {
        do {
            return try JSONCoding.decoder.decode(type, from: data)
        } catch let error as DecodingError {
            throw APIError.decoding(Self.describe(error))
        } catch {
            throw APIError.decoding(error.localizedDescription)
        }
    }

    static func ensureSuccess(_ response: HTTPURLResponse, data: Data) throws {
        guard (200 ..< 300).contains(response.statusCode) else {
            throw parseError(status: response.statusCode, data: data)
        }
    }

    /// Turn a non-2xx body into the most readable `APIError` we can.
    static func parseError(status: Int, data: Data) -> APIError {
        let decoder = JSONCoding.decoder
        if status == 422, let envelope = try? decoder.decode(QueueErrorEnvelope.self, from: data) {
            return .queueRejected(message: envelope.detail.message, rows: envelope.detail.rows ?? [])
        }
        var detail: String?
        if let envelope = try? decoder.decode(StringDetailEnvelope.self, from: data) {
            detail = envelope.detail
        } else if let envelope = try? decoder.decode(QueueErrorEnvelope.self, from: data) {
            detail = envelope.detail.message
        } else if let envelope = try? decoder.decode(ValidationErrorEnvelope.self, from: data) {
            detail = envelope.detail.map { item in
                let location = (item.loc ?? []).map { $0.displayString }.joined(separator: ".")
                let message = item.msg ?? item.type ?? "invalid"
                return location.isEmpty ? message : "\(location): \(message)"
            }.joined(separator: "; ")
        } else if let text = String(data: data, encoding: .utf8), !text.isEmpty {
            detail = String(text.prefix(300))
        }
        return .http(status: status, message: message(for: status, detail: detail))
    }

    static func message(for status: Int, detail: String?) -> String {
        let prefix: String
        switch status {
        case 401:
            prefix = "API key rejected (401)."
        case 403:
            prefix = "Forbidden (403)."
        case 404:
            prefix = "Not found (404) — check the base URL."
        case 413:
            prefix = "Image too large (413): the API accepts up to 10 MiB."
        case 415:
            prefix = "Unsupported image type (415): JPEG or PNG only."
        case 422:
            prefix = "The server rejected the request (422)."
        case 503:
            prefix = "Metadata unavailable (503): the service could not reach BigQuery."
        default:
            prefix = "Server error (HTTP \(status))."
        }
        if let detail, !detail.isEmpty {
            return prefix + " " + detail
        }
        return prefix
    }

    static func describeTransportError(_ error: Error) -> String {
        if let urlError = error as? URLError {
            switch urlError.code {
            case .cannotConnectToHost, .cannotFindHost, .dnsLookupFailed:
                return "Cannot reach the server — check the base URL. (\(urlError.localizedDescription))"
            case .notConnectedToInternet:
                return "No internet connection."
            case .timedOut:
                return "The request timed out."
            default:
                return urlError.localizedDescription
            }
        }
        return error.localizedDescription
    }

    static func describe(_ error: DecodingError) -> String {
        switch error {
        case .keyNotFound(let key, let context):
            return "missing key '\(key.stringValue)' at \(path(context))"
        case .typeMismatch(let type, let context):
            return "wrong type for \(path(context)) (expected \(type))"
        case .valueNotFound(let type, let context):
            return "null for \(path(context)) (expected \(type))"
        case .dataCorrupted(let context):
            return context.debugDescription
        @unknown default:
            return error.localizedDescription
        }
    }

    private static func path(_ context: DecodingError.Context) -> String {
        let joined = context.codingPath.map { $0.stringValue }.joined(separator: ".")
        return joined.isEmpty ? "root" : joined
    }
}
