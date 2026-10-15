import PhotosUI
import SwiftUI
import UIKit

/// One image slot of the form: a scan number typed like on the workstation, or a photo from the
/// library that is uploaded through the API.
@MainActor
struct ImageFieldView: View {
    enum Mode: String, CaseIterable, Hashable {
        case scan = "Scan #"
        case photo = "Photo"
    }

    let side: ImageSide

    @Environment(QueueViewModel.self) private var queue
    @Environment(ImageUploader.self) private var uploader

    @State private var mode: Mode = .scan
    @State private var pickerItem: PhotosPickerItem?
    @State private var thumbnail: UIImage?

    private var ref: ImageRef {
        queue.imageRef(for: side)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Text("Image \(side.title)")
                    .font(.headline)
                Spacer()
                if !ref.isBlank {
                    Button("Remove", role: .destructive) {
                        queue.removeImage(side: side)
                        pickerItem = nil
                        thumbnail = nil
                    }
                    .buttonStyle(.borderless)
                    .font(.callout)
                }
            }
            Picker("Source", selection: $mode) {
                ForEach(Mode.allCases, id: \.self) { mode in
                    Text(mode.rawValue).tag(mode)
                }
            }
            .pickerStyle(.segmented)
            .labelsHidden()

            switch mode {
            case .scan:
                scanField
            case .photo:
                photoField
            }
        }
        .padding(.vertical, 4)
        .onAppear { syncMode() }
        .onChange(of: ref.kind) { syncMode() }
        .onChange(of: pickerItem) { handlePickerChange() }
        .task(id: ref.localFileName) { await loadThumbnail() }
    }

    // MARK: Scan number

    private var scanField: some View {
        VStack(alignment: .leading, spacing: 4) {
            TextField(
                "Scan number (e.g. 123)",
                text: Binding(
                    get: { ref.kind == .scan ? ref.scanNumber : "" },
                    set: { queue.setScanNumber($0, side: side) }
                )
            )
            .keyboardType(.numberPad)
            .autocorrectionDisabled()
            .onSubmit { KeyboardDismissModifier.hideKeyboard() }
            Text(scanCaption)
                .font(.caption)
                .foregroundStyle(.secondary)
        }
    }

    private var scanCaption: String {
        let number = ref.kind == .scan ? ref.scanNumber.trimmingCharacters(in: .whitespacesAndNewlines) : ""
        if number.isEmpty {
            return "Sent verbatim; the workstation resolves it under scans/ with its scan_prefix / padding / extension, e.g. scans/SCAN_0123.jpg."
        }
        return "-> scans/\(Self.exampleFileName(for: number)) on the workstation (prefix and padding come from app.yaml)."
    }

    /// Illustrative only: mirrors `handle_image_path` with the default `SCAN_` / 4 digits / `.jpg`.
    static func exampleFileName(for number: String) -> String {
        let stem = number.split(separator: ".", maxSplits: 1, omittingEmptySubsequences: false).first.map(String.init) ?? number
        let padded = stem.count < 4 ? String(repeating: "0", count: 4 - stem.count) + stem : stem
        return "SCAN_\(padded).jpg"
    }

    // MARK: Photo

    private var photoField: some View {
        VStack(alignment: .leading, spacing: 8) {
            if let thumbnail {
                Image(uiImage: thumbnail)
                    .resizable()
                    .scaledToFit()
                    .frame(maxHeight: 160)
                    .clipShape(RoundedRectangle(cornerRadius: 8))
            } else if ref.isUploaded {
                Label("Uploaded photo (no local copy)", systemImage: "photo")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            PhotosPicker(selection: $pickerItem, matching: .images) {
                Label(ref.kind == .photo ? "Replace photo" : "Choose photo", systemImage: "photo.on.rectangle")
            }
            .buttonStyle(.borderless)
            uploadStatus
        }
    }

    @ViewBuilder
    private var uploadStatus: some View {
        if ref.kind == .photo {
            let state = ref.localFileName.map { uploader.state(for: $0) } ?? UploadState.idle
            if case .uploading(let fraction) = state {
                ProgressView(value: fraction) {
                    Text("Uploading…")
                        .font(.caption)
                }
            } else if ref.isUploaded {
                Label("Uploaded", systemImage: "checkmark.circle")
                    .font(.caption)
                    .foregroundStyle(.green)
                Text(ref.remoteUri ?? "")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.middle)
            } else {
                HStack {
                    Label("Needs upload", systemImage: "exclamationmark.triangle")
                        .font(.caption)
                        .foregroundStyle(.orange)
                    Spacer()
                    if let name = ref.localFileName {
                        Button("Retry") {
                            queue.retryUpload(fileName: name, side: side)
                        }
                        .buttonStyle(.borderless)
                        .font(.callout)
                    }
                }
                if case .failed(let message) = state {
                    Text(message)
                        .font(.caption2)
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        }
    }

    // MARK: Helpers

    private func syncMode() {
        switch ref.kind {
        case .scan:
            mode = .scan
        case .photo:
            mode = .photo
        case .none:
            break
        }
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
            mode = .photo
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
