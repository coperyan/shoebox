import SwiftUI

@main
struct ShoeboxQueueApp: App {
    @State private var settings: AppSettings
    @State private var metadata: MetadataStore
    @State private var uploader: ImageUploader
    @State private var queue: QueueViewModel
    @State private var preview: PreviewViewModel
    @State private var listing: ListingRunViewModel

    init() {
        let store = DraftStore(baseURL: DraftStore.defaultDirectory)
        let settings = AppSettings()
        let metadata = MetadataStore(directory: store.baseURL)
        let uploader = ImageUploader()
        let queue = QueueViewModel(
            store: store,
            metadata: metadata,
            uploader: uploader,
            apiProvider: { settings.makeClient() }
        )
        _settings = State(initialValue: settings)
        _metadata = State(initialValue: metadata)
        _uploader = State(initialValue: uploader)
        _queue = State(initialValue: queue)
        _preview = State(initialValue: PreviewViewModel())
        _listing = State(initialValue: ListingRunViewModel())
    }

    var body: some Scene {
        WindowGroup {
            RootView()
                .environment(settings)
                .environment(metadata)
                .environment(uploader)
                .environment(queue)
                .environment(preview)
                .environment(listing)
                .task {
                    // restore -> disk cache -> conditional refresh (cached copy stays if it fails)
                    queue.restore()
                    await metadata.loadCache()
                    queue.refreshAutofill()
                    if let api = settings.makeClient() {
                        await metadata.refresh(using: api, force: false)
                        queue.refreshAutofill()
                    }
                    queue.store.pruneOrphanImages(keeping: [])
                }
        }
    }
}
