import SwiftUI

struct ConnectionsView: View {
    @EnvironmentObject private var connections: CalendarConnections

    var body: some View {
        List {
            Section {
                Toggle(isOn: Binding(get: { connections.state == .connected || connections.connecting }, set: { connections.setIPhoneEnabled($0) })) {
                    HStack {
                        Label {
                            VStack(alignment: .leading, spacing: 4) {
                                Text("iPhone Calendar")
                                Text(connections.connecting ? "Connecting…" : connections.state.label)
                                    .font(.subheadline).foregroundStyle(.secondary)
                            }
                        } icon: { Image(systemName: "calendar") }
                        if connections.connecting { ProgressView() }
                    }.frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
                }.disabled(connections.connecting || connections.googleBusy || connections.state == .restricted)
                if connections.state == .denied {
                    Button { Theme.openSettings() } label: {
                        Text("Open Settings").frame(maxWidth: .infinity, minHeight: 44, alignment: .leading).contentShape(Rectangle())
                    }
                }
                if CalendarFeatures.googleEnabled {
                    Button {
                        if connections.googleState == .connected { connections.disconnectGoogle() }
                        else { connections.connectGoogle() }
                    } label: {
                        HStack {
                            Label {
                                VStack(alignment: .leading, spacing: 4) {
                                    Text("Google Calendar").foregroundStyle(.primary)
                                    Text(connections.googleState.rawValue).font(.subheadline).foregroundStyle(.secondary)
                                }
                            } icon: { Image(systemName: "calendar") }
                            Spacer()
                            if connections.googleBusy { ProgressView() }
                            else { Text(connections.googleState == .connected ? "Disconnect" : connections.googleState == .reconnect ? "Reconnect" : "Connect") }
                        }.frame(maxWidth: .infinity, minHeight: 44).contentShape(Rectangle())
                    }.disabled(connections.googleBusy)
                }
            } footer: {
                Text("Gusan reads busy times to help Smart answer availability questions and Agent plan around your schedule. It never creates or changes events.")
            }
            Section {
                Toggle("Include event titles", isOn: Binding(get: { connections.includeTitles }, set: { connections.setIncludeTitles($0) }))
                    .frame(minHeight: 44).contentShape(Rectangle()).disabled(connections.connecting || connections.googleBusy)
            } footer: { Text("Busy times only by default. Turn on event titles to share them with Smart and Agent when you ask about scheduling.") }
            if let updated = connections.lastUpdated {
                Section {
                    HStack {
                        Text("Last updated")
                        Spacer()
                        Text(updated, style: .relative).foregroundStyle(.secondary)
                        if connections.refreshing { ProgressView() }
                    }
                }
            }
            if let error = connections.error { Section { Text(error).foregroundStyle(.secondary) } }
        }
        .navigationTitle("Connections").navigationBarTitleDisplayMode(.inline)
        .task { await connections.refresh() }
    }
}
