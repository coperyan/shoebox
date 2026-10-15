import SwiftUI
import UIKit

/// A "Done" button above the keyboard plus dismiss-on-scroll, for screens whose fields use the
/// number / decimal pads (which have no return key) or where the return key should close the
/// keyboard instead of leaving it up.
struct KeyboardDismissModifier: ViewModifier {
    func body(content: Content) -> some View {
        content
            .scrollDismissesKeyboard(.interactively)
            .toolbar {
                ToolbarItemGroup(placement: .keyboard) {
                    Spacer()
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
    func keyboardDismissToolbar() -> some View {
        modifier(KeyboardDismissModifier())
    }
}
