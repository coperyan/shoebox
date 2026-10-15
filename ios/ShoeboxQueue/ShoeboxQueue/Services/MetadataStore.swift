import Foundation
import Observation

/// Holds the dropdown metadata, cached on disk as `metadata.json` (the payload) and
/// `metadata.meta.json` (version, ETag, fetched-at) so the app works offline.
@MainActor
@Observable
final class MetadataStore {
    struct CacheMeta: Codable, Hashable {
        var version: String
        var etag: String?
        var fetchedAt: String
    }

    let directory: URL

    private(set) var metadata: Metadata?
    private(set) var index: MetadataIndex = .empty
    private(set) var etag: String?
    private(set) var fetchedAt: String?
    private(set) var isRefreshing = false
    /// A refresh failed but a cached copy is in use.
    private(set) var isStale = false
    private(set) var lastError: String?

    init(directory: URL) {
        self.directory = directory
    }

    var version: String? {
        metadata?.version
    }

    var generatedAt: String? {
        metadata?.generatedAt
    }

    var hasMetadata: Bool {
        metadata != nil
    }

    var setCount: Int {
        index.setNames.count
    }

    private var dataURL: URL {
        directory.appendingPathComponent("metadata.json")
    }

    private var metaURL: URL {
        directory.appendingPathComponent("metadata.meta.json")
    }

    /// Install a payload directly (tests, and the cache / network paths below).
    func apply(_ metadata: Metadata, etag: String?, fetchedAt: String? = nil) {
        self.metadata = metadata
        self.index = MetadataIndex(metadata: metadata)
        self.etag = etag
        self.fetchedAt = fetchedAt ?? Timestamps.now()
    }

    /// Read the disk cache (decoding happens off the main actor).
    func loadCache() async {
        let dataURL = self.dataURL
        let metaURL = self.metaURL
        let loaded = await Task.detached(priority: .userInitiated) { () -> (Metadata, CacheMeta?)? in
            guard let data = try? Data(contentsOf: dataURL),
                  let metadata = try? JSONCoding.decoder.decode(Metadata.self, from: data) else {
                return nil
            }
            var meta: CacheMeta?
            if let metaData = try? Data(contentsOf: metaURL) {
                meta = try? JSONCoding.decoder.decode(CacheMeta.self, from: metaData)
            }
            return (metadata, meta)
        }.value
        guard let loaded else {
            return
        }
        // Only replace what a network refresh has not already installed.
        if metadata == nil {
            apply(loaded.0, etag: loaded.1?.etag, fetchedAt: loaded.1?.fetchedAt)
        }
    }

    /// `GET /metadata` with `If-None-Match` (unless `force`). Failures keep the cached copy and
    /// raise the stale banner.
    func refresh(using api: any QueueAPI, force: Bool) async {
        guard !isRefreshing else {
            return
        }
        isRefreshing = true
        defer { isRefreshing = false }

        do {
            let result = try await api.fetchMetadata(ifNoneMatch: force ? nil : etag)
            switch result {
            case .notModified:
                lastError = nil
                isStale = false
                if metadata != nil {
                    fetchedAt = Timestamps.now()
                    await persistMeta()
                }
            case .updated(let fresh, let newEtag):
                apply(fresh, etag: newEtag, fetchedAt: Timestamps.now())
                lastError = nil
                isStale = false
                await persist(fresh)
                await persistMeta()
            }
        } catch {
            lastError = error.localizedDescription
            isStale = metadata != nil
        }
    }

    func clearCache() {
        try? FileManager.default.removeItem(at: dataURL)
        try? FileManager.default.removeItem(at: metaURL)
    }

    private func persist(_ metadata: Metadata) async {
        let url = dataURL
        await Task.detached(priority: .utility) {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let data = try? JSONCoding.encoder.encode(metadata) {
                try? data.write(to: url, options: .atomic)
            }
        }.value
    }

    private func persistMeta() async {
        guard let version = metadata?.version else {
            return
        }
        let meta = CacheMeta(version: version, etag: etag, fetchedAt: fetchedAt ?? Timestamps.now())
        let url = metaURL
        await Task.detached(priority: .utility) {
            try? FileManager.default.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
            if let data = try? JSONCoding.fileEncoder.encode(meta) {
                try? data.write(to: url, options: .atomic)
            }
        }.value
    }
}
