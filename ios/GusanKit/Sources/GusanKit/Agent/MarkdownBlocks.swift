import Foundation

/// A tiny block-level markdown splitter. SwiftUI's `AttributedString(markdown:)` handles inline
/// styles but flattens blocks, so the app renders these blocks one by one; the keyboard uses
/// `plainText` when inserting a result at the cursor.
public enum MarkdownBlock: Equatable {
    case heading(level: Int, text: String)
    case paragraph(String)
    case bullets([String])
    case numbered([String])
    case code(String)
    case quote(String)
    case rule
}

public enum MarkdownBlocks {
    public static func parse(_ markdown: String) -> [MarkdownBlock] {
        var blocks: [MarkdownBlock] = []
        var paragraph: [String] = []
        var bullets: [String] = []
        var numbered: [String] = []
        var quote: [String] = []
        var code: [String]? = nil

        func flush() {
            if !paragraph.isEmpty { blocks.append(.paragraph(paragraph.joined(separator: " "))); paragraph = [] }
            if !bullets.isEmpty { blocks.append(.bullets(bullets)); bullets = [] }
            if !numbered.isEmpty { blocks.append(.numbered(numbered)); numbered = [] }
            if !quote.isEmpty { blocks.append(.quote(quote.joined(separator: " "))); quote = [] }
        }

        for rawLine in markdown.replacingOccurrences(of: "\r\n", with: "\n").components(separatedBy: "\n") {
            let line = rawLine.trimmingCharacters(in: .whitespaces)
            if let open = code {
                if line.hasPrefix("```") { blocks.append(.code(open.joined(separator: "\n"))); code = nil } else { code!.append(rawLine) }
                continue
            }
            if line.hasPrefix("```") { flush(); code = []; continue }
            if line.isEmpty { flush(); continue }
            if line.range(of: #"^(-{3,}|\*{3,}|_{3,})$"#, options: .regularExpression) != nil { flush(); blocks.append(.rule); continue }
            if let match = line.range(of: #"^#{1,6}\s+"#, options: .regularExpression) {
                flush()
                let level = line[match].filter { $0 == "#" }.count
                blocks.append(.heading(level: level, text: String(line[match.upperBound...]).trimmingCharacters(in: .whitespaces)))
                continue
            }
            if let match = line.range(of: #"^[-*+•]\s+"#, options: .regularExpression) {
                if !paragraph.isEmpty || !numbered.isEmpty || !quote.isEmpty { flush() }
                bullets.append(String(line[match.upperBound...]))
                continue
            }
            if let match = line.range(of: #"^\d+[.)]\s+"#, options: .regularExpression) {
                if !paragraph.isEmpty || !bullets.isEmpty || !quote.isEmpty { flush() }
                numbered.append(String(line[match.upperBound...]))
                continue
            }
            if line.hasPrefix(">") {
                if !paragraph.isEmpty || !bullets.isEmpty || !numbered.isEmpty { flush() }
                quote.append(line.dropFirst().trimmingCharacters(in: .whitespaces))
                continue
            }
            if !bullets.isEmpty || !numbered.isEmpty || !quote.isEmpty { flush() }
            paragraph.append(line)
        }
        if let open = code { blocks.append(.code(open.joined(separator: "\n"))) }
        flush()
        return blocks
    }

    /// Markdown with the inline syntax stripped, for pasting into any text field.
    public static func plainText(_ markdown: String) -> String {
        var lines: [String] = []
        for block in parse(markdown) {
            switch block {
            case .heading(_, let text): lines.append(stripInline(text))
            case .paragraph(let text): lines.append(stripInline(text))
            case .bullets(let items): lines.append(items.map { "• " + stripInline($0) }.joined(separator: "\n"))
            case .numbered(let items): lines.append(items.enumerated().map { "\($0.offset + 1). " + stripInline($0.element) }.joined(separator: "\n"))
            case .code(let text): lines.append(text)
            case .quote(let text): lines.append(stripInline(text))
            case .rule: continue
            }
        }
        return lines.joined(separator: "\n\n")
    }

    public static func firstLine(_ markdown: String) -> String {
        let text = plainText(markdown)
        let first = text.split(whereSeparator: \.isNewline).first.map(String.init) ?? text
        return first.count > 140 ? String(first.prefix(137)) + "…" : first
    }

    public static func stripInline(_ text: String) -> String {
        var out = text
        for (pattern, template) in [
            (#"!\[([^\]]*)\]\([^)]*\)"#, "$1"),
            (#"\[([^\]]+)\]\(([^)\s]+)[^)]*\)"#, "$1 ($2)"),
            (#"(\*\*|__)(.+?)\1"#, "$2"),
            (#"(?<![\w*])(\*|_)(?=\S)(.+?)(?<=\S)\1(?![\w*])"#, "$2"),
            (#"`([^`]*)`"#, "$1"),
            (#"~~(.+?)~~"#, "$1"),
        ] {
            out = out.replacingOccurrences(of: pattern, with: template, options: .regularExpression)
        }
        return out
    }
}
