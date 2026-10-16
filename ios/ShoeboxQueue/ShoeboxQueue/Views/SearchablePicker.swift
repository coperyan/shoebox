import SwiftUI

/// A `NavigationLink` row that opens a searchable list of options with a "None" entry on top.
/// Options can be flat (`options:`) or sectioned (`groups:`, e.g. sets by year). Pressing the
/// Search key selects the best match (`PickerSearch.firstMatch`). A selection that is no longer
/// in the options (older metadata) is shown as "(not in metadata)" instead of silently blanking
/// the field. With `onSelectMany`, each option also gets a checkbox; once any is checked, taps
/// and the Search key check instead of picking, and a bottom button hands the checked set over.
@MainActor
struct SearchablePicker: View {
    let title: String
    let groups: [PickerGroup]
    @Binding var selection: String
    /// A small italic line under the title (e.g. the card's player / team / year).
    var subtitle: String = ""
    /// Called after a non-empty option is picked and the list is dismissed.
    var onSelect: ((String) -> Void)? = nil
    /// Called with two or more checked options (in no particular order) when the list is
    /// dismissed with them; nil hides the checkboxes.
    var onSelectMany: (([String]) -> Void)? = nil
    /// Open the list with the search field focused and the keyboard up.
    var focusesSearch = false

    init(
        title: String,
        options: [String],
        selection: Binding<String>,
        subtitle: String = "",
        focusesSearch: Bool = false,
        onSelect: ((String) -> Void)? = nil,
        onSelectMany: (([String]) -> Void)? = nil
    ) {
        self.init(
            title: title,
            groups: [PickerGroup(options: options)],
            selection: selection,
            subtitle: subtitle,
            focusesSearch: focusesSearch,
            onSelect: onSelect,
            onSelectMany: onSelectMany
        )
    }

    init(
        title: String,
        groups: [PickerGroup],
        selection: Binding<String>,
        subtitle: String = "",
        focusesSearch: Bool = false,
        onSelect: ((String) -> Void)? = nil,
        onSelectMany: (([String]) -> Void)? = nil
    ) {
        self.title = title
        self.groups = groups
        self._selection = selection
        self.subtitle = subtitle
        self.focusesSearch = focusesSearch
        self.onSelect = onSelect
        self.onSelectMany = onSelectMany
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
            SearchablePickerList(
                title: title,
                groups: groups,
                selection: $selection,
                focusesSearch: focusesSearch,
                onSelect: onSelect,
                onSelectMany: onSelectMany
            )
        } label: {
            VStack(alignment: .leading, spacing: 2) {
                LabeledContent(title) {
                    Text(displayText)
                        .foregroundStyle(displayColor)
                        .multilineTextAlignment(.trailing)
                }
                if !subtitle.isEmpty {
                    Text(subtitle)
                        .font(.caption)
                        .italic()
                        .foregroundStyle(.secondary)
                        .lineLimit(2)
                }
            }
        }
    }
}

@MainActor
struct SearchablePickerList: View {
    let title: String
    let groups: [PickerGroup]
    @Binding var selection: String
    var focusesSearch = false
    var onSelect: ((String) -> Void)? = nil
    var onSelectMany: (([String]) -> Void)? = nil

    /// Long enough for the push animation to finish; activating the search field earlier is dropped.
    private static let focusAfterPush: Duration = .milliseconds(400)

    @Environment(\.dismiss) private var dismiss
    @State private var query = ""
    @State private var isSearchPresented = false
    @State private var checked: Set<String> = []

    init(
        title: String,
        options: [String],
        selection: Binding<String>,
        focusesSearch: Bool = false,
        onSelect: ((String) -> Void)? = nil,
        onSelectMany: (([String]) -> Void)? = nil
    ) {
        self.init(
            title: title,
            groups: [PickerGroup(options: options)],
            selection: selection,
            focusesSearch: focusesSearch,
            onSelect: onSelect,
            onSelectMany: onSelectMany
        )
    }

    init(
        title: String,
        groups: [PickerGroup],
        selection: Binding<String>,
        focusesSearch: Bool = false,
        onSelect: ((String) -> Void)? = nil,
        onSelectMany: (([String]) -> Void)? = nil
    ) {
        self.title = title
        self.groups = groups
        self._selection = selection
        self.focusesSearch = focusesSearch
        self.onSelect = onSelect
        self.onSelectMany = onSelectMany
    }

    private var allowsMany: Bool {
        onSelectMany != nil
    }

    /// Once something is checked, taps and the Search key check instead of picking.
    private var isChecking: Bool {
        allowsMany && !checked.isEmpty
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
        .searchable(
            text: $query,
            isPresented: $isSearchPresented,
            placement: .navigationBarDrawer(displayMode: .always),
            prompt: "Search"
        )
        .onSubmit(of: .search) {
            guard let match = PickerSearch.firstMatch(in: groups, query: query) else {
                return
            }
            if isChecking {
                checked.insert(match)
                query = ""
            } else {
                select(match)
            }
        }
        .safeAreaInset(edge: .bottom) {
            if isChecking {
                checkedBar
            }
        }
        .navigationTitle(title)
        .navigationBarTitleDisplayMode(.inline)
        .task {
            guard focusesSearch else {
                return
            }
            try? await Task.sleep(for: Self.focusAfterPush)
            isSearchPresented = true
        }
    }

    private var checkedBar: some View {
        HStack {
            Button("Uncheck all") {
                checked = []
            }
            Spacer()
            Button {
                selectChecked()
            } label: {
                Text(checked.count == 1 ? "Add 1 card" : "Add \(checked.count) cards")
                    .fontWeight(.semibold)
            }
            .buttonStyle(.borderedProminent)
        }
        .padding(.horizontal)
        .padding(.vertical, 10)
        .background(.bar)
    }

    private func select(_ value: String) {
        selection = value
        dismiss()
        if !value.isEmpty {
            onSelect?(value)
        }
    }

    private func toggle(_ option: String) {
        if checked.contains(option) {
            checked.remove(option)
        } else {
            checked.insert(option)
        }
    }

    /// One checked option is an ordinary pick; more go to `onSelectMany`.
    private func selectChecked() {
        let values = Array(checked)
        if values.count == 1, let only = values.first {
            select(only)
            return
        }
        dismiss()
        onSelectMany?(values)
    }

    @ViewBuilder
    private func optionRows(_ options: [String]) -> some View {
        ForEach(Array(options.enumerated()), id: \.offset) { _, option in
            if allowsMany {
                HStack(spacing: 12) {
                    Button {
                        toggle(option)
                    } label: {
                        checkbox(checked.contains(option))
                    }
                    .buttonStyle(.borderless)
                    .accessibilityLabel(checked.contains(option) ? "Uncheck \(option)" : "Check \(option)")
                    Button {
                        if isChecking {
                            toggle(option)
                        } else {
                            select(option)
                        }
                    } label: {
                        row(option, selected: !isChecking && option == selection)
                    }
                    .buttonStyle(.borderless)
                }
            } else {
                Button {
                    select(option)
                } label: {
                    row(option, selected: option == selection)
                }
            }
        }
    }

    private func checkbox(_ isChecked: Bool) -> some View {
        Image(systemName: isChecked ? "checkmark.circle.fill" : "circle")
            .imageScale(.large)
            .foregroundStyle(isChecked ? AnyShapeStyle(.tint) : AnyShapeStyle(.secondary))
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
        .contentShape(Rectangle())
    }
}
