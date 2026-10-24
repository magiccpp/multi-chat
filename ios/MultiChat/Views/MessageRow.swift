import MultiChatKit
import SwiftUI
import UIKit

func markdown(_ text: String) -> AttributedString {
    // Models put <br> inside table cells; inline Markdown keeps line breaks.
    let cleaned = text.replacingOccurrences(of: "<br>", with: "\n").replacingOccurrences(of: "<br/>", with: "\n")
    let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    return (try? AttributedString(markdown: cleaned, options: options)) ?? AttributedString(cleaned)
}

struct MessageRow: View {
    let model: SessionModel
    let message: Message
    @State private var expanded = false

    init(model: SessionModel, message: Message) {
        self.model = model
        self.message = message
    }

    private var state: SessionState { model.state }

    var body: some View {
        let color = message.isReply ? Color(hex: state.color(message.modelId)) : Color.secondary
        VStack(alignment: .leading, spacing: 6) {
            header(color: color)
            if !message.content.isEmpty {
                Text(message.isReply ? markdown(message.content) : AttributedString(message.content))
                    .textSelection(.enabled)
                    .font(.callout)
            } else if message.status == "streaming" && (message.toolCalls ?? []).isEmpty {
                Text("thinking…").italic().foregroundStyle(.secondary)
            }
            ForEach(message.toolCalls ?? [], id: \.id) { call in
                ToolCallView(call: call, result: state.results[call.id]?.content ?? state.progress[call.id], expanded: expanded)
            }
            if (message.toolCalls ?? []).contains(where: { (state.results[$0.id]?.content.split(separator: "\n").count ?? 0) > 6 }) {
                Button(expanded ? "Show less" : "Show full output") { expanded.toggle() }.font(.caption)
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(message.isReply ? 0.08 : 0.04), in: RoundedRectangle(cornerRadius: 10))
        .overlay(alignment: .leading) {
            UnevenRoundedRectangle(topLeadingRadius: 10, bottomLeadingRadius: 10).fill(color).frame(width: 4)
        }
    }

    @ViewBuilder
    private func header(color: Color) -> some View {
        HStack(spacing: 6) {
            Text("#\(message.seq)").font(.caption).foregroundStyle(.secondary)
            if message.isReply {
                Text("● \(state.name(message.modelId))").font(.subheadline.bold()).foregroundStyle(color)
                if message.phase == "build" { PhaseBadge(build: true) }
            } else {
                Text("You").font(.subheadline.bold())
                if let r = message.recipients, r.count < state.participants.count {
                    Text("→ " + r.map { state.name($0) }.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                }
            }
            if message.status == "streaming" { ProgressView().controlSize(.mini) }
            if message.status == "error" || message.status == "cancelled" {
                Text(message.status).font(.caption).foregroundStyle(.red)
            }
            Spacer()
            if message.isReply {
                Button { model.vote(message, 1) } label: {
                    Image(systemName: message.vote == 1 ? "hand.thumbsup.fill" : "hand.thumbsup")
                }
                .tint(.green)
                Button { model.vote(message, -1) } label: {
                    Image(systemName: message.vote == -1 ? "hand.thumbsdown.fill" : "hand.thumbsdown")
                }
                .tint(.red)
                Menu {
                    if !model.isBuild, let id = message.modelId, message.status == "complete" {
                        Button { model.pickBuilder(id) } label: { Label("Pick as builder", systemImage: "star") }
                    }
                    Button { UIPasteboard.general.string = message.content } label: { Label("Copy", systemImage: "doc.on.doc") }
                } label: {
                    Image(systemName: "ellipsis")
                }
            }
        }
        .buttonStyle(.borderless)
        .font(.subheadline)
    }
}

struct ToolCallView: View {
    let call: ToolCall
    let result: String?
    let expanded: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 4) {
                Image(systemName: "gearshape")
                Text(call.name).bold()
                Text(call.arguments?.argumentSummary ?? "").lineLimit(1)
                if call.external == true { Text("(agent CLI)").foregroundStyle(.secondary) }
            }
            .foregroundStyle(.teal)
            if let result, !result.isEmpty {
                let lines = result.split(separator: "\n", omittingEmptySubsequences: false)
                Text((expanded ? lines : Array(lines.prefix(6))).joined(separator: "\n"))
                    .foregroundStyle(.secondary)
                    .padding(.leading, 18)
            }
        }
        .font(.caption.monospaced())
    }
}

/// Inline card for a pending approval; edits open the diff sheet.
struct ApprovalCard: View {
    let model: SessionModel
    let approval: Approval
    let review: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Label(title, systemImage: approval.kind == "command" ? "terminal" : "pencil.and.outline").font(.subheadline.bold())
            if let command = approval.command {
                Text(command).font(.caption.monospaced()).padding(6)
                    .frame(maxWidth: .infinity, alignment: .leading)
                    .background(Color.secondary.opacity(0.12), in: RoundedRectangle(cornerRadius: 6))
            }
            HStack {
                if approval.kind == "edit" {
                    Button("Review diff", action: review).buttonStyle(.bordered)
                }
                Spacer()
                Button("Reject", role: .destructive) { model.approve(approval, false) }.buttonStyle(.bordered)
                Button("Approve") { model.approve(approval, true) }.buttonStyle(.borderedProminent)
            }
        }
        .padding(10)
        .overlay(RoundedRectangle(cornerRadius: 10).stroke(Color.orange, lineWidth: 2))
    }

    private var title: String {
        let who = model.state.name(approval.modelId)
        switch approval.kind {
        case "edit": return "\(who) wants to edit \(approval.path ?? "a file")"
        case "command": return "\(who) wants to run a command"
        default: return "\(who) wants to use a tool"
        }
    }
}

struct DiffSheet: View {
    let model: SessionModel
    let approval: Approval
    @Environment(\.dismiss) private var dismiss

    init(model: SessionModel, approval: Approval) {
        self.model = model
        self.approval = approval
    }

    var body: some View {
        NavigationStack {
            ScrollView([.vertical, .horizontal]) {
                VStack(alignment: .leading, spacing: 0) {
                    ForEach(Array((approval.diff ?? "").split(separator: "\n", omittingEmptySubsequences: false).enumerated()), id: \.offset) { _, line in
                        Text(line.isEmpty ? " " : String(line))
                            .font(.caption.monospaced())
                            .foregroundStyle(color(for: line))
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .background(background(for: line))
                    }
                }
                .padding(8)
            }
            .navigationTitle(approval.path ?? "Diff")
            .navigationBarTitleDisplayMode(.inline)
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Close") { dismiss() } }
                ToolbarItemGroup(placement: .bottomBar) {
                    Button("Reject", role: .destructive) { model.approve(approval, false); dismiss() }
                    Spacer()
                    Button("Approve") { model.approve(approval, true); dismiss() }.bold()
                }
            }
        }
    }

    private func color(for line: Substring) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") { return .secondary }
        if line.hasPrefix("@@") { return .teal }
        if line.hasPrefix("+") { return .green }
        if line.hasPrefix("-") { return .red }
        return .primary
    }

    private func background(for line: Substring) -> Color {
        if line.hasPrefix("+") && !line.hasPrefix("+++") { return .green.opacity(0.12) }
        if line.hasPrefix("-") && !line.hasPrefix("---") { return .red.opacity(0.12) }
        return .clear
    }
}
