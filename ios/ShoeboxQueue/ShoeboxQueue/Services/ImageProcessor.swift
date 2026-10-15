import Foundation
import Observation
import UIKit

/// Downscales library photos before they are stored and uploaded. Pure functions; call them off
/// the main actor (`Task.detached`).
enum ImageProcessor {
    struct ProcessingError: LocalizedError {
        var errorDescription: String? {
            "The selected image could not be decoded."
        }
    }

    static let defaultMaxLongEdge: CGFloat = 2000
    static let defaultQuality: CGFloat = 0.85

    /// Re-encodes `data` as a JPEG whose long edge is at most `maxLongEdge` pixels. Drawing through
    /// `UIGraphicsImageRenderer` bakes the EXIF orientation in, so the server sees an upright image.
    static func downscaledJPEG(_ data: Data, maxLongEdge: CGFloat = defaultMaxLongEdge, quality: CGFloat = defaultQuality) throws -> Data {
        guard let image = UIImage(data: data) else {
            throw ProcessingError()
        }
        let target = targetSize(for: image.size, maxEdge: maxLongEdge)
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        format.opaque = true
        let renderer = UIGraphicsImageRenderer(size: target, format: format)
        return renderer.jpegData(withCompressionQuality: quality) { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
    }

    /// A small upright preview for the form.
    static func thumbnail(_ data: Data, maxEdge: CGFloat = 240) -> UIImage? {
        guard let image = UIImage(data: data) else {
            return nil
        }
        let target = targetSize(for: image.size, maxEdge: maxEdge)
        let format = UIGraphicsImageRendererFormat.default()
        format.scale = 1
        let renderer = UIGraphicsImageRenderer(size: target, format: format)
        return renderer.image { _ in
            image.draw(in: CGRect(origin: .zero, size: target))
        }
    }

    static func targetSize(for size: CGSize, maxEdge: CGFloat) -> CGSize {
        let longEdge = max(size.width, size.height)
        guard longEdge > 0 else {
            return CGSize(width: 1, height: 1)
        }
        let scale = longEdge > maxEdge ? maxEdge / longEdge : 1
        return CGSize(
            width: max(1, (size.width * scale).rounded(.down)),
            height: max(1, (size.height * scale).rounded(.down))
        )
    }
}

enum UploadState: Hashable {
    case idle
    case uploading(Double)
    case done(String)
    case failed(String)

    var isUploading: Bool {
        if case .uploading = self {
            return true
        }
        return false
    }
}

/// Tracks `POST /images` per local file. The `client_id` is the file's UUID stem, so a retry
/// overwrites the same object instead of leaving strays in `uploads/`.
@MainActor
@Observable
final class ImageUploader {
    private(set) var states: [String: UploadState] = [:]

    func state(for fileName: String) -> UploadState {
        states[fileName] ?? .idle
    }

    func reset(fileName: String) {
        states[fileName] = nil
    }

    static func clientId(for fileName: String) -> String? {
        let stem = (fileName as NSString).deletingPathExtension
        let cleaned = stem.filter { character in
            character.isASCII && (character.isLetter || character.isNumber || character == "-" || character == "_")
        }
        guard cleaned.count >= 8 else {
            return nil
        }
        return String(cleaned.prefix(64))
    }

    /// Uploads and returns the `gs://` URI. Throws on failure after recording `.failed`.
    func upload(fileName: String, data: Data, side: ImageSide, api: any QueueAPI) async throws -> String {
        states[fileName] = .uploading(0)
        let clientId = Self.clientId(for: fileName)
        let progress: UploadProgressHandler = { [weak self] fraction in
            Task { @MainActor in
                self?.setProgress(fileName: fileName, fraction: fraction)
            }
        }
        do {
            let response = try await api.uploadImage(jpeg: data, side: side, clientId: clientId, progress: progress)
            states[fileName] = .done(response.uri)
            return response.uri
        } catch {
            states[fileName] = .failed(error.localizedDescription)
            throw error
        }
    }

    private func setProgress(fileName: String, fraction: Double) {
        if case .uploading = states[fileName] ?? .idle {
            states[fileName] = .uploading(fraction)
        }
    }
}
