import SwiftUI

/// Sheet listing the named local drafts. Tapping one previews its first rows
/// (`QueuePreviewSheet`) before it replaces the working queue; swipe to delete.
@MainActor
struct DraftsView: View {
    /// A draft loaded in full for the preview sheet.
    struct Opened: Identifiable {
        let draft: NamedDraft
        let rows: [QueueRow]

        var id: UUID { draft.id }
    }

    @Environment(QueueViewModel.self) private var queue
    @Environment(PreviewViewModel.self) private var preview
    @Environment(\.dismiss) private var dismiss

    @State private var drafts: [NamedDraftSummary] = []
    @State private var opened: Opened?

    var body: some View {
        NavigationStack {
            List {
                if drafts.isEmpty {
                    Text("No saved drafts. Use \"Save as draft…\" in the Queue menu.")
                        .foregroundStyle(.secondary)
                }
                ForEach(drafts) { draft in
                    Button {
                        show(draft)
                    } label: {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(draft.name)
                                .foregroundStyle(.primary)
                            Text("\(draft.rowCount) rows · \(draft.updatedAt)")
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
                .onDelete { offsets in
                    delete(at: offsets)
                }
            }
            .navigationTitle("Drafts")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .topBarTrailing) {
                    Button("Done") { dismiss() }
                }
            }
            .sheet(item: $opened) { opened in
                QueuePreviewSheet(
                    title: opened.draft.name,
                    subtitle: "Draft saved on this phone, updated \(opened.draft.updatedAt).",
                    rows: opened.rows,
                    currentCount: queue.rows.count
                ) {
                    open(opened.draft)
                }
            }
            .onAppear { reload() }
        }
    }

    private func reload() {
        drafts = queue.listDrafts()
    }

    private func show(_ summary: NamedDraftSummary) {
        guard let named = queue.namedDraft(id: summary.id) else {
            queue.errorMessage = QueueViewModel.QueueError.draftNotFound.localizedDescription
            reload()
            return
        }
        opened = Opened(draft: named, rows: queue.autofilled(named.rows))
    }

    private func open(_ draft: NamedDraft) {
        do {
            try queue.openDraft(id: draft.id)
            preview.reset()
            dismiss()
        } catch {
            queue.errorMessage = error.localizedDescription
            reload()
        }
    }

    private func delete(at offsets: IndexSet) {
        for index in offsets where drafts.indices.contains(index) {
            queue.deleteDraft(id: drafts[index].id)
        }
        reload()
    }
}
