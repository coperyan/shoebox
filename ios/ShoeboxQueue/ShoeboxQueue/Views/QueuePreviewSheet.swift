import SwiftUI

/// What "Load last submitted queue" and opening a named draft show before they replace the
/// working queue: where the rows come from, how many there are, and the first few of them.
@MainActor
struct QueuePreviewSheet: View {
    static let previewLimit = 5

    let title: String
    let subtitle: String?
    let rows: [QueueRow]
    /// Rows on the Queue tab right now; when non-zero the confirm button is destructive.
    let currentCount: Int
    let onConfirm: () -> Void

    @Environment(\.dismiss) private var dismiss

    private var shown: ArraySlice<QueueRow> {
        rows.prefix(Self.previewLimit)
    }

    private var confirmTitle: String {
        currentCount > 0 ? "Replace queue with \(rows.count) rows" : "Load \(rows.count) rows"
    }

    var body: some View {
        NavigationStack {
            List {
                Section {
                    ForEach(Array(shown.enumerated()), id: \.element.id) { index, row in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.listLabel(index: index))
                                .lineLimit(2)
                            Text(row.queueSubtitle)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                    if rows.count > shown.count {
                        Text("… and \(rows.count - shown.count) more")
                            .foregroundStyle(.secondary)
                    }
                } header: {
                    Text(rows.count > shown.count ? "First \(shown.count) of \(rows.count) rows" : "\(rows.count) rows")
                } footer: {
                    if let subtitle {
                        Text(subtitle)
                    }
                }
                Section {
                    Button(role: currentCount > 0 ? .destructive : nil) {
                        onConfirm()
                        dismiss()
                    } label: {
                        Label(confirmTitle, systemImage: "tray.and.arrow.down")
                    }
                } footer: {
                    if currentCount > 0 {
                        Text("The \(currentCount) rows on the Queue tab will be replaced. Save the queue as a draft first if you want to keep it.")
                    }
                }
            }
            .navigationTitle(title)
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) {
                    Button("Cancel") { dismiss() }
                }
            }
        }
    }
}
