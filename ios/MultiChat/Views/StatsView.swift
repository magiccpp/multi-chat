import Charts
import MultiChatKit
import SwiftUI

/// Price and performance per model, across all sessions on the server.
struct StatsView: View {
    @Environment(AppModel.self) private var app
    @State private var stats: Stats?
    @State private var days = 0  // 0 = all time
    @State private var error: String?

    init() {}

    var body: some View {
        List {
            Picker("Period", selection: $days) {
                Text("All").tag(0)
                Text("30 days").tag(30)
                Text("7 days").tag(7)
                Text("Today").tag(1)
            }
            .pickerStyle(.segmented)
            .listRowBackground(Color.clear)

            if let stats {
                Section {
                    HStack {
                        tile(money(stats.totals.cost), "Total cost")
                        tile(tokenCount(stats.totals.inputTokens + stats.totals.outputTokens), "Tokens")
                        tile("\(stats.totals.upvotes)/\(stats.totals.downvotes)", "▲ / ▼")
                    }
                }
                Section("Cost per model") {
                    Chart(stats.models) { r in
                        BarMark(x: .value("Cost", r.cost), y: .value("Model", r.displayName))
                            .foregroundStyle(Color(hex: r.color))
                            .annotation(position: .trailing) { Text(money(r.cost)).font(.caption2) }
                    }
                    .chartXAxis(.hidden)
                    .frame(height: CGFloat(max(stats.models.count, 1)) * 34 + 20)
                }
                Section("Win rate (picked as builder)") {
                    Chart(stats.models.filter { $0.winRate != nil }) { r in
                        BarMark(x: .value("Win rate", (r.winRate ?? 0) * 100), y: .value("Model", r.displayName))
                            .foregroundStyle(Color(hex: r.color))
                            .annotation(position: .trailing) { Text("\(Int((r.winRate ?? 0) * 100))%").font(.caption2) }
                    }
                    .chartXScale(domain: 0...100)
                    .chartXAxis(.hidden)
                    .frame(height: CGFloat(max(stats.models.count, 1)) * 34 + 20)
                }
                Section("Models") {
                    ForEach(stats.models) { r in StatRowView(row: r) }
                }
            } else if let error {
                ContentUnavailableView("No statistics", systemImage: "chart.bar", description: Text(error))
            } else {
                ProgressView()
            }
        }
        .navigationTitle("Statistics")
        .refreshable { await load() }
        .task(id: days) { await load() }
    }

    private func tile(_ value: String, _ label: String) -> some View {
        VStack {
            Text(value).font(.title3.bold().monospacedDigit())
            Text(label).font(.caption).foregroundStyle(.secondary)
        }
        .frame(maxWidth: .infinity)
    }

    private func load() async {
        guard let api = app.api else { return }
        var from: String?
        if days > 0 {
            from = ISO8601DateFormatter().string(from: Date().addingTimeInterval(-Double(days) * 86400))
        }
        do {
            stats = try await api.stats(from: from)
            error = nil
        } catch {
            self.error = error.localizedDescription
        }
    }
}

struct StatRowView: View {
    let row: StatRow

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Circle().fill(Color(hex: row.color)).frame(width: 9, height: 9)
                Text(row.displayName).bold()
                Text(row.endpoint).font(.caption).foregroundStyle(.secondary)
                Spacer()
                Text(money(row.cost)).monospacedDigit()
            }
            Grid(alignment: .leading, horizontalSpacing: 14, verticalSpacing: 2) {
                GridRow {
                    metric("tokens", (row.estimated ? "~" : "") + tokenCount(row.inputTokens + row.outputTokens))
                    metric("▲/▼", "\(row.upvotes)/\(row.downvotes)")
                    metric("approval", row.approval.map { "\(Int($0 * 100))%" } ?? "–")
                    metric("picks", "\(row.builderPicks)")
                }
                GridRow {
                    metric("latency", row.avgLatencyMs.map { String(format: "%.1fs", $0 / 1000) } ?? "–")
                    metric("TTFT", row.avgTtftMs.map { String(format: "%.1fs", $0 / 1000) } ?? "–")
                    metric("tok/s", row.tokensPerS.map { String(format: "%.0f", $0) } ?? "–")
                    metric("$ / ▲", row.costPerUpvote.map(money) ?? "–")
                }
            }
        }
    }

    private func metric(_ label: String, _ value: String) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            Text(value).font(.caption.monospacedDigit())
            Text(label).font(.caption2).foregroundStyle(.secondary)
        }
    }
}
