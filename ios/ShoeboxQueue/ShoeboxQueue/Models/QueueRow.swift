import Foundation

/// Which side of the card an image belongs to. Raw values are the `side` form field of `POST /images`.
enum ImageSide: String, Codable, Hashable, CaseIterable {
    case front
    case back

    var title: String {
        switch self {
        case .front:
            return "Front"
        case .back:
            return "Back"
        }
    }
}

/// How a row references an image.
///
/// - `scan`: a bare scan number typed like in the Streamlit UI. It is sent verbatim and the
///   workstation resolves it with `shoebox.ui.helpers.handle_image_path`.
/// - `photo`: a picture from the phone's library. It lives locally as `images/<uuid>.jpg` until it
///   is uploaded through `POST /images`, after which `remoteUri` holds the `gs://` URI that goes on
///   the wire.
struct ImageRef: Codable, Hashable {
    enum Kind: String, Codable, Hashable {
        case none
        case scan
        case photo
    }

    var kind: Kind = .none
    var scanNumber: String = ""
    var remoteUri: String? = nil
    var localFileName: String? = nil

    /// No image.
    static let empty = ImageRef()

    static func scan(_ number: String) -> ImageRef {
        ImageRef(kind: .scan, scanNumber: number.trimmingCharacters(in: .whitespacesAndNewlines), remoteUri: nil, localFileName: nil)
    }

    static func photo(localFileName: String?, remoteUri: String? = nil) -> ImageRef {
        ImageRef(kind: .photo, scanNumber: "", remoteUri: remoteUri, localFileName: localFileName)
    }

    /// The string that goes into `image_front` / `image_back` on the wire.
    var wireValue: String {
        switch kind {
        case .none:
            return ""
        case .scan:
            return scanNumber.trimmingCharacters(in: .whitespacesAndNewlines)
        case .photo:
            return remoteUri ?? ""
        }
    }

    /// True when the reference carries nothing the user entered.
    var isBlank: Bool {
        switch kind {
        case .none:
            return true
        case .scan:
            return wireValue.isEmpty
        case .photo:
            return false
        }
    }

    /// A photo that has not been uploaded yet.
    var needsUpload: Bool {
        kind == .photo && (remoteUri ?? "").isEmpty
    }

    var isUploaded: Bool {
        kind == .photo && !(remoteUri ?? "").isEmpty
    }

    /// Inverse of `wireValue` for values coming back from the API (`GET /queue`).
    static func fromWire(_ value: String) -> ImageRef {
        let trimmed = value.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            return .empty
        }
        if trimmed.lowercased().hasPrefix("gs://") {
            return .photo(localFileName: nil, remoteUri: trimmed)
        }
        return .scan(trimmed)
    }
}

/// One queue row as the phone sees it: the 8 user-entered fields plus display-only autofill
/// fields copied from the metadata (the server recomputes them on preview / submit).
struct QueueRow: Codable, Hashable, Identifiable {
    /// Every new row starts at this price (the usual auction start); it is never carried over
    /// from the previous row.
    static let defaultPrice = 0.99

    var id: UUID = UUID()

    // --- the 8 user fields (QueueRowIn) ---
    var setName: String = ""
    var subsetName: String = ""
    var parallelVariety: String = ""
    var cardNumber: String = ""
    var quantity: Int = 1
    var price: Double = QueueRow.defaultPrice
    var imageFront: ImageRef = .empty
    var imageBack: ImageRef = .empty

    // --- display-only autofill (mirrors metadata_update_draft in shoebox/ui/app.py) ---
    var player: String? = nil
    var team: String? = nil
    var setYear: String? = nil
    var subsetType: String? = nil
    var note: String? = nil
    var noteCheck: String? = nil
    var printRun: Int? = nil
    var parallelNote: String? = nil

    /// Set, subset and card # are required for a row to be enriched.
    var isComplete: Bool {
        !setName.trimmingCharacters(in: .whitespaces).isEmpty
            && !subsetName.trimmingCharacters(in: .whitespaces).isEmpty
            && !cardNumber.trimmingCharacters(in: .whitespaces).isEmpty
    }

    var needsUpload: Bool {
        imageFront.needsUpload || imageBack.needsUpload
    }

    var hasCardAutofill: Bool {
        player != nil || team != nil || setYear != nil || subsetType != nil || note != nil || noteCheck != nil
    }

    var hasParallelAutofill: Bool {
        printRun != nil || parallelNote != nil
    }

    /// Same text as the Streamlit expander / caption: `"N. set | subset | parallel | card"`.
    func listLabel(index: Int) -> String {
        "\(index + 1). \(setName) | \(subsetName) | \(parallelVariety) | \(cardNumber)"
    }

    /// The second line of a queue row: quantity, price, player and both images.
    var queueSubtitle: String {
        var parts: [String] = ["qty \(quantity)", String(format: "$%.2f", price)]
        if let player, !player.isEmpty {
            parts.append(player)
        }
        parts.append("front: \(imageFront.queueSummary)")
        parts.append("back: \(imageBack.queueSummary)")
        return parts.joined(separator: " · ")
    }

    /// The metadata autofill as one line under Card #: player · team · year · subset type, then
    /// any note, note check, print run ("/50") and parallel note. Empty when nothing matched.
    var autofillSummary: String {
        var parts = [player, team, setYear, subsetType, note, noteCheck]
        parts.append(printRun.map { "/\($0)" })
        parts.append(parallelNote)
        return parts
            .compactMap { $0?.trimmingCharacters(in: .whitespacesAndNewlines) }
            .filter { !$0.isEmpty }
            .joined(separator: " · ")
    }

    func image(for side: ImageSide) -> ImageRef {
        switch side {
        case .front:
            return imageFront
        case .back:
            return imageBack
        }
    }

    mutating func setImage(_ ref: ImageRef, for side: ImageSide) {
        switch side {
        case .front:
            imageFront = ref
        case .back:
            imageBack = ref
        }
    }

    mutating func clearCardAutofill() {
        player = nil
        team = nil
        setYear = nil
        subsetType = nil
        note = nil
        noteCheck = nil
    }

    mutating func clearParallelAutofill() {
        printRun = nil
        parallelNote = nil
    }

    /// The exact 8-key wire shape for preview / submit / draft.
    func toInput() -> QueueRowInput {
        let trimmedParallel = parallelVariety.trimmingCharacters(in: .whitespacesAndNewlines)
        return QueueRowInput(
            setName: setName.trimmingCharacters(in: .whitespacesAndNewlines),
            subsetName: subsetName.trimmingCharacters(in: .whitespacesAndNewlines),
            parallelVariety: trimmedParallel.isEmpty ? nil : trimmedParallel,
            cardNumber: cardNumber.trimmingCharacters(in: .whitespacesAndNewlines),
            quantity: min(max(quantity, 1), 999),
            price: QueueRowInput.roundedPrice(max(price, 0)),
            imageFront: imageFront.wireValue,
            imageBack: imageBack.wireValue
        )
    }

    /// Rebuild a row from the raw fields the API hands back (`GET /queue`). Autofill is not
    /// restored here; `QueueViewModel.applyAutofill` re-runs it against the current metadata.
    static func fromInput(_ input: QueueRowInput) -> QueueRow {
        var row = QueueRow()
        row.setName = input.setName
        row.subsetName = input.subsetName
        row.parallelVariety = input.parallelVariety ?? ""
        row.cardNumber = input.cardNumber
        row.quantity = min(max(input.quantity, 1), 999)
        row.price = max(input.price, 0)
        row.imageFront = ImageRef.fromWire(input.imageFront)
        row.imageBack = ImageRef.fromWire(input.imageBack)
        return row
    }
}

/// `QueueRowIn` / `DraftRowIn` on the wire (snake_case via the shared encoder):
/// `set_name, subset_name, parallel_variety, card_number, quantity, price, image_front, image_back`.
///
/// Decoding is synthesized. Encoding is written out so that `parallel_variety` is emitted as an
/// explicit `null` (a synthesized encoder would drop the key for `nil`). The key enum below has no
/// raw-value overrides: every case name is the property name, so the snake_case strategy still
/// produces the wire keys.
struct QueueRowInput: Codable, Hashable {
    var setName: String
    var subsetName: String
    var parallelVariety: String?
    var cardNumber: String
    var quantity: Int
    var price: Double
    var imageFront: String
    var imageBack: String

    enum CodingKeys: String, CodingKey {
        case setName
        case subsetName
        case parallelVariety
        case cardNumber
        case quantity
        case price
        case imageFront
        case imageBack
    }

    init(
        setName: String,
        subsetName: String,
        parallelVariety: String?,
        cardNumber: String,
        quantity: Int,
        price: Double,
        imageFront: String,
        imageBack: String
    ) {
        self.setName = setName
        self.subsetName = subsetName
        self.parallelVariety = parallelVariety
        self.cardNumber = cardNumber
        self.quantity = quantity
        self.price = price
        self.imageFront = imageFront
        self.imageBack = imageBack
    }

    init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        setName = try container.decodeIfPresent(String.self, forKey: .setName) ?? ""
        subsetName = try container.decodeIfPresent(String.self, forKey: .subsetName) ?? ""
        parallelVariety = try container.decodeIfPresent(String.self, forKey: .parallelVariety)
        cardNumber = try container.decodeIfPresent(String.self, forKey: .cardNumber) ?? ""
        quantity = try container.decodeIfPresent(Int.self, forKey: .quantity) ?? 1
        price = try container.decodeIfPresent(Double.self, forKey: .price) ?? 0
        imageFront = try container.decodeIfPresent(String.self, forKey: .imageFront) ?? ""
        imageBack = try container.decodeIfPresent(String.self, forKey: .imageBack) ?? ""
    }

    func encode(to encoder: Encoder) throws {
        var container = encoder.container(keyedBy: CodingKeys.self)
        try container.encode(setName, forKey: .setName)
        try container.encode(subsetName, forKey: .subsetName)
        if let parallelVariety {
            try container.encode(parallelVariety, forKey: .parallelVariety)
        } else {
            try container.encodeNil(forKey: .parallelVariety)
        }
        try container.encode(cardNumber, forKey: .cardNumber)
        try container.encode(quantity, forKey: .quantity)
        try container.encode(price, forKey: .price)
        try container.encode(imageFront, forKey: .imageFront)
        try container.encode(imageBack, forKey: .imageBack)
    }

    /// Prices travel with two decimals, like the Streamlit `%.2f` field.
    static func roundedPrice(_ value: Double) -> Double {
        (value * 100).rounded() / 100
    }
}

extension ImageRef {
    /// "none", "scan 234", "photo" or "photo (pending)" for the queue list and previews.
    var queueSummary: String {
        switch kind {
        case .none:
            return "none"
        case .scan:
            return scanNumber.isEmpty ? "none" : "scan \(scanNumber)"
        case .photo:
            return isUploaded ? "photo" : "photo (pending)"
        }
    }
}
