import SwiftUI

/// Vault tab placeholder; the Vault feature (logins from iPhone Passwords, Face ID, audit) lands separately.
struct VaultView: View {
    var body: some View {
        List {
            Section {
                Label("Logins for the agent will live here.", systemImage: "key")
            } footer: {
                Text("Gusan Cloud types them in for the agent; the agent never sees them.")
            }
        }
        .navigationTitle("Vault")
    }
}
