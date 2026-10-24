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
            if !message.attachments.isEmpty {
                AttachmentsView(model: model, attachments: message.attachments)
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
            if message.isSummary {
                Label(state.name(message.modelId), systemImage: "rectangle.compress.vertical").font(.subheadline.bold())
                    .foregroundStyle(Color(hex: state.color(message.modelId)))
                Text("context compacted").font(.caption).foregroundStyle(.secondary)
            } else if message.isReply {
                Text("● \(state.name(message.modelId))").font(.subheadline.bold()).foregroundStyle(color)
                if message.phase == "build" { PhaseBadge(build: true) }
            } else {
                Text("You").font(.subheadline.bold())
                if let r = message.recipients, r.count < state.participants.count {
                    Text("→ " + r.map { state.name($0) }.joined(separator: ", ")).font(.caption).foregroundStyle(.secondary)
                }
            }
            if message.isReply, let u = message.usage {
                Text("\(u.estimated ? "~" : "")\(tokenCount(u.tokens)) tok · \(money(u.cost))")
                    .font(.caption2.monospacedDigit()).foregroundStyle(.secondary)
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

/// Attachments of a message: image thumbnails (tap to view) and document chips (tap to share / open).
struct AttachmentsView: View {
    let model: SessionModel
    let attachments: [AttachmentInfo]
    @State private var preview: PreviewItem?

    struct PreviewItem: Identifiable {
        let id: Int
        let filename: String
        let image: UIImage?
        let url: URL?
    }

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(spacing: 8) {
                ForEach(attachments) { a in
                    if a.isImage {
                        AttachmentThumbnail(model: model, attachment: a) { image in
                            preview = PreviewItem(id: a.id, filename: a.filename, image: image, url: nil)
                        }
                    } else {
                        Button { open(a) } label: {
                            Label("\(a.filename) · \(ByteCountFormatter.string(fromByteCount: Int64(a.size), countStyle: .file))",
                                  systemImage: a.kind == "document" ? "doc.text" : "shippingbox")
                                .font(.caption).lineLimit(1)
                                .padding(.horizontal, 8).padding(.vertical, 5)
                                .background(Color.secondary.opacity(0.12), in: Capsule())
                        }
                        .buttonStyle(.plain)
                    }
                }
            }
        }
        .sheet(item: $preview) { item in
            NavigationStack {
                Group {
                    if let image = item.image {
                        ScrollView([.horizontal, .vertical]) { Image(uiImage: image).resizable().scaledToFit().frame(maxWidth: .infinity) }
                    } else if let url = item.url {
                        ShareLink(item: url) { Label("Share or open \(item.filename)", systemImage: "square.and.arrow.up") }
                    }
                }
                .navigationTitle(item.filename)
                .navigationBarTitleDisplayMode(.inline)
                .toolbar {
                    ToolbarItem(placement: .confirmationAction) { Button("Done") { preview = nil } }
                    ToolbarItem(placement: .primaryAction) {
                        if let image = item.image {
                            ShareLink(item: Image(uiImage: image), preview: SharePreview(item.filename, image: Image(uiImage: image)))
                        }
                    }
                }
            }
        }
    }

    private func open(_ a: AttachmentInfo) {
        Task {
            guard let data = await model.attachmentData(a.id) else { return }
            let url = FileManager.default.temporaryDirectory.appendingPathComponent((a.filename as NSString).lastPathComponent)
            try? data.write(to: url)
            preview = PreviewItem(id: a.id, filename: a.filename, image: nil, url: url)
        }
    }
}

struct AttachmentThumbnail: View {
    let model: SessionModel
    let attachment: AttachmentInfo
    let tapped: (UIImage) -> Void
    @State private var image: UIImage?

    var body: some View {
        Group {
            if let image {
                Image(uiImage: image).resizable().scaledToFill()
            } else {
                ZStack { Color.secondary.opacity(0.12); ProgressView() }
            }
        }
        .frame(width: 120, height: 90)
        .clipShape(RoundedRectangle(cornerRadius: 8))
        .onTapGesture { if let image { tapped(image) } }
        .task {
            if image == nil, let data = await model.attachmentData(attachment.id) { image = UIImage(data: data) }
        }
    }
}
