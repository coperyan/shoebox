import SwiftUI

/// The saved rows, labelled like the Streamlit captions. Tap to edit, swipe to delete, reorder
/// in edit mode. Place it inside a `List` section.
@MainActor
struct QueueListView: View {
    @Environment(QueueViewModel.self) private var queue

    var body: some View {
        if queue.rows.isEmpty {
            Text("No rows added yet.")
                .foregroundStyle(.secondary)
        } else {
            ForEach(Array(queue.rows.enumerated()), id: \.element.id) { index, row in
                Button {
                    queue.beginEdit(at: index)
                } label: {
                    HStack(spacing: 8) {
                        VStack(alignment: .leading, spacing: 2) {
                            Text(row.listLabel(index: index))
                                .foregroundStyle(.primary)
                                .lineLimit(2)
                            Text(row.queueSubtitle)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                        Spacer()
                        if row.needsUpload {
                            Image(systemName: "exclamationmark.triangle.fill")
                                .foregroundStyle(.orange)
                                .accessibilityLabel("Photo not uploaded")
                        }
                        if queue.editIndex == index {
                            Image(systemName: "pencil.circle.fill")
                                .foregroundStyle(.tint)
                                .accessibilityLabel("Editing")
                        }
                    }
                }
            }
            .onDelete { offsets in
                queue.delete(at: offsets)
            }
            .onMove { source, destination in
                queue.move(from: source, to: destination)
            }
        }
    }
}
