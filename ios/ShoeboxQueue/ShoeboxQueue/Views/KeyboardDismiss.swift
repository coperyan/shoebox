import SwiftUI
import UIKit

/// A "Done" button above the keyboard plus dismiss-on-scroll, for screens whose fields use the
/// number / decimal pads (which have no return key) or where the return key should close the
/// keyboard instead of leaving it up.
struct KeyboardDismissModifier: ViewModifier {
    /// When set, a "Next" button sits before "Done" (for screens that chain their fields).
    var next: (() -> Void)? = nil
    /// When set, a button with this title sits before "Done" (e.g. Save while working a batch).
    var save: (title: String, action: () -> Void)? = nil

    func body(content: Content) -> some View {
        content
            .scrollDismissesKeyboard(.interactively)
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
                    if let next {
                        Button("Next", action: next)
                    }
                    if let save {
                        Button(save.title, action: save.action)
                    }
                    Button("Done") {
                        Self.hideKeyboard()
                    }
                    .fontWeight(.semibold)
                }
            }
    }

    /// Resigns whatever text field is first responder, without needing a `@FocusState` per field.
    static func hideKeyboard() {
        UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
    }
}

extension View {
    func keyboardDismissToolbar(next: (() -> Void)? = nil, save: (title: String, action: () -> Void)? = nil) -> some View {
        modifier(KeyboardDismissModifier(next: next, save: save))
    }
}
