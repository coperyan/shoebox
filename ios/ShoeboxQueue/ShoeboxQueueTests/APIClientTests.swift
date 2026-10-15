import Foundation
import XCTest
@testable import ShoeboxQueue

final class APIClientTests: XCTestCase {
    private let baseURLString = "http://localhost:8080"

    private func makeClient(apiKey: String = "k") throws -> APIClient {
        StubURLProtocol.reset()
        addTeardownBlock {
            StubURLProtocol.reset()
        }
        let baseURL = try XCTUnwrap(URL(string: baseURLString))
        return APIClient(config: APIConfig(baseURL: baseURL, apiKey: apiKey), session: StubURLProtocol.makeSession())
    }

    private func sampleInput(card: String = "1") -> QueueRowInput {
        QueueRowInput(
            setName: "2023 Topps Chrome",
            subsetName: "Base",
            parallelVariety: "Gold Refractor",
            cardNumber: card,
            quantity: 2,
            price: 12.5,
            imageFront: "123",
            imageBack: ""
        )
    }

    private func jsonObject(_ data: Data) throws -> [String: Any] {
        let object = try JSONSerialization.jsonObject(with: data)
        return try XCTUnwrap(object as? [String: Any])
    }

    // MARK: Request building

    func testBuildRequestURLHeadersAndBody() throws {
        let client = try makeClient()
        let body = try client.encodeRows([sampleInput()])
        let request = client.buildRequest(path: "queue/preview", method: "POST", body: body, contentType: "application/json")
        XCTAssertEqual(request.url?.absoluteString, "http://localhost:8080/queue/preview")
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.value(forHTTPHeaderField: "X-API-Key"), "k")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        XCTAssertEqual(request.cachePolicy, .reloadIgnoringLocalCacheData)

        let object = try jsonObject(try XCTUnwrap(request.httpBody))
        XCTAssertEqual(Set(object.keys), ["rows"])
        let rows = try XCTUnwrap(object["rows"] as? [[String: Any]])
        XCTAssertEqual(rows.count, 1)
        XCTAssertEqual(
            Set(rows[0].keys),
            ["set_name", "subset_name", "parallel_variety", "card_number", "quantity", "price", "image_front", "image_back"]
        )
    }

    func testURLJoiningToleratesSlashes() throws {
        let base = try XCTUnwrap(URL(string: "https://example.run.app/"))
        XCTAssertEqual(APIClient.url(base: base, path: "/healthz").absoluteString, "https://example.run.app/healthz")
        XCTAssertEqual(APIClient.url(base: base, path: "queue/draft").absoluteString, "https://example.run.app/queue/draft")
    }

    // MARK: Metadata

    func testMetadataDecodeWithNullsAndETag() async throws {
        let client = try makeClient()
        let fixture = try Fixtures.data("metadata_sample")
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 200, headers: ["ETag": "W/\"abc123def4567890\""], body: fixture)
        }

        let result = try await client.fetchMetadata(ifNoneMatch: nil)
        guard case .updated(let metadata, let etag) = result else {
            return XCTFail("expected .updated")
        }
        XCTAssertEqual(etag, "W/\"abc123def4567890\"")
        XCTAssertEqual(metadata.version, "abc123def4567890")
        XCTAssertEqual(metadata.ttlS, 3600)
        XCTAssertEqual(metadata.counts.sets, 2)
        XCTAssertEqual(metadata.sets.count, 2)
        let base = metadata.sets[0].subsets[0]
        XCTAssertEqual(base.cards.map { $0.cardNumber }, ["1", "2", "10", "1A", "A", "NNO"])
        XCTAssertNil(base.cards[5].derivedCardNumber)
        XCTAssertNil(base.cards[4].team)
        XCTAssertEqual(base.parallels[0].printRun, 50)
        XCTAssertNil(base.parallels[1].printRun)
        XCTAssertNil(base.parallels[1].otherNote)

        let request = try XCTUnwrap(StubURLProtocol.recorded.first)
        XCTAssertEqual(request.url?.path, "/metadata")
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertNil(request.value(forHTTPHeaderField: "If-None-Match"), "no etag -> no conditional header")
    }

    func testIfNoneMatchSentAndNotModifiedHandled() async throws {
        let client = try makeClient()
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 304, body: Data())
        }
        let result = try await client.fetchMetadata(ifNoneMatch: "W/\"abc\"")
        guard case .notModified = result else {
            return XCTFail("expected .notModified")
        }
        let request = try XCTUnwrap(StubURLProtocol.recorded.first)
        XCTAssertEqual(request.value(forHTTPHeaderField: "If-None-Match"), "W/\"abc\"")
    }

    // MARK: Headers on every route

    func testAPIKeyHeaderOnEveryRequest() async throws {
        let client = try makeClient(apiKey: "secret-key")
        let metadataFixture = try Fixtures.data("metadata_sample")
        let previewFixture = try Fixtures.data("preview_sample")
        let writeBody = Data(
            #"{"kind":"draft","submitted_at":"2026-10-15T12:00:00+00:00","counts":{"rows":1,"ok":0,"errors":0},"queue_uri":null,"draft_uri":"gs://b/queue/listing_queue_draft.json","metadata_version":null}"#.utf8
        )
        let getBody = Data(
            #"{"kind":null,"submitted_at":null,"rows":[],"counts":{"rows":0,"ok":0,"errors":0},"queue_uri":"gs://b/q.jsonl","draft_uri":"gs://b/d.json"}"#.utf8
        )
        let healthBody = Data(#"{"status":"ok","version":"1.2.3"}"#.utf8)
        let uploadBody = Data(
            #"{"uri":"gs://b/uploads/2026-10/abcdefgh.jpg","side":"front","client_id":"abcdefgh","bytes":3,"content_type":"image/jpeg"}"#.utf8
        )
        let runFixture = try Fixtures.data("listing_run_sample")
        let runsFixture = try Fixtures.data("listing_runs_sample")
        StubURLProtocol.handler = { request in
            let path = request.url?.path ?? ""
            switch (request.httpMethod ?? "", path) {
            case ("POST", "/queue/listings"):
                return try StubURLProtocol.reply(request, status: 202, body: runFixture)
            case ("GET", "/queue/listings"):
                return try StubURLProtocol.reply(request, status: 200, body: runsFixture)
            case ("GET", "/queue/listings/20261015T120000Z-a1b2c3d4"):
                return try StubURLProtocol.reply(request, status: 200, body: runFixture)
            case ("GET", "/healthz"):
                return try StubURLProtocol.reply(request, status: 200, body: healthBody)
            case ("GET", "/metadata"):
                return try StubURLProtocol.reply(request, status: 200, body: metadataFixture)
            case ("POST", "/queue/preview"):
                return try StubURLProtocol.reply(request, status: 200, body: previewFixture)
            case ("PUT", "/queue"), ("PUT", "/queue/draft"):
                return try StubURLProtocol.reply(request, status: 200, body: writeBody)
            case ("GET", "/queue"):
                return try StubURLProtocol.reply(request, status: 200, body: getBody)
            case ("POST", "/images"):
                return try StubURLProtocol.reply(request, status: 201, body: uploadBody)
            default:
                return try StubURLProtocol.reply(request, status: 404, body: Data(#"{"detail":"Not Found"}"#.utf8))
            }
        }

        _ = try await client.health()
        _ = try await client.fetchMetadata(ifNoneMatch: nil)
        _ = try await client.previewQueue([sampleInput()])
        _ = try await client.submitQueue([sampleInput()])
        _ = try await client.saveDraft([sampleInput()])
        _ = try await client.fetchLastQueue()
        _ = try await client.uploadImage(jpeg: Data([0xFF, 0xD8, 0xFF]), side: .front, clientId: "abcdefgh", progress: nil)
        _ = try await client.startListingRun(publish: false, schedule: true)
        _ = try await client.fetchListingRuns()
        _ = try await client.fetchListingRun(id: "20261015T120000Z-a1b2c3d4")

        XCTAssertEqual(StubURLProtocol.recorded.count, 10)
        for request in StubURLProtocol.recorded {
            XCTAssertEqual(request.value(forHTTPHeaderField: "X-API-Key"), "secret-key", "\(request.url?.path ?? "?")")
        }
        XCTAssertEqual(
            StubURLProtocol.recorded.map { $0.httpMethod ?? "" },
            ["GET", "GET", "POST", "PUT", "PUT", "GET", "POST", "POST", "GET", "GET"]
        )
        XCTAssertEqual(
            StubURLProtocol.recorded.map { $0.url?.path ?? "" },
            [
                "/healthz", "/metadata", "/queue/preview", "/queue", "/queue/draft", "/queue", "/images",
                "/queue/listings", "/queue/listings", "/queue/listings/20261015T120000Z-a1b2c3d4",
            ]
        )
    }

    // MARK: Response decoding

    func testPreviewSubmitAndLastQueueDecode() async throws {
        let client = try makeClient()
        let previewFixture = try Fixtures.data("preview_sample")
        let writeBody = Data(
            #"{"kind":"queue","submitted_at":"2026-10-15T12:00:00+00:00","counts":{"rows":2,"ok":2,"errors":0},"queue_uri":"gs://b/queue/listing_queue_enriched.jsonl","draft_uri":"gs://b/queue/listing_queue_draft.json","metadata_version":"abc123def4567890"}"#.utf8
        )
        let getBody = Data(
            #"{"kind":"draft","submitted_at":"2026-10-15T13:00:00+00:00","rows":[{"set_name":"S","subset_name":"B","parallel_variety":null,"card_number":"","quantity":1,"price":0.0,"image_front":"","image_back":"gs://b/uploads/2026-10/x.jpg"}],"counts":{"rows":1,"ok":0,"errors":0},"queue_uri":"gs://b/q.jsonl","draft_uri":"gs://b/d.json"}"#.utf8
        )
        StubURLProtocol.handler = { request in
            switch (request.httpMethod ?? "", request.url?.path ?? "") {
            case ("POST", "/queue/preview"):
                return try StubURLProtocol.reply(request, status: 200, body: previewFixture)
            case ("PUT", "/queue"):
                return try StubURLProtocol.reply(request, status: 200, body: writeBody)
            case ("GET", "/queue"):
                return try StubURLProtocol.reply(request, status: 200, body: getBody)
            default:
                return try StubURLProtocol.reply(request, status: 404, body: Data())
            }
        }

        let preview = try await client.previewQueue([sampleInput(), sampleInput(card: "zzz")])
        XCTAssertEqual(preview.counts, Counts(rows: 2, ok: 1, errors: 1))
        XCTAssertEqual(preview.metadataVersion, "abc123def4567890")
        XCTAssertEqual(preview.rows.count, 2)
        let okRow = preview.rows[0]
        XCTAssertTrue(okRow.ok)
        XCTAssertNil(okRow.error)
        XCTAssertEqual(okRow.warningMessages, [])
        XCTAssertEqual(okRow.enriched?.count, 21)
        for key in EnrichedFieldOrder.keys {
            XCTAssertNotNil(okRow.enrichedValue(key), "missing enriched key \(key)")
        }
        XCTAssertEqual(okRow.enrichedValue("set_name"), .string("2023 Topps Chrome"))
        XCTAssertEqual(okRow.enrichedValue("print_run"), .int(50))
        XCTAssertEqual(okRow.enrichedValue("price"), .double(12.5))
        XCTAssertEqual(okRow.enrichedValue("note"), .null)
        XCTAssertEqual(okRow.enrichedValue("image_front")?.displayString, "123")
        let badRow = preview.rows[1]
        XCTAssertFalse(badRow.ok)
        XCTAssertNil(badRow.enriched)
        XCTAssertEqual(badRow.error, "No checklist match")
        XCTAssertEqual(badRow.warningMessages, ["image_front missing", "image_back missing"])

        let written = try await client.submitQueue([sampleInput()])
        XCTAssertEqual(written.kind, "queue")
        XCTAssertEqual(written.queueUri, "gs://b/queue/listing_queue_enriched.jsonl")
        XCTAssertEqual(written.submittedAt, "2026-10-15T12:00:00+00:00")
        XCTAssertEqual(written.metadataVersion, "abc123def4567890")

        let last = try await client.fetchLastQueue()
        XCTAssertEqual(last.kind, "draft")
        XCTAssertEqual(last.rows.count, 1)
        XCTAssertNil(last.rows[0].parallelVariety)
        XCTAssertEqual(last.rows[0].cardNumber, "")
        XCTAssertEqual(last.rows[0].imageBack, "gs://b/uploads/2026-10/x.jpg")
    }

    // MARK: Listing runs

    func testStartListingRunSendsFlagsAndDecodesRun() async throws {
        let client = try makeClient()
        let fixture = try Fixtures.data("listing_run_sample")
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 202, body: fixture)
        }

        let run = try await client.startListingRun(publish: false, schedule: true)

        let request = try XCTUnwrap(StubURLProtocol.recorded.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/queue/listings")
        XCTAssertEqual(request.value(forHTTPHeaderField: "Content-Type"), "application/json")
        let encoded = try jsonObject(try JSONCoding.encoder.encode(StartListingRunRequest(publish: false, schedule: true)))
        XCTAssertEqual(Set(encoded.keys), ["publish", "schedule"], "no scrape_prices flag: the job never scrapes")
        XCTAssertEqual(encoded["publish"] as? Bool, false)
        XCTAssertEqual(encoded["schedule"] as? Bool, true)
        // The stub sees request bodies as a stream; check the wire body when it is available.
        if let data = StubURLProtocol.body(of: request), !data.isEmpty {
            let body = try jsonObject(data)
            XCTAssertEqual(body["publish"] as? Bool, false)
            XCTAssertEqual(body["schedule"] as? Bool, true)
        }

        XCTAssertEqual(run.runId, "20261015T120000Z-a1b2c3d4")
        XCTAssertEqual(run.id, run.runId)
        XCTAssertEqual(run.state, "done")
        XCTAssertTrue(run.isFinished)
        XCTAssertFalse(run.isFailed)
        XCTAssertEqual(run.requestedAt, "2026-10-15T12:00:00+00:00")
        XCTAssertEqual(run.startedAt, "2026-10-15T12:00:07+00:00")
        XCTAssertEqual(run.finishedAt, "2026-10-15T12:03:41+00:00")
        XCTAssertFalse(run.publish)
        XCTAssertTrue(run.schedule)
        XCTAssertEqual(run.queueUri, "gs://bucket/queue/listing_queue_enriched.jsonl")
        XCTAssertEqual(run.metadataVersion, "abc123def4567890")
        XCTAssertEqual(run.rowsTotal, 2)
        XCTAssertEqual(run.rowsDone, 2)
        XCTAssertEqual(run.listed, 1)
        XCTAssertEqual(run.failed, 1)
        XCTAssertEqual(run.progress, 1, accuracy: 0.0001)
        XCTAssertNil(run.error)
        XCTAssertEqual(run.host, "create-listings-x7k2q-task0")
        XCTAssertEqual(run.results.count, 2)
        XCTAssertEqual(run.results[0].sku, "2023TC-B-GR-1")
        XCTAssertTrue(run.results[0].success)
        XCTAssertEqual(run.results[0].listingId, "1234567890")
        XCTAssertEqual(run.results[0].offerId, "98765432")
        XCTAssertNil(run.results[0].error)
        XCTAssertFalse(run.results[1].success)
        XCTAssertNil(run.results[1].listingId)
        XCTAssertEqual(run.results[1].error, "OneDrive: Pictures/Card Scans/SCAN_0234.jpg not found")
    }

    func testFetchListingRunsDefaultsMissingResults() async throws {
        let client = try makeClient()
        let fixture = try Fixtures.data("listing_runs_sample")
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 200, body: fixture)
        }

        let runs = try await client.fetchListingRuns()

        let request = try XCTUnwrap(StubURLProtocol.recorded.first)
        XCTAssertEqual(request.httpMethod, "GET")
        XCTAssertEqual(request.url?.path, "/queue/listings")
        XCTAssertEqual(runs.count, 2)
        XCTAssertEqual(runs.map { $0.runId }, ["20261015T140000Z-ffee1122", "20261015T120000Z-a1b2c3d4"])
        let running = runs[0]
        XCTAssertEqual(running.state, "running")
        XCTAssertFalse(running.isFinished)
        XCTAssertEqual(running.results, [], "no results key -> empty list")
        XCTAssertNil(running.finishedAt)
        XCTAssertEqual(running.progress, 0.25, accuracy: 0.0001)
        XCTAssertEqual(runs[1].results, [])
        XCTAssertTrue(runs[1].isFinished)
    }

    func testFetchListingRunByIdAndNotFound() async throws {
        let client = try makeClient()
        let fixture = try Fixtures.data("listing_run_sample")
        StubURLProtocol.handler = { request in
            if request.url?.path == "/queue/listings/20261015T120000Z-a1b2c3d4" {
                return try StubURLProtocol.reply(request, status: 200, body: fixture)
            }
            return try StubURLProtocol.reply(request, status: 404, body: Data(#"{"detail":"unknown run"}"#.utf8))
        }

        let run = try await client.fetchListingRun(id: "20261015T120000Z-a1b2c3d4")
        XCTAssertEqual(run.runId, "20261015T120000Z-a1b2c3d4")
        XCTAssertEqual(StubURLProtocol.recorded.first?.httpMethod, "GET")

        do {
            _ = try await client.fetchListingRun(id: "nope")
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .http(let status, let message) = error else {
                return XCTFail("expected .http, got \(error)")
            }
            XCTAssertEqual(status, 404)
            XCTAssertTrue(message.contains("nope"))
            XCTAssertFalse(message.contains("base URL"), "an unknown run id is not a wrong base URL")
        }
    }

    func testStartListingRunConflictSurfacesServerMessage() async throws {
        let client = try makeClient()
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(
                request,
                status: 409,
                body: Data(#"{"detail":{"message":"a run is in progress","run_id":"20261015T140000Z-ffee1122"}}"#.utf8)
            )
        }
        do {
            _ = try await client.startListingRun(publish: true, schedule: true)
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .http(let status, let message) = error else {
                return XCTFail("expected .http, got \(error)")
            }
            XCTAssertEqual(status, 409)
            XCTAssertTrue(message.contains("a run is in progress"), message)
        }
    }

    func testStartListingRunRejectedWithoutSubmittedQueue() async throws {
        let client = try makeClient()
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 409, body: Data(#"{"detail":{"message":"submit the queue first"}}"#.utf8))
        }
        do {
            _ = try await client.startListingRun(publish: true, schedule: true)
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error.status, 409)
            XCTAssertTrue((error.errorDescription ?? "").contains("submit the queue first"))
        }
    }

    func testStartListingRunJobFailureIs502() async throws {
        let client = try makeClient()
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 502, body: Data(#"{"detail":"could not start create-listings: PERMISSION_DENIED"}"#.utf8))
        }
        do {
            _ = try await client.startListingRun(publish: false, schedule: false)
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .http(let status, let message) = error else {
                return XCTFail("expected .http, got \(error)")
            }
            XCTAssertEqual(status, 502)
            XCTAssertTrue(message.contains("PERMISSION_DENIED"))
        }
    }

    // MARK: Multipart

    func testMultipartBodyLayout() throws {
        var body = MultipartBody(boundary: "XYZ")
        body.addFile(name: "file", filename: "abcdefgh.jpg", contentType: "image/jpeg", data: Data("JPEGDATA".utf8))
        body.addField(name: "side", value: "front")
        body.addField(name: "client_id", value: "abcdefgh")
        let text = try XCTUnwrap(String(data: body.finalized(), encoding: .utf8))

        XCTAssertEqual(body.contentType, "multipart/form-data; boundary=XYZ")
        XCTAssertTrue(text.hasPrefix("--XYZ\r\nContent-Disposition: form-data; name=\"file\"; filename=\"abcdefgh.jpg\"\r\nContent-Type: image/jpeg\r\n\r\nJPEGDATA\r\n"))
        XCTAssertTrue(text.contains("--XYZ\r\nContent-Disposition: form-data; name=\"side\"\r\n\r\nfront\r\n"))
        XCTAssertTrue(text.contains("--XYZ\r\nContent-Disposition: form-data; name=\"client_id\"\r\n\r\nabcdefgh\r\n"))
        XCTAssertTrue(text.hasSuffix("--XYZ--\r\n"))
    }

    func testUploadImageSendsMultipartAndDecodes() async throws {
        let client = try makeClient()
        let uploadBody = Data(
            #"{"uri":"gs://b/uploads/2026-10/abcdefgh.jpg","side":"back","client_id":"abcdefgh","bytes":3,"content_type":"image/jpeg"}"#.utf8
        )
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 201, body: uploadBody)
        }
        let response = try await client.uploadImage(jpeg: Data([0xFF, 0xD8, 0xFF]), side: .back, clientId: "abcdefgh", progress: nil)
        XCTAssertEqual(response.uri, "gs://b/uploads/2026-10/abcdefgh.jpg")
        XCTAssertEqual(response.clientId, "abcdefgh")
        XCTAssertEqual(response.contentType, "image/jpeg")

        let request = try XCTUnwrap(StubURLProtocol.recorded.first)
        XCTAssertEqual(request.httpMethod, "POST")
        XCTAssertEqual(request.url?.path, "/images")
        let contentType = try XCTUnwrap(request.value(forHTTPHeaderField: "Content-Type"))
        XCTAssertTrue(contentType.hasPrefix("multipart/form-data; boundary="))
        // The stub sees upload bodies as a stream; check the layout when it is available.
        if let body = StubURLProtocol.body(of: request), let text = String(data: body, encoding: .isoLatin1) {
            XCTAssertTrue(text.contains("name=\"file\"; filename=\"abcdefgh.jpg\""))
            XCTAssertTrue(text.contains("Content-Type: image/jpeg"))
            XCTAssertTrue(text.contains("name=\"side\"\r\n\r\nback"))
            XCTAssertTrue(text.contains("name=\"client_id\"\r\n\r\nabcdefgh"))
        }
    }

    // MARK: Errors

    func testUnauthorizedBecomesReadableHTTPError() async throws {
        let client = try makeClient(apiKey: "wrong")
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 401, body: Data(#"{"detail":"Invalid API key"}"#.utf8))
        }
        do {
            _ = try await client.fetchLastQueue()
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .http(let status, let message) = error else {
                return XCTFail("expected .http, got \(error)")
            }
            XCTAssertEqual(status, 401)
            XCTAssertTrue(message.contains("API key rejected"))
            XCTAssertTrue(message.contains("Invalid API key"))
        }
    }

    func testNotFoundPointsAtBaseURL() async throws {
        let client = try makeClient()
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 404, body: Data(#"{"detail":"Not Found"}"#.utf8))
        }
        do {
            _ = try await client.health()
            XCTFail("expected an error")
        } catch let error as APIError {
            XCTAssertEqual(error.status, 404)
            XCTAssertTrue((error.errorDescription ?? "").contains("check the base URL"))
        }
    }

    func testQueueRejected422SurfacesRowErrors() async throws {
        let client = try makeClient()
        let body = Data(
            #"{"detail":{"message":"1 rows failed enrichment","rows":[{"index":0,"ok":true,"enriched":{"card_id":"x"},"error":null,"warnings":[]},{"index":1,"ok":false,"enriched":null,"error":"No checklist match","warnings":["image_front missing"]}]}}"#.utf8
        )
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 422, body: body)
        }
        do {
            _ = try await client.submitQueue([sampleInput(), sampleInput(card: "zzz")])
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .queueRejected(let message, let rows) = error else {
                return XCTFail("expected .queueRejected, got \(error)")
            }
            XCTAssertEqual(message, "1 rows failed enrichment")
            XCTAssertEqual(rows.count, 2)
            XCTAssertEqual(rows[1].error, "No checklist match")
            let description = error.errorDescription ?? ""
            XCTAssertTrue(description.contains("1 rows failed enrichment"))
            XCTAssertTrue(description.contains("Row 2: No checklist match"))
            XCTAssertFalse(description.contains("Row 1:"), "ok rows are not listed")
        }
    }

    func testValidation422FallsBackToHTTPError() async throws {
        let client = try makeClient()
        let body = Data(
            #"{"detail":[{"loc":["body","rows",0,"quantity"],"msg":"Input should be less than or equal to 999","type":"less_than_equal"}]}"#.utf8
        )
        StubURLProtocol.handler = { request in
            try StubURLProtocol.reply(request, status: 422, body: body)
        }
        do {
            _ = try await client.previewQueue([sampleInput()])
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .http(let status, let message) = error else {
                return XCTFail("expected .http, got \(error)")
            }
            XCTAssertEqual(status, 422)
            XCTAssertTrue(message.contains("body.rows.0.quantity"))
            XCTAssertTrue(message.contains("less than or equal to 999"))
        }
    }

    func testTransportFailureBecomesTransportError() async throws {
        let client = try makeClient()
        StubURLProtocol.handler = { _ in
            throw URLError(.cannotConnectToHost)
        }
        do {
            _ = try await client.health()
            XCTFail("expected an error")
        } catch let error as APIError {
            guard case .transport(let message) = error else {
                return XCTFail("expected .transport, got \(error)")
            }
            XCTAssertTrue(message.contains("check the base URL"))
        }
    }
}
