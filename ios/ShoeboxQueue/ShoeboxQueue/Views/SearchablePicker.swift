import SwiftUI

/// A `NavigationLink` row that opens a searchable list of options with a "None" entry on top.
/// A selection that is no longer in `options` (older metadata) is shown as "(not in metadata)"
/// instead of silently blanking the field.
@MainActor
struct SearchablePicker: View {
    let title: String
    let options: [String]
    @Binding var selection: String

    private var isStale: Bool {
        !selection.isEmpty && !options.contains(selection)
    }

    private var displayText: String {
        if selection.isEmpty {
            return "None"
        }
        return isStale ? "\(selection) (not in metadata)" : selection
    }

    private var displayColor: Color {
        if isStale {
            return Color.orange
        }
        return selection.isEmpty ? Color.secondary : Color.primary
    }

    var body: some View {
        NavigationLink {
            SearchablePickerList(title: title, options: options, selection: $selection)
        } label: {
            LabeledContent(title) {
                Text(displayText)
                    .foregroundStyle(displayColor)
                    .multilineTextAlignment(.trailing)
            }
        }
    }
}

@MainActor
struct SearchablePickerList: View {
    let title: String
    let options: [String]
    @Binding var selection: String

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    private var filtered: [String] {
        let trimmed = query.trimmingCharacters(in: .whitespacesAndNewlines)
        if trimmed.isEmpty {
            return options
        }
        return options.filter { $0.localizedCaseInsensitiveContains(trimmed) }
    }

    private var isStale: Bool {
        !selection.isEmpty && !options.contains(selection)
    }

    var body: some View {
        List {
            Button {
                selection = ""
                dismiss()
            } label: {
                row("None", selected: selection.isEmpty)
            }
            if isStale {
                row("\(selection) (not in metadata)", selected: true)
                    .foregroundStyle(.orange)
            }
            ForEach(Array(filtered.enumerated()), id: \.offset) { _, option in
                Button {
                    selection = option
                    dismiss()
                } label: {
                    row(option, selected: option == selection)
                }
            }
            if filtered.isEmpty && !options.isEmpty {
                Text("No matches for \"\(query)\"")
                    .foregroundStyle(.secondary)
            }
            if options.isEmpty {
                Text("No options — choose the parent fields first, or refresh the metadata.")
                    .foregroundStyle(.secondary)
            }
        }
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search")
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private func row(_ text: String, selected: Bool) -> some View {
        HStack {
            Text(text)
                .foregroundStyle(.primary)
            Spacer()
            if selected {
                Image(systemName: "checkmark")
                    .foregroundStyle(.tint)
            }
        }
    }
}
