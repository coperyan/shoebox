import SwiftUI

/// Sheet listing the named local drafts. Opening one replaces the working queue (with a
/// confirmation when the queue is not empty); swipe to delete.
@MainActor
struct DraftsView: View {
    @Environment(QueueViewModel.self) private var queue
    @Environment(PreviewViewModel.self) private var preview
    @Environment(\.dismiss) private var dismiss

    @State private var drafts: [NamedDraftSummary] = []
    @State private var pendingOpen: NamedDraftSummary?

    var body: some View {
        NavigationStack {
            List {
                if drafts.isEmpty {
                    Text("No saved drafts. Use \"Save as draft…\" in the Queue menu.")
                        .foregroundStyle(.secondary)
                }
                ForEach(drafts) { draft in
                    Button {
                        if queue.rows.isEmpty {
                            open(draft)
                        } else {
                            pendingOpen = draft
                        }
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
            .confirmationDialog(
                "Replace the current queue?",
                isPresented: pendingOpenBinding,
                titleVisibility: .visible,
                presenting: pendingOpen
            ) { draft in
                Button("Replace with \"\(draft.name)\"", role: .destructive) { open(draft) }
                Button("Cancel", role: .cancel) {}
            } message: { draft in
                Text("The \(queue.rows.count) rows on the Queue tab will be replaced by the \(draft.rowCount) rows of this draft. Save the queue as a draft first if you want to keep it.")
            }
            .onAppear { reload() }
        }
    }

    private var pendingOpenBinding: Binding<Bool> {
        Binding(
            get: { pendingOpen != nil },
            set: { presented in
                if !presented {
                    pendingOpen = nil
                }
            }
        )
    }

    private func reload() {
        drafts = queue.listDrafts()
    }

    private func open(_ draft: NamedDraftSummary) {
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
