import SwiftUI

/// "Add / Edit Row" — the single form from the Streamlit UI. Place it inside a `List` / `Form`.
/// Save and Clear live in the navigation bar (`RootView`), not in the form. Order follows the
/// data-entry flow: card -> images -> quantity & price; picking a card # jumps to the first
/// image field and the keyboard's Next button walks the rest. Checking several card #s starts a
/// batch: the form steps through them, one Save per card.
@MainActor
struct RowFormView: View {
    /// The typed fields, in Next order.
    enum Field: Hashable {
        case imageFront
        case imageBack
        case quantity
        case price

        static func image(_ side: ImageSide) -> Field {
            side == .front ? .imageFront : .imageBack
        }

        var imageSide: ImageSide? {
            switch self {
            case .imageFront:
                return .front
            case .imageBack:
                return .back
            case .quantity, .price:
                return nil
            }
        }

        var next: Field? {
            switch self {
            case .imageFront:
                return .imageBack
            case .imageBack:
                return .quantity
            case .quantity:
                return .price
            case .price:
                return nil
            }
        }
    }

    /// Long enough for the picker's pop animation to finish; focusing earlier is dropped.
    private static let focusAfterPop: Duration = .milliseconds(550)

    let focus: FocusState<Field?>.Binding

    @Environment(QueueViewModel.self) private var queue

    var body: some View {
        @Bindable var queue = queue

        Section {
            SearchablePicker(
                title: "Set Name",
                groups: queue.setGroups,
                selection: Binding(
                    get: { queue.draft.setName },
                    set: { queue.selectSet($0) }
                )
            )
            SearchablePicker(
                title: "Subset Name",
                options: queue.subsetOptions,
                selection: Binding(
                    get: { queue.draft.subsetName },
                    set: { queue.selectSubset($0) }
                )
            )
            .disabled(queue.draft.setName.isEmpty)
            SearchablePicker(
                title: "Parallel / Variety (optional)",
                options: queue.parallelOptions,
                selection: Binding(
                    get: { queue.draft.parallelVariety },
                    set: { queue.selectParallel($0) }
                )
            )
            .disabled(queue.draft.subsetName.isEmpty)
            SearchablePicker(
                title: "Card #",
                options: queue.cardOptions,
                selection: Binding(
                    get: { queue.draft.cardNumber },
                    set: { queue.selectCard($0) }
                ),
                subtitle: queue.draft.autofillSummary,
                focusesSearch: true,
                onSelect: { _ in focusFirstImage() },
                onSelectMany: queue.mode == .add ? { cards in
                    queue.startBatch(cards)
                    focusFirstImage()
                } : nil
            )
            .disabled(queue.draft.subsetName.isEmpty)
            if queue.mode == .add, let batch = queue.batch {
                batchRow(batch)
            }
        } header: {
            Text("Add / Edit Row")
        } footer: {
            if queue.canSaveDraft {
                Text("Mode: \(queue.mode.title). Save and Clear are in the top bar.")
            } else {
                Text("Mode: \(queue.mode.title). Set, subset and card # are required.")
            }
        }

        Section("Images") {
            ImageFieldView(side: .front, focus: focus)
            ImageFieldView(side: .back, focus: focus)
        }

        Section("Quantity & Price") {
            Stepper(value: $queue.draft.quantity, in: 1 ... 999) {
                HStack {
                    Text("Quantity")
                    Spacer()
                    TextField("Qty", value: $queue.draft.quantity, format: .number)
                        .keyboardType(.numberPad)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 72)
                        .focused(focus, equals: .quantity)
                }
            }
            Stepper(value: $queue.draft.price, in: 0 ... 1_000_000, step: 0.5) {
                HStack {
                    Text("Price")
                    Spacer()
                    TextField("0.00", value: $queue.draft.price, format: .number.precision(.fractionLength(2)))
                        .keyboardType(.decimalPad)
                        .multilineTextAlignment(.trailing)
                        .frame(width: 100)
                        .focused(focus, equals: .price)
                }
            }
        }
    }

    private func batchRow(_ batch: CardBatch) -> some View {
        HStack(spacing: 12) {
            VStack(alignment: .leading, spacing: 2) {
                Label(batch.progress, systemImage: "square.stack.3d.up")
                    .font(.subheadline.weight(.semibold))
                Text(batch.upcoming.isEmpty ? "Last card — Save finishes the batch." : "Next: " + batch.upcoming.map { "#\($0)" }.joined(separator: ", "))
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            Spacer()
            Button("Skip") {
                queue.skipBatchCard()
                focusFirstImage(after: .zero)
            }
            .buttonStyle(.borderless)
            Button("End", role: .destructive) {
                queue.endBatch()
            }
            .buttonStyle(.borderless)
        }
    }

    /// After a card # is picked: the first image side that takes a scan number.
    private func focusFirstImage(after delay: Duration? = nil) {
        guard let target = queue.firstScanSide.map(Field.image) else {
            return
        }
        Task { @MainActor in
            try? await Task.sleep(for: delay ?? Self.focusAfterPop)
            focus.wrappedValue = target
        }
    }
}
