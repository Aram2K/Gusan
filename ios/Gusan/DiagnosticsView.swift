import SwiftUI
import GusanKit

private struct DiagnosticExport: Encodable {
    let app: SessionReport
    let keyboard: SessionReport?
}

struct DiagnosticsView: View {
    @EnvironmentObject private var model: AppModel
    @State private var copied = false
    var body: some View {
        ScreenContent {
            AppCard {
                Text("Connection report").font(.title2).bold()
                Text("This report contains connection checks, never your tokens or typed text.").font(.body).foregroundStyle(.secondary)
                Button { model.refreshDiagnostics(); copied = false } label: { Label("Refresh report", systemImage: "arrow.clockwise") }.frame(minHeight: 44)
                Button { copyReport() } label: { Label(copied ? "Report copied" : "Copy report", systemImage: copied ? "checkmark" : "doc.on.doc") }.frame(minHeight: 44).disabled(model.diagnostics == nil)
            }
            if let diagnostics = model.diagnostics {
                groupCard("App", report: diagnostics.local)
                if let peer = diagnostics.peer { groupCard("Keyboard", report: peer) }
                else { AppCard { Text("Keyboard: no heartbeat yet").font(.headline); Text("Open the Gusan keyboard, then refresh this report.").font(.body).foregroundStyle(.secondary) } }
                ForEach(diagnostics.local.stores, id: \.name) { store in
                    AppCard {
                        Text(store.name).font(.headline)
                        healthRow("App", health: store)
                        Divider()
                        healthRow("Keyboard", health: diagnostics.peer?.stores.first { $0.name == store.name })
                    }
                }
                AppCard {
                    Text("Session location").font(.headline)
                    Text("App: " + location(diagnostics.local)).font(.body)
                    Text("Keyboard: " + (diagnostics.peer.map(location) ?? "Not checked")).font(.body)
                }
            } else { AppCard { Text("No report yet").font(.headline); Button("Try again") { model.refreshDiagnostics() }.frame(minHeight: 44) } }
        }.navigationTitle("Diagnostics").navigationBarTitleDisplayMode(.inline).onAppear { model.refreshDiagnostics() }
    }
    private func groupCard(_ title: String, report: SessionReport) -> some View {
        AppCard {
            Text(title).font(.title2).bold()
            Text("Checked " + report.timestamp.formatted()).font(.footnote)
            Text("App group: " + (report.groupID ?? "None")).font(.footnote).textSelection(.enabled)
            Text("Shared keychain group: " + (report.keychainGroupID ?? "None")).font(.footnote).textSelection(.enabled)
            if report.role == .keyboard { Text("Full Access: " + (report.fullAccess.map { $0 ? "Yes" : "No" } ?? "Unknown")).font(.footnote) }
        }
    }
    private func healthRow(_ title: String, health: StoreHealth?) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(title).font(.subheadline).bold()
            if let health {
                Text("Read: \(health.readable ? "Yes" : "No") · Write: \(health.writable ? "Yes" : "No") · Session: \(health.hasSession ? "Yes" : "No")").font(.footnote)
            } else { Text("Not checked").font(.footnote).foregroundStyle(.secondary) }
        }.accessibilityElement(children: .combine)
    }
    private func location(_ report: SessionReport) -> String {
        let names = report.stores.filter(\.hasSession).map(\.name)
        return names.isEmpty ? "No session" : names.joined(separator: ", ")
    }
    private func copyReport() {
        guard let diagnostics = model.diagnostics else { return }
        let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]; encoder.dateEncodingStrategy = .iso8601
        guard let data = try? encoder.encode(DiagnosticExport(app: diagnostics.local, keyboard: diagnostics.peer)), let report = String(data: data, encoding: .utf8) else { return }
        UIPasteboard.general.string = report; copied = true
        UIAccessibility.post(notification: .announcement, argument: "Report copied")
    }
}
