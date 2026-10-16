import PhotosUI
import SwiftUI
import UIKit

/// One image slot of the form on a single row: a scan number typed like on the workstation, with
/// a small photo button beside it that picks from the library instead (uploaded through the API).
/// An attached photo replaces the text field with its thumbnail and upload status.
@MainActor
struct ImageFieldView: View {
    let side: ImageSide
    let focus: FocusState<RowFormView.Field?>.Binding

    @Environment(QueueViewModel.self) private var queue
    @Environment(ImageUploader.self) private var uploader

    /// Local text so keystrokes do not touch the draft (see `QueueViewModel.stageScanNumber`).
    @State private var text = ""
    @State private var pickerItem: PhotosPickerItem?
    @State private var thumbnail: UIImage?

    private var ref: ImageRef {
        queue.imageRef(for: side)
    }

    private var field: RowFormView.Field {
        .image(side)
    }

    private var isBlank: Bool {
        ref.kind == .photo ? false : text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 10) {
                Text(side.title)
                    .frame(width: 48, alignment: .leading)
                if ref.kind == .photo {
                    photoSummary
                    Spacer(minLength: 0)
                } else {
                    TextField("Scan #", text: $text)
                        .keyboardType(.numberPad)
                        .autocorrectionDisabled()
                        .focused(focus, equals: field)
                }
                if !isBlank {
                    Button {
                        remove()
                    } label: {
                        Image(systemName: "xmark.circle.fill")
                            .foregroundStyle(.secondary)
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel("Remove \(side.title.lowercased()) image")
                }
                PhotosPicker(selection: $pickerItem, matching: .images) {
                    Image(systemName: "photo.on.rectangle")
                        .imageScale(.large)
                }
                .buttonStyle(.borderless)
                .accessibilityLabel(ref.kind == .photo ? "Replace \(side.title.lowercased()) photo" : "Choose \(side.title.lowercased()) photo")
            }
            uploadFailure
        }
        .onAppear { reloadText() }
        .onChange(of: text) { queue.stageScanNumber(text, side: side) }
        .onChange(of: ref) { reloadText() }
        .onChange(of: queue.draft.id) { reloadText() }
        .onChange(of: focus.wrappedValue == field) { _, isFocused in
            if !isFocused {
                queue.commitStagedScans()
            }
        }
        .onChange(of: pickerItem) { handlePickerChange() }
        .task(id: ref.localFileName) { await loadThumbnail() }
    }

    // MARK: Photo

    private var photoSummary: some View {
        HStack(spacing: 8) {
            Group {
                if let thumbnail {
                    Image(uiImage: thumbnail)
                        .resizable()
                        .scaledToFill()
                } else {
                    Image(systemName: "photo")
                        .foregroundStyle(.secondary)
                }
            }
            .frame(width: 36, height: 36)
            .clipShape(RoundedRectangle(cornerRadius: 6))
            uploadStatus
        }
    }

    @ViewBuilder
    private var uploadStatus: some View {
        let state = ref.localFileName.map { uploader.state(for: $0) } ?? UploadState.idle
        if case .uploading(let fraction) = state {
            ProgressView(value: fraction)
                .frame(width: 60)
                .accessibilityLabel("Uploading")
        } else if ref.isUploaded {
            Label("Uploaded", systemImage: "checkmark.circle")
                .font(.caption)
                .foregroundStyle(.green)
        } else {
            Label("Needs upload", systemImage: "exclamationmark.triangle")
                .font(.caption)
                .foregroundStyle(.orange)
            if let name = ref.localFileName {
                Button("Retry") {
                    queue.retryUpload(fileName: name, side: side)
                }
                .buttonStyle(.borderless)
                .font(.caption)
            }
        }
    }

    @ViewBuilder
    private var uploadFailure: some View {
        if ref.kind == .photo, let name = ref.localFileName, case .failed(let message) = uploader.state(for: name) {
            Text(message)
                .font(.caption2)
                .foregroundStyle(.secondary)
                .lineLimit(2)
        }
    }

    // MARK: Helpers

    private func reloadText() {
        let current = queue.scanText(for: side)
        if text != current {
            text = current
        }
    }

    private func remove() {
        queue.removeImage(side: side)
        text = ""
        pickerItem = nil
        thumbnail = nil
    }

    private func handlePickerChange() {
        guard let item = pickerItem else {
            return
        }
        Task {
            do {
                if let data = try await item.loadTransferable(type: Data.self) {
                    await queue.attachPhoto(data: data, side: side)
                } else {
                    queue.errorMessage = "Could not read the selected photo."
                }
            } catch {
                queue.errorMessage = error.localizedDescription
            }
            pickerItem = nil
        }
    }

    private func loadThumbnail() async {
        guard let name = ref.localFileName, let data = queue.imageData(fileName: name) else {
            thumbnail = nil
            return
        }
        let image = await Task.detached(priority: .utility) { () -> UIImage? in
            ImageProcessor.thumbnail(data)
        }.value
        thumbnail = image
    }
}
