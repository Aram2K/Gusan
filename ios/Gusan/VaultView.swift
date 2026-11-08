import SwiftUI
import GusanKit

@MainActor
struct VaultView: View {
    @EnvironmentObject private var model: AppModel
    @Environment(\.scenePhase) private var scenePhase
    @StateObject private var vault = VaultController()
    @State private var adding = false
    @State private var deleting: VaultLogin?
    private var available: Bool { vault.isDemo || (model.session != nil && model.isPro && !model.settings.privacyMode) }

    init() {}

    var body: some View {
        List {
            Section {
                Label("Logins the agent never sees", systemImage: "lock.shield")
                    .font(.headline)
                Text("Always ask before using a login is always on. Each use needs your approval with Face ID on this iPhone.")
                    .font(.footnote).foregroundStyle(.secondary)
            }
            if !available {
                Section {
                    if model.session == nil {
                        Text("Sign in to use Vault with Gusan Pro.")
                        Button("Sign in") { model.handleURL(URL(string: "gusan://signin")!) }
                    } else if model.settings.privacyMode {
                        Text("Vault is unavailable in Privacy Mode. Turn it off in Activity to use saved logins.")
                    } else {
                        Text("Vault requires Gusan Pro. Manage your plan in Account.")
                    }
                }
            } else {
                if let error = vault.error {
                    Section {
                        Label(error, systemImage: "exclamationmark.triangle").foregroundStyle(Theme.danger)
                        Button("Try again") { Task { await vault.refresh() } }.disabled(vault.busy)
                    }
                }
                Section("Sign-in requests") {
                    if vault.pending.isEmpty {
                        Text("No sign-in requests. An agent will ask here when it needs a saved login.").foregroundStyle(.secondary)
                    }
                    ForEach(vault.pending) { request in
                        requestRow(request)
                    }
                }
                Section("Logins") {
                    if vault.logins.isEmpty { Text("Add a login with +. Passwords stay in this iPhone's private keychain.").foregroundStyle(.secondary) }
                    ForEach(vault.logins) { login in
                        loginRow(login)
                            .swipeActions(allowsFullSwipe: false) {
                                Button(role: .destructive) { deleting = login } label: { Label("Delete", systemImage: "trash") }
                                    .disabled(vault.busy)
                            }
                    }
                }
                Section("Recent use") {
                    if vault.events.isEmpty { Text("Your login activity will appear here.").foregroundStyle(.secondary) }
                    ForEach(vault.events) { event in
                        VStack(alignment: .leading, spacing: 4) {
                            Text(event.label)
                            Text(event.at, style: .time).font(.caption).foregroundStyle(.secondary)
                        }.accessibilityElement(children: .combine)
                    }
                }
                if vault.loading { ProgressView("Refreshing Vault…") }
            }
        }
        .listStyle(.insetGrouped)
        .navigationTitle("Vault")
        .toolbar {
            ToolbarItem(placement: .primaryAction) {
                Button { adding = true } label: { Label("Add login", systemImage: "plus") }
                    .disabled(!available || vault.busy)
            }
        }
        .refreshable { if available { await vault.refresh() } }
        .task(id: pollingKey) {
            vault.configure(model: model)
            guard available, scenePhase == .active, !adding else { return }
            while !Task.isCancelled {
                await vault.refresh()
                do { try await Task.sleep(nanoseconds: 5_000_000_000) } catch { break }
            }
        }
        .sheet(isPresented: $adding) {
            VaultAddLoginSheet(vault: vault)
        }
        .confirmationDialog("Delete this login from this iPhone and Gusan Cloud?", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
            if let login = deleting {
                Button("Delete \(login.hostname)", role: .destructive) {
                    deleting = nil
                    Task { await vault.delete(login) }
                }
            }
            Button("Cancel", role: .cancel) { deleting = nil }
        }
    }

    private var pollingKey: String {
        "\(model.session?.userID ?? model.session?.accessToken ?? "signed-out"):\(available):\(scenePhase == .active):\(adding)"
    }

    private func requestRow(_ request: VaultPendingLogin) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Label("\(request.hostname) asks you to sign in", systemImage: "person.badge.key.fill").font(.headline)
            Text(request.title).font(.subheadline)
            Text("Agent is on \(request.pageURL?.absoluteString ?? "an unknown page")")
                .font(.footnote).foregroundStyle(.secondary)
            if !request.pageMatches {
                Label("The page doesn't match this login's site. Don't share this login.", systemImage: "exclamationmark.shield.fill")
                    .font(.footnote).foregroundStyle(Theme.danger)
            }
            if request.pageMatches {
                PrimaryButton(title: "Use login with Face ID", icon: "faceid", busy: vault.busy) {
                    Task { await vault.approve(request) }
                }.disabled(request.expiresAt <= Date())
            }
            HStack {
                Button("Deny", role: .destructive) { Task { await vault.deny(request) } }
                    .frame(minHeight: 44).disabled(vault.busy)
                Spacer()
                Text("Expires \(request.expiresAt, style: .time)").font(.caption).foregroundStyle(.secondary)
            }
        }.padding(.vertical, 8)
    }

    private func loginRow(_ login: VaultLogin) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(login.hostname).font(.headline)
                if login.hasTOTP { Label("TOTP", systemImage: "number.shield").font(.caption).foregroundStyle(.secondary) }
            }
            if let hint = login.usernameHint { Text(hint).font(.subheadline).foregroundStyle(.secondary) }
            if let lastUsed = login.lastUsedAt {
                Text("Last used \(lastUsed.formatted(date: .abbreviated, time: .shortened))").font(.caption).foregroundStyle(.secondary)
            } else { Text("Not used yet").font(.caption).foregroundStyle(.secondary) }
        }.accessibilityElement(children: .combine)
    }
}
