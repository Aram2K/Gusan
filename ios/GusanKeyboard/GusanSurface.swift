import UIKit
import GusanKit

func keyboardFont(_ size: CGFloat, style: UIFont.TextStyle, maximum: CGFloat, weight: UIFont.Weight = .regular, traits: UITraitCollection) -> UIFont {
    UIFontMetrics(forTextStyle: style).scaledFont(for: .systemFont(ofSize: size, weight: weight), maximumPointSize: maximum, compatibleWith: traits)
}

class ToolbarButton: UIButton {
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        bounds.insetBy(dx: -max(0, (44 - bounds.width) / 2), dy: -max(0, (44 - bounds.height) / 2)).contains(point)
    }
}

final class VoiceKey: ToolbarButton {
    private let gradient = CAGradientLayer()
    private var phase = VoicePhase.off
    private var theme: KeyboardTheme?
    override init(frame: CGRect) {
        super.init(frame: frame)
        apply(KeyboardTheme(appearance: nil, traits: traitCollection))
        gradient.startPoint = CGPoint(x: 0, y: 0); gradient.endPoint = CGPoint(x: 1, y: 1)
        layer.insertSublayer(gradient, at: 0)
        tintColor = .white
        update(phase: .off)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func apply(_ theme: KeyboardTheme) {
        self.theme = theme
        let active = [.recording, .transcribing, .thinking].contains(phase)
        gradient.colors = active ? [theme.brandBlue.cgColor, KeyboardTheme.blue.cgColor] : [theme.special.cgColor, theme.special.cgColor]
        tintColor = active ? .white : theme.foreground
    }
    func update(phase: VoicePhase) {
        self.phase = phase
        if let theme { apply(theme) }
        let recording = phase == .recording
        let busy = phase == .transcribing || phase == .thinking
        setImage(UIImage(systemName: recording ? "stop.circle.fill" : busy ? "xmark.circle" : "mic.fill"), for: .normal)
        accessibilityLabel = recording ? "Stop dictation" : busy ? "Cancel dictation" : "Start dictation"
        accessibilityHint = recording ? "Stops recording and processes your voice" : busy ? "Cancels the voice request" : "Records in place while the voice session is ready. First activation opens Gusan."
    }
    override func layoutSubviews() {
        super.layoutSubviews(); gradient.frame = bounds; gradient.cornerRadius = bounds.height / 2
        if let imageView { bringSubviewToFront(imageView) }
    }
}

final class GusanToolbar: UIView {
    var contextHeight: CGFloat { max(24, ceil((context.titleLabel?.font.lineHeight ?? 16) + 8)) }
    var controlsHeight: CGFloat { max(44, ceil((message.titleLabel?.font.lineHeight ?? 16) * 2 + 8)) }
    var preferredHeight: CGFloat { contextHeight + controlsHeight }
    let logo = ToolbarButton(type: .system)
    let mic = VoiceKey(frame: .zero)
    let message = UIButton(type: .system)
    let context = UIButton(type: .system)
    let accessory = UIButton(type: .system)
    let spinner = UIActivityIndicatorView(style: .medium)
    override init(frame: CGRect) {
        super.init(frame: frame)
        logo.setImage(UIImage(named: "GusanLogo")?.withRenderingMode(.alwaysOriginal), for: .normal)
        logo.imageView?.contentMode = .scaleAspectFit; logo.accessibilityLabel = "Gusan actions"
        logo.accessibilityHint = "Tap for actions. Hold to run your default action."
        message.accessibilityHint = "Tap a status message for details"
        context.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
        context.contentHorizontalAlignment = .leading
        context.accessibilityLabel = "Text context"
        context.accessibilityHint = "Shows the full text Gusan will use"
        message.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
        message.titleLabel?.numberOfLines = 2; message.titleLabel?.lineBreakMode = .byWordWrapping
        message.contentHorizontalAlignment = .leading
        accessory.titleLabel?.font = .systemFont(ofSize: 13, weight: .semibold)
        accessory.titleLabel?.numberOfLines = 2
        accessory.titleLabel?.lineBreakMode = .byWordWrapping
        [logo, context, message, accessory, spinner, mic].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layoutSubviews() {
        super.layoutSubviews()
        context.frame = CGRect(x: 8, y: 0, width: bounds.width - 16, height: contextHeight)
        logo.frame = CGRect(x: 10, y: contextHeight + (controlsHeight - 30) / 2, width: 30, height: 30)
        mic.frame = CGRect(x: bounds.width - 44, y: contextHeight + (controlsHeight - 34) / 2, width: 34, height: 34)
        let extra: CGFloat = accessory.isHidden ? 0 : max(48, accessory.intrinsicContentSize.width + 8)
        accessory.frame = CGRect(x: mic.frame.minX - extra - 4, y: contextHeight, width: extra, height: controlsHeight)
        spinner.frame = CGRect(x: 48, y: contextHeight + (controlsHeight - 20) / 2, width: 20, height: 20)
        let start: CGFloat = spinner.isAnimating ? 74 : 50
        message.frame = CGRect(x: start, y: contextHeight, width: max(0, mic.frame.minX - start - extra - 8), height: controlsHeight)
    }
    func apply(_ theme: KeyboardTheme) {
        message.setTitleColor(theme.foreground, for: .normal)
        context.setTitleColor(theme.foreground, for: .normal)
        context.backgroundColor = theme.special.withAlphaComponent(0.4)
        context.layer.cornerRadius = 6
        accessory.tintColor = KeyboardTheme.blue
        logo.backgroundColor = theme.letter; logo.layer.cornerRadius = 15
        mic.apply(theme)
    }
    func updateTypography(_ traits: UITraitCollection) {
        context.titleLabel?.font = keyboardFont(12, style: .caption1, maximum: 20, weight: .medium, traits: traits)
        message.titleLabel?.font = keyboardFont(12, style: .caption1, maximum: 20, weight: .medium, traits: traits)
        accessory.titleLabel?.font = keyboardFont(13, style: .footnote, maximum: 20, weight: .semibold, traits: traits)
        setNeedsLayout()
    }
}

final class GusanPanel: UIView {
    var compact = false {
        didSet {
            if compact != oldValue { tileScroll.setContentOffset(.zero, animated: false); setNeedsLayout() }
        }
    }
    var onAction: ((SmartAction) -> Void)?
    var onAsk: (() -> Void)?
    var onClose: (() -> Void)?
    var onSnapshot: (() -> Void)?
    private let ask = UIButton(type: .system)
    private let close = UIButton(type: .system)
    private let preview = UIButton(type: .system)
    private var tiles: [UIButton] = []
    private let tileScroll = UIScrollView()
    private var actionFont = UIFont.systemFont(ofSize: 11, weight: .medium)
    var preferredHeight: CGFloat { max(216, 104 + 2 * ceil(actionFont.lineHeight * 2 + 28)) }
    private let actions: [SmartAction] = [.mode(.smartAuto), .rephrase, .fixGrammar, .mode(.writeEmail), .mode(.makeTodoList), .mode(.translate), .mode(.summarize)]
    override init(frame: CGRect) {
        super.init(frame: frame)
        tileScroll.showsHorizontalScrollIndicator = false
        addSubview(tileScroll)
        ask.setTitle("Ask Gusan…", for: .normal); ask.setImage(UIImage(systemName: "sparkles"), for: .normal)
        ask.contentHorizontalAlignment = .leading; ask.titleLabel?.font = .systemFont(ofSize: 16, weight: .semibold)
        ask.addTarget(self, action: #selector(askPressed), for: .touchUpInside)
        ask.accessibilityHint = "Type an instruction for the shown text"
        close.setImage(UIImage(systemName: "keyboard"), for: .normal); close.accessibilityLabel = "Return to typing"
        close.accessibilityHint = "Closes Gusan actions"
        close.addTarget(self, action: #selector(closePressed), for: .touchUpInside)
        preview.titleLabel?.font = .systemFont(ofSize: 12)
        preview.titleLabel?.numberOfLines = 2
        preview.titleLabel?.lineBreakMode = .byTruncatingTail
        preview.contentHorizontalAlignment = .leading
        preview.accessibilityLabel = "Text snapshot"
        preview.accessibilityHint = "Expands the complete text Gusan will use"
        preview.addTarget(self, action: #selector(snapshotPressed), for: .touchUpInside)
        let icons = ["sparkles", "arrow.triangle.2.circlepath", "checkmark.seal", "envelope", "checklist", "character.bubble", "text.alignleft"]
        for index in actions.indices {
            let tile = UIButton(type: .system); tile.tag = index
            let dot = UILabel(); dot.text = "•"; dot.tag = 100; dot.textColor = KeyboardTheme.blue
            dot.isAccessibilityElement = false; tile.addSubview(dot)
            tile.setImage(UIImage(systemName: icons[index]), for: .normal)
            tile.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
            tile.titleLabel?.adjustsFontSizeToFitWidth = true; tile.titleLabel?.minimumScaleFactor = 0.8
            tile.addTarget(self, action: #selector(tilePressed(_:)), for: .touchUpInside)
            tiles.append(tile); tileScroll.addSubview(tile)
        }
        [ask, close, preview].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(theme: KeyboardTheme, target: String, defaultAction: SmartAction, context: String) {
        let names = ["Smart", "Rephrase", "Fix grammar", "Email", "To-do", "Translate → " + target.uppercased(), "Summarize"]
        let font = actionFont
        for (index, tile) in tiles.enumerated() {
            let selected = actions[index] == defaultAction
            var config = UIButton.Configuration.plain()
            config.image = tile.image(for: .normal)
            config.imagePlacement = .top; config.imagePadding = 4
            config.title = names[index]
            config.baseForegroundColor = theme.dark ? .white : KeyboardTheme.blue
            config.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { attributes in
                var result = attributes; result.font = font; return result
            }
            config.contentInsets = NSDirectionalEdgeInsets(top: 4, leading: 2, bottom: 4, trailing: 2)
            config.titleLineBreakMode = .byWordWrapping
            tile.configuration = config
            tile.viewWithTag(100)?.isHidden = !selected
            (tile.viewWithTag(100) as? UILabel)?.textColor = theme.dark ? .white : KeyboardTheme.blue
            tile.accessibilityLabel = names[index] + (selected ? ", default action" : "")
            tile.accessibilityHint = "Replaces the shown context"
            tile.backgroundColor = theme.letter; tile.layer.cornerRadius = 10
            tile.tintColor = theme.dark ? .white : KeyboardTheme.blue
        }
        ask.backgroundColor = theme.letter; ask.layer.cornerRadius = 10; ask.tintColor = theme.dark ? .white : KeyboardTheme.blue
        close.tintColor = KeyboardTheme.blue
        preview.setTitleColor(theme.foreground, for: .normal)
        preview.setTitle(context.isEmpty ? "No text: Ask Gusan will write it ›" : "Using: “" + context + "” ›", for: .normal)
        preview.accessibilityValue = context.isEmpty ? "No existing text" : context
        preview.backgroundColor = theme.special; preview.layer.cornerRadius = 10; preview.clipsToBounds = true
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        let width = bounds.width
        ask.frame = CGRect(x: 8, y: 0, width: width - 64, height: 44)
        close.frame = CGRect(x: width - 48, y: 0, width: 44, height: 44)
        let tileWidth = (width - 40) / 4
        let tileHeight = max(44, (bounds.height - 104) / 2)
        tileScroll.frame = CGRect(x: 0, y: 44, width: width, height: max(0, bounds.height - (compact ? 44 : 96)))
        tileScroll.alwaysBounceHorizontal = compact
        preview.isHidden = compact
        for (index, tile) in tiles.enumerated() {
            if compact {
                let width = max(96, ceil(actionFont.pointSize * 6 + 12))
                tile.frame = CGRect(x: 8 + CGFloat(index) * (width + 8), y: 4, width: width, height: max(44, tileScroll.bounds.height - 8))
            } else {
                tile.frame = CGRect(x: 8 + CGFloat(index % 4) * (tileWidth + 8), y: CGFloat(index / 4) * (tileHeight + 8), width: tileWidth, height: tileHeight)
            }
            if let dot = tile.viewWithTag(100) { dot.frame = CGRect(x: tile.bounds.width - 13, y: 0, width: 10, height: 16); tile.bringSubviewToFront(dot) }
        }
        tileScroll.contentSize = CGSize(width: compact ? (tiles.last?.frame.maxX ?? 0) + 8 : width, height: tileScroll.bounds.height)
        preview.frame = CGRect(x: 8, y: bounds.height - 44, width: width - 16, height: 44)
    }
    @objc private func snapshotPressed() { onSnapshot?() }
    func updateTypography(_ traits: UITraitCollection) {
        actionFont = keyboardFont(11, style: .caption1, maximum: 18, weight: .medium, traits: traits)
        ask.titleLabel?.font = keyboardFont(16, style: .body, maximum: 22, weight: .semibold, traits: traits)
        preview.titleLabel?.font = keyboardFont(12, style: .caption1, maximum: 18, traits: traits)
        setNeedsLayout()
    }
    @objc private func askPressed() { onAsk?() }
    @objc private func closePressed() { onClose?() }
    @objc private func tilePressed(_ sender: UIButton) { onAction?(actions[sender.tag]) }
}

final class InstructionHeader: UIView {
    var compact = false { didSet { if compact != oldValue { scroll.isHidden = compact; setNeedsLayout() } } }
    var preferredHeight: CGFloat { compact ? 44 : 88 }
    var onSuggestion: ((String) -> Void)?
    var onRun: (() -> Void)?
    var onCancel: (() -> Void)?
    let field = UILabel()
    private let run = UIButton(type: .system)
    private let cancel = UIButton(type: .system)
    private let scroll = UIScrollView()
    private let suggestions = UIStackView()
    override init(frame: CGRect) {
        super.init(frame: frame)
        field.font = .systemFont(ofSize: 16); field.lineBreakMode = .byTruncatingHead
        field.accessibilityLabel = "Gusan instruction"
        run.setImage(UIImage(systemName: "arrow.up.circle.fill"), for: .normal); run.accessibilityLabel = "Run instruction"
        run.accessibilityHint = "Replaces the snapshotted context using this instruction"
        run.addTarget(self, action: #selector(runPressed), for: .touchUpInside)
        cancel.setImage(UIImage(systemName: "xmark"), for: .normal); cancel.accessibilityLabel = "Cancel instruction"
        cancel.accessibilityHint = "Returns to typing without changing your text"
        cancel.addTarget(self, action: #selector(cancelPressed), for: .touchUpInside)
        suggestions.spacing = 8; suggestions.translatesAutoresizingMaskIntoConstraints = false
        scroll.showsHorizontalScrollIndicator = false; scroll.addSubview(suggestions)
        scroll.alwaysBounceHorizontal = true
        NSLayoutConstraint.activate([suggestions.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor), suggestions.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor), suggestions.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor), suggestions.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor), suggestions.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor)])
        InstructionHeader.defaultSuggestions.forEach { suggestions.addArrangedSubview(makeChip($0)) }
        [field, run, cancel, scroll].forEach { addSubview($0) }
    }
    private func makeChip(_ title: String) -> UIButton {
        let chip = UIButton(type: .system); chip.setTitle(title, for: .normal)
        chip.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
        chip.accessibilityHint = "Use this instruction"
        chip.widthAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
        chip.addTarget(self, action: #selector(suggestionPressed(_:)), for: .touchUpInside)
        return chip
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    static let defaultSuggestions = ["More formal", "Shorter", "Friendlier", "Fix grammar", "Translate to Armenian"]
    private var currentSuggestions = InstructionHeader.defaultSuggestions
    func update(text: String, theme: KeyboardTheme, busy: Bool, placeholder: String = "Tell Gusan what to do…", suggestions: [String] = InstructionHeader.defaultSuggestions) {
        if suggestions != currentSuggestions {
            currentSuggestions = suggestions
            self.suggestions.arrangedSubviews.forEach { $0.removeFromSuperview() }
            suggestions.forEach { self.suggestions.addArrangedSubview(makeChip($0)) }
        }
        field.text = busy ? "Working…" : text.isEmpty ? placeholder + " │" : text + "│"
        field.accessibilityValue = busy ? "Working on: " + text : text
        field.textColor = text.isEmpty ? .secondaryLabel : theme.foreground
        run.tintColor = KeyboardTheme.blue; cancel.tintColor = theme.foreground
        run.isEnabled = !busy && !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
        cancel.accessibilityLabel = busy ? "Cancel request" : "Cancel instruction"
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        cancel.frame = CGRect(x: 0, y: 0, width: 44, height: 44)
        run.frame = CGRect(x: bounds.width - 44, y: 0, width: 44, height: 44)
        field.frame = CGRect(x: 46, y: 0, width: bounds.width - 94, height: 44)
        scroll.frame = CGRect(x: 12, y: 44, width: bounds.width - 24, height: 44)
    }
    @objc private func suggestionPressed(_ sender: UIButton) { if let title = sender.currentTitle { onSuggestion?(title) } }
    func updateTypography(_ traits: UITraitCollection) {
        field.font = keyboardFont(16, style: .body, maximum: 22, traits: traits)
        for case let chip as UIButton in suggestions.arrangedSubviews {
            chip.titleLabel?.font = keyboardFont(12, style: .caption1, maximum: 20, weight: .medium, traits: traits)
        }
        setNeedsLayout()
    }
    @objc private func runPressed() { onRun?() }
    @objc private func cancelPressed() { onCancel?() }
}
