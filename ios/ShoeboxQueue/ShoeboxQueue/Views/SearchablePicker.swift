import SwiftUI

/// A `NavigationLink` row that opens a searchable list of options with a "None" entry on top.
/// Options can be flat (`options:`) or sectioned (`groups:`, e.g. sets by year). Pressing the
/// Search key selects the best match (`PickerSearch.firstMatch`). A selection that is no longer
/// in the options (older metadata) is shown as "(not in metadata)" instead of silently blanking
/// the field.
@MainActor
struct SearchablePicker: View {
    let title: String
    let groups: [PickerGroup]
    @Binding var selection: String

    init(title: String, options: [String], selection: Binding<String>) {
        self.init(title: title, groups: [PickerGroup(options: options)], selection: selection)
    }

    init(title: String, groups: [PickerGroup], selection: Binding<String>) {
        self.title = title
        self.groups = groups
        self._selection = selection
    }

    private var options: [String] {
        groups.flatMap { $0.options }
    }

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
            SearchablePickerList(title: title, groups: groups, selection: $selection)
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
    let groups: [PickerGroup]
    @Binding var selection: String

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""

    init(title: String, options: [String], selection: Binding<String>) {
        self.init(title: title, groups: [PickerGroup(options: options)], selection: selection)
    }

    init(title: String, groups: [PickerGroup], selection: Binding<String>) {
        self.title = title
        self.groups = groups
        self._selection = selection
    }

    private var options: [String] {
        groups.flatMap { $0.options }
    }

    /// A single untitled group renders as one flat list, as before; anything else is sectioned.
    private var isSectioned: Bool {
        groups.count > 1 || groups.first?.title != nil
    }

    private var filteredGroups: [PickerGroup] {
        PickerSearch.filter(groups, query: query)
    }

    private var isStale: Bool {
        !selection.isEmpty && !options.contains(selection)
    }

    var body: some View {
        List {
            Section {
                Button {
                    select("")
                } label: {
                    row("None", selected: selection.isEmpty)
                }
                if isStale {
                    row("\(selection) (not in metadata)", selected: true)
                        .foregroundStyle(.orange)
                }
                if !isSectioned, let group = filteredGroups.first {
                    optionRows(group.options)
                }
                if filteredGroups.isEmpty && !options.isEmpty {
                    Text("No matches for \"\(query)\"")
                        .foregroundStyle(.secondary)
                }
                if options.isEmpty {
                    Text("No options — choose the parent fields first, or refresh the metadata.")
                        .foregroundStyle(.secondary)
                }
            }
            if isSectioned {
                ForEach(Array(filteredGroups.enumerated()), id: \.offset) { _, group in
                    Section {
                        optionRows(group.options)
                    } header: {
                        if let title = group.title {
                            Text(title)
                        }
                    }
                }
            }
        }
        .searchable(text: $query, placement: .navigationBarDrawer(displayMode: .always), prompt: "Search")
        .onSubmit(of: .search) {
            if let match = PickerSearch.firstMatch(in: groups, query: query) {
                select(match)
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
    }

    private func select(_ value: String) {
        selection = value
        dismiss()
    }

    @ViewBuilder
    private func optionRows(_ options: [String]) -> some View {
        ForEach(Array(options.enumerated()), id: \.offset) { _, option in
            Button {
                select(option)
            } label: {
                row(option, selected: option == selection)
            }
        }
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
