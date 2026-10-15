import Foundation

/// Plain JSON files under Application Support/ShoeboxQueue:
///
/// - `current.json`         the working queue (`WorkingState`), written atomically on autosave
/// - `drafts/<uuid>.json`   named drafts (`NamedDraft`)
/// - `images/<uuid>.jpg`    downscaled photos waiting for / already uploaded
///
/// `MetadataStore` keeps its cache files in the same directory.
final class DraftStore {
    enum StoreError: LocalizedError {
        case draftNotFound(UUID)

        var errorDescription: String? {
            switch self {
            case .draftNotFound(let id):
                return "Draft \(id.uuidString) no longer exists."
            }
        }
    }

    static let directoryName = "ShoeboxQueue"

    static var defaultDirectory: URL {
        let base = FileManager.default.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? FileManager.default.temporaryDirectory
        return base.appendingPathComponent(directoryName, isDirectory: true)
    }

    let baseURL: URL
    private let fileManager = FileManager.default

    init(baseURL: URL) {
        self.baseURL = baseURL
        ensureDirectories()
    }

    var currentURL: URL {
        baseURL.appendingPathComponent("current.json")
    }

    var draftsURL: URL {
        baseURL.appendingPathComponent("drafts", isDirectory: true)
    }

    var imagesURL: URL {
        baseURL.appendingPathComponent("images", isDirectory: true)
    }

    private func ensureDirectories() {
        for url in [baseURL, draftsURL, imagesURL] {
            try? fileManager.createDirectory(at: url, withIntermediateDirectories: true)
        }
    }

    // MARK: Working queue

    func saveCurrent(_ state: WorkingState) throws {
        ensureDirectories()
        let data = try JSONCoding.fileEncoder.encode(state)
        try data.write(to: currentURL, options: .atomic)
    }

    func loadCurrent() -> WorkingState? {
        guard let data = try? Data(contentsOf: currentURL) else {
            return nil
        }
        return try? JSONCoding.decoder.decode(WorkingState.self, from: data)
    }

    func clearCurrent() {
        try? fileManager.removeItem(at: currentURL)
    }

    // MARK: Named drafts

    func draftURL(id: UUID) -> URL {
        draftsURL.appendingPathComponent(id.uuidString + ".json")
    }

    /// Newest first (by `updatedAt`, then name).
    func listDrafts() -> [NamedDraftSummary] {
        ensureDirectories()
        let urls = (try? fileManager.contentsOfDirectory(at: draftsURL, includingPropertiesForKeys: nil)) ?? []
        var summaries: [NamedDraftSummary] = []
        for url in urls where url.pathExtension.lowercased() == "json" {
            guard let data = try? Data(contentsOf: url),
                  let draft = try? JSONCoding.decoder.decode(NamedDraft.self, from: data) else {
                continue
            }
            summaries.append(draft.summary)
        }
        return summaries.sorted { lhs, rhs in
            if lhs.updatedAt != rhs.updatedAt {
                return lhs.updatedAt > rhs.updatedAt
            }
            return lhs.name < rhs.name
        }
    }

    func saveDraft(_ draft: NamedDraft) throws {
        ensureDirectories()
        let data = try JSONCoding.fileEncoder.encode(draft)
        try data.write(to: draftURL(id: draft.id), options: .atomic)
    }

    func loadDraft(id: UUID) -> NamedDraft? {
        guard let data = try? Data(contentsOf: draftURL(id: id)) else {
            return nil
        }
        return try? JSONCoding.decoder.decode(NamedDraft.self, from: data)
    }

    func deleteDraft(id: UUID) throws {
        let url = draftURL(id: id)
        guard fileManager.fileExists(atPath: url.path) else {
            throw StoreError.draftNotFound(id)
        }
        try fileManager.removeItem(at: url)
    }

    // MARK: Images

    func imageURL(fileName: String) -> URL {
        imagesURL.appendingPathComponent(fileName)
    }

    /// Writes the JPEG and returns its file name (`<uuid>.jpg` unless one is given).
    @discardableResult
    func saveImage(_ data: Data, fileName: String? = nil) throws -> String {
        ensureDirectories()
        let name = fileName ?? (UUID().uuidString + ".jpg")
        try data.write(to: imageURL(fileName: name), options: .atomic)
        return name
    }

    func loadImage(fileName: String) -> Data? {
        try? Data(contentsOf: imageURL(fileName: fileName))
    }

    func imageExists(fileName: String) -> Bool {
        fileManager.fileExists(atPath: imageURL(fileName: fileName).path)
    }

    func deleteImage(fileName: String) {
        try? fileManager.removeItem(at: imageURL(fileName: fileName))
    }

    func listImageFileNames() -> [String] {
        let urls = (try? fileManager.contentsOfDirectory(at: imagesURL, includingPropertiesForKeys: nil)) ?? []
        return urls.map { $0.lastPathComponent }.sorted()
    }

    /// Deletes image files that no row in the working queue or in any named draft references.
    /// Returns the number of files removed.
    @discardableResult
    func pruneOrphanImages(keeping referenced: Set<String>) -> Int {
        var keep = referenced
        if let current = loadCurrent() {
            keep.formUnion(Self.imageFileNames(in: current.rows + [current.draft]))
        }
        let draftURLs = (try? fileManager.contentsOfDirectory(at: draftsURL, includingPropertiesForKeys: nil)) ?? []
        for url in draftURLs where url.pathExtension.lowercased() == "json" {
            if let data = try? Data(contentsOf: url),
               let draft = try? JSONCoding.decoder.decode(NamedDraft.self, from: data) {
                keep.formUnion(Self.imageFileNames(in: draft.rows))
            }
        }
        var removed = 0
        for name in listImageFileNames() where !keep.contains(name) {
            deleteImage(fileName: name)
            removed += 1
        }
        return removed
    }

    static func imageFileNames(in rows: [QueueRow]) -> Set<String> {
        var names = Set<String>()
        for row in rows {
            if let name = row.imageFront.localFileName {
                names.insert(name)
            }
            if let name = row.imageBack.localFileName {
                names.insert(name)
            }
        }
        return names
    }
}
