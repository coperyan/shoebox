import Foundation
import XCTest
@testable import ShoeboxQueue

// MARK: - Fixtures

/// Anchor for `Bundle(for:)`.
final class FixtureAnchor {}

enum FixtureError: Error {
    case missing(String)
    case badResponse
}

enum Fixtures {
    static func data(_ name: String, ext: String = "json") throws -> Data {
        let bundle = Bundle(for: FixtureAnchor.self)
        guard let url = bundle.url(forResource: name, withExtension: ext) else {
            throw FixtureError.missing("\(name).\(ext)")
        }
        return try Data(contentsOf: url)
    }

    static func metadata() throws -> Metadata {
        try JSONCoding.decoder.decode(Metadata.self, from: data("metadata_sample"))
    }

    static func preview() throws -> PreviewResponse {
        try JSONCoding.decoder.decode(PreviewResponse.self, from: data("preview_sample"))
    }

    static let setName = "2023 Topps Chrome"
    static let subsetName = "Base"
}

enum TempDirectory {
    static func make() throws -> URL {
        let url = FileManager.default.temporaryDirectory
            .appendingPathComponent("ShoeboxQueueTests-" + UUID().uuidString, isDirectory: true)
        try FileManager.default.createDirectory(at: url, withIntermediateDirectories: true)
        return url
    }
}

// MARK: - URL stubbing

final class StubURLProtocol: URLProtocol {
    typealias Handler = @Sendable (URLRequest) throws -> (HTTPURLResponse, Data)

    static var handler: Handler?
    static var recorded: [URLRequest] = []

    static func reset() {
        handler = nil
        recorded = []
    }

    static func makeSession() -> URLSession {
        let configuration = URLSessionConfiguration.ephemeral
        configuration.protocolClasses = [StubURLProtocol.self]
        return URLSession(configuration: configuration)
    }

    static func response(for request: URLRequest, status: Int, headers: [String: String] = [:]) throws -> HTTPURLResponse {
        guard let url = request.url else {
            throw FixtureError.badResponse
        }
        var allHeaders = ["Content-Type": "application/json"]
        for (key, value) in headers {
            allHeaders[key] = value
        }
        guard let response = HTTPURLResponse(url: url, statusCode: status, httpVersion: "HTTP/1.1", headerFields: allHeaders) else {
            throw FixtureError.badResponse
        }
        return response
    }

    /// A handler return value in one call.
    static func reply(_ request: URLRequest, status: Int, headers: [String: String] = [:], body: Data = Data()) throws -> (HTTPURLResponse, Data) {
        let response = try self.response(for: request, status: status, headers: headers)
        return (response, body)
    }

    /// Best effort: URLProtocol sees request bodies as a stream.
    static func body(of request: URLRequest) -> Data? {
        if let body = request.httpBody {
            return body
        }
        guard let stream = request.httpBodyStream else {
            return nil
        }
        stream.open()
        defer { stream.close() }
        var data = Data()
        let bufferSize = 64 * 1024
        var buffer = [UInt8](repeating: 0, count: bufferSize)
        while stream.hasBytesAvailable {
            let read = stream.read(&buffer, maxLength: bufferSize)
            if read <= 0 {
                break
            }
            data.append(buffer, count: read)
        }
        return data
    }

    override class func canInit(with request: URLRequest) -> Bool {
        true
    }

    override class func canonicalRequest(for request: URLRequest) -> URLRequest {
        request
    }

    override func startLoading() {
        Self.recorded.append(request)
        guard let handler = Self.handler else {
            client?.urlProtocol(self, didFailWithError: URLError(.badServerResponse))
            return
        }
        do {
            let (response, data) = try handler(request)
            client?.urlProtocol(self, didReceive: response, cacheStoragePolicy: .notAllowed)
            client?.urlProtocol(self, didLoad: data)
            client?.urlProtocolDidFinishLoading(self)
        } catch {
            client?.urlProtocol(self, didFailWithError: error)
        }
    }

    override func stopLoading() {}
}

// MARK: - Fake API for the view-model tests

final class FakeAPI: QueueAPI {
    struct FakeError: LocalizedError {
        let message: String

        var errorDescription: String? {
            message
        }
    }

    var healthResponse = HealthResponse(status: "ok", version: "test")
    var metadataResult: MetadataFetchResult = .notModified
    var previewResponse: PreviewResponse?
    var writeResponse = QueueWriteResponse(
        kind: "queue",
        submittedAt: "2026-10-15T12:00:00+00:00",
        counts: Counts(rows: 0, ok: 0, errors: 0),
        queueUri: "gs://bucket/queue/listing_queue_enriched.jsonl",
        draftUri: "gs://bucket/queue/listing_queue_draft.json",
        metadataVersion: "abc123def4567890"
    )
    var submitError: Error?
    var lastQueue = QueueGetResponse(
        kind: nil,
        submittedAt: nil,
        rows: [],
        counts: Counts(rows: 0, ok: 0, errors: 0),
        queueUri: "gs://bucket/queue/listing_queue_enriched.jsonl",
        draftUri: "gs://bucket/queue/listing_queue_draft.json"
    )
    var uploadError: Error?

    private(set) var fetchedEtags: [String?] = []
    private(set) var previewedRows: [[QueueRowInput]] = []
    private(set) var submittedRows: [[QueueRowInput]] = []
    private(set) var draftRows: [[QueueRowInput]] = []
    private(set) var uploadedClientIds: [String?] = []
    private(set) var uploadedSides: [ImageSide] = []

    func health() async throws -> HealthResponse {
        healthResponse
    }

    func fetchMetadata(ifNoneMatch etag: String?) async throws -> MetadataFetchResult {
        fetchedEtags.append(etag)
        return metadataResult
    }

    func refreshMetadata() async throws -> MetadataRefreshResponse {
        MetadataRefreshResponse(version: "abc123def4567890", generatedAt: "2026-10-15T12:00:00+00:00")
    }

    func previewQueue(_ rows: [QueueRowInput]) async throws -> PreviewResponse {
        previewedRows.append(rows)
        if let previewResponse {
            return previewResponse
        }
        return PreviewResponse(
            rows: rows.enumerated().map { RowResult(index: $0.offset, ok: true, enriched: [:], error: nil, warnings: []) },
            counts: Counts(rows: rows.count, ok: rows.count, errors: 0),
            metadataVersion: "abc123def4567890"
        )
    }

    func submitQueue(_ rows: [QueueRowInput]) async throws -> QueueWriteResponse {
        submittedRows.append(rows)
        if let submitError {
            throw submitError
        }
        var response = writeResponse
        response.counts = Counts(rows: rows.count, ok: rows.count, errors: 0)
        return response
    }

    func saveDraft(_ rows: [QueueRowInput]) async throws -> QueueWriteResponse {
        draftRows.append(rows)
        var response = writeResponse
        response.kind = "draft"
        response.queueUri = nil
        response.counts = Counts(rows: rows.count, ok: 0, errors: 0)
        return response
    }

    func fetchLastQueue() async throws -> QueueGetResponse {
        lastQueue
    }

    func uploadImage(jpeg: Data, side: ImageSide, clientId: String?, progress: UploadProgressHandler?) async throws -> UploadResponse {
        uploadedClientIds.append(clientId)
        uploadedSides.append(side)
        if let uploadError {
            throw uploadError
        }
        progress?(1)
        let name = clientId ?? UUID().uuidString
        return UploadResponse(
            uri: "gs://bucket/uploads/2026-10/\(name).jpg",
            side: side.rawValue,
            clientId: clientId,
            bytes: jpeg.count,
            contentType: "image/jpeg"
        )
    }
}
