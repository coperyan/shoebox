import SwiftUI

/// "Add / Edit Row" — the single form from the Streamlit UI. Place it inside a `List` / `Form`.
@MainActor
struct RowFormView: View {
    @Environment(QueueViewModel.self) private var queue

    var body: some View {
        @Bindable var queue = queue

        Section {
            SearchablePicker(
                title: "Set Name",
                options: queue.setOptions,
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
                )
            )
            .disabled(queue.draft.subsetName.isEmpty)
        } header: {
            Text("Add / Edit Row")
        } footer: {
            Text("Mode: \(queue.mode.title)")
        }

        if queue.draft.hasCardAutofill || queue.draft.hasParallelAutofill {
            Section("Card details (from metadata)") {
                if queue.draft.hasCardAutofill {
                    autofillRow("Player", queue.draft.player)
                    autofillRow("Team", queue.draft.team)
                    autofillRow("Year", queue.draft.setYear)
                    autofillRow("Subset type", queue.draft.subsetType)
                    autofillRow("Note", queue.draft.note)
                    autofillRow("Note check", queue.draft.noteCheck)
                }
                if queue.draft.hasParallelAutofill {
                    autofillRow("Print run", queue.draft.printRun.map { String($0) })
                    autofillRow("Parallel note", queue.draft.parallelNote)
                }
            }
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
                }
            }
        }

        Section("Images") {
            ImageFieldView(side: .front)
            ImageFieldView(side: .back)
        }

        Section {
            Button {
                queue.saveDraft()
            } label: {
                Label(queue.mode == .add ? "Save (Add)" : "Save (Update)", systemImage: "checkmark.circle")
            }
            .disabled(!queue.canSaveDraft)

            Button(role: .cancel) {
                queue.cancelDraft()
            } label: {
                Label("Cancel / New", systemImage: "arrow.uturn.backward")
            }
        } footer: {
            if !queue.canSaveDraft {
                Text("Set, subset and card # are required.")
            }
        }
    }

    @ViewBuilder
    private func autofillRow(_ title: String, _ value: String?) -> some View {
        if let value, !value.isEmpty {
            LabeledContent(title) {
                Text(value)
                    .multilineTextAlignment(.trailing)
            }
        }
    }
}
