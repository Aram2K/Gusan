import SwiftUI
import UIKit
import GusanKit

@MainActor
struct VaultAddLoginSheet: View {
    @ObservedObject var vault: VaultController
    @Environment(\.dismiss) private var dismiss
    @State private var hostname = ""
    @State private var username = ""
    @State private var password = ""
    @State private var authenticator = ""

    var body: some View {
        VStack(spacing: 0) {
            HStack {
                Button("Cancel") { clear(); dismiss() }.disabled(vault.busy)
                Spacer()
                Text("Add login").font(.headline)
                Spacer()
                Button("Save") { Task { await save() } }
                    .bold().disabled(vault.busy || hostname.isEmpty || password.isEmpty)
            }.padding(20)
            Form {
                Section("Site and login") {
                    VaultAutoFillField(title: "Site or URL", text: $hostname, contentType: .URL, keyboardType: .URL)
                    VaultAutoFillField(title: "Username or email", text: $username, contentType: .username)
                    VaultAutoFillField(title: "Password", text: $password, contentType: .password, secure: true)
                }
                Section {
                    VaultAutoFillField(title: "Authenticator secret (optional)", text: $authenticator, secure: true)
                } header: { Text("Two-factor authentication") } footer: {
                    Text("Paste a base32 secret or an otpauth:// URL. This is the setup secret, not a six-digit code.")
                }
                Section {
                    Text("Gusan imports a copy of this login into this iPhone's keychain, protected by Face ID. Changes in iPhone Passwords won't sync. The agent never sees it: when it needs this login you approve with Face ID, and it's typed into the site for the agent. Re-enrolling Face ID removes saved logins.")
                        .font(.footnote).foregroundStyle(.secondary)
                }
                if vault.busy { ProgressView("Saving securely…") }
                if let error = vault.error { Section { Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.danger) } }
            }
            .disabled(vault.busy)
        }
        .interactiveDismissDisabled(vault.busy)
        .onDisappear { clear() }
    }

    private func save() async {
        if await vault.add(hostname: hostname, username: username, password: password, authenticator: authenticator) {
            clear(); dismiss()
        }
    }
    private func clear() { hostname = ""; username = ""; password = ""; authenticator = "" }
}

struct VaultAutoFillField: UIViewRepresentable {
    let title: String
    @Binding var text: String
    var contentType: UITextContentType? = nil
    var keyboardType: UIKeyboardType = .default
    var secure = false

    func makeUIView(context: Context) -> UITextField {
        let field = UITextField()
        field.placeholder = title; field.accessibilityLabel = title
        field.textContentType = contentType; field.keyboardType = keyboardType
        field.isSecureTextEntry = secure
        field.autocapitalizationType = .none; field.autocorrectionType = .no
        field.spellCheckingType = .no
        field.font = .preferredFont(forTextStyle: .body); field.adjustsFontForContentSizeCategory = true
        field.setContentCompressionResistancePriority(.defaultLow, for: .horizontal)
        field.addTarget(context.coordinator, action: #selector(Coordinator.changed(_:)), for: .editingChanged)
        return field
    }
    func updateUIView(_ field: UITextField, context: Context) {
        context.coordinator.binding = $text
        if field.text != text { field.text = text }
    }
    func makeCoordinator() -> Coordinator { Coordinator(binding: $text) }
    final class Coordinator: NSObject {
        var binding: Binding<String>
        init(binding: Binding<String>) { self.binding = binding }
        @objc func changed(_ field: UITextField) { binding.wrappedValue = field.text ?? "" }
    }
}
