import UIKit
import GusanKit

private final class ContextLabel: UILabel {
    override func drawText(in rect: CGRect) { super.drawText(in: rect.insetBy(dx: 8, dy: 0)) }
}

class ToolbarButton: UIButton {
    override func point(inside point: CGPoint, with event: UIEvent?) -> Bool {
        bounds.insetBy(dx: -max(0, (44 - bounds.width) / 2), dy: -max(0, (44 - bounds.height) / 2)).contains(point)
    }
}

final class VoiceKey: ToolbarButton {
    private let gradient = CAGradientLayer()
    override init(frame: CGRect) {
        super.init(frame: frame)
        apply(KeyboardTheme(appearance: nil, traits: traitCollection))
        gradient.startPoint = CGPoint(x: 0, y: 0); gradient.endPoint = CGPoint(x: 1, y: 1)
        layer.insertSublayer(gradient, at: 0)
        tintColor = .white
        update(phase: .off)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func apply(_ theme: KeyboardTheme) { gradient.colors = [theme.brandBlue.cgColor, KeyboardTheme.blue.cgColor] }
    func update(phase: VoicePhase) {
        let recording = phase == .recording
        let busy = phase == .transcribing || phase == .thinking
        setImage(UIImage(systemName: recording ? "stop.circle.fill" : busy ? "xmark.circle" : "mic.fill"), for: .normal)
        accessibilityLabel = recording ? "Stop dictation" : busy ? "Cancel dictation" : "Start dictation"
        accessibilityHint = recording ? "Stops recording and processes your voice" : busy ? "Cancels the voice request" : "Opens the Gusan app to start a voice session the first time"
    }
    override func layoutSubviews() {
        super.layoutSubviews(); gradient.frame = bounds; gradient.cornerRadius = bounds.height / 2
        if let imageView { bringSubviewToFront(imageView) }
    }
}

final class GusanToolbar: UIView {
    let logo = ToolbarButton(type: .system)
    let mic = VoiceKey(frame: .zero)
    let message = UIButton(type: .system)
    let accessory = UIButton(type: .system)
    let spinner = UIActivityIndicatorView(style: .medium)
    override init(frame: CGRect) {
        super.init(frame: frame)
        logo.setImage(UIImage(named: "GusanLogo")?.withRenderingMode(.alwaysOriginal), for: .normal)
        logo.imageView?.contentMode = .scaleAspectFit; logo.accessibilityLabel = "Gusan actions"
        logo.accessibilityHint = "Tap for actions. Hold to run your default action."
        message.accessibilityHint = "Tap a status message for details"
        message.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
        message.titleLabel?.numberOfLines = 2; message.titleLabel?.lineBreakMode = .byWordWrapping
        message.contentHorizontalAlignment = .leading
        accessory.titleLabel?.font = .systemFont(ofSize: 13, weight: .semibold)
        accessory.titleLabel?.numberOfLines = 2
        accessory.titleLabel?.lineBreakMode = .byWordWrapping
        [logo, message, accessory, spinner, mic].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    override func layoutSubviews() {
        super.layoutSubviews()
        logo.frame = CGRect(x: 10, y: 7, width: 30, height: 30)
        mic.frame = CGRect(x: bounds.width - 44, y: 5, width: 34, height: 34)
        let extra: CGFloat = accessory.isHidden ? 0 : (accessory.currentTitle == "How to enable" ? 88 : 56)
        accessory.frame = CGRect(x: mic.frame.minX - extra - 4, y: 0, width: extra, height: 44)
        spinner.frame = CGRect(x: 48, y: 12, width: 20, height: 20)
        let start: CGFloat = spinner.isAnimating ? 74 : 50
        message.frame = CGRect(x: start, y: 0, width: max(0, mic.frame.minX - start - extra - 8), height: 44)
    }
    func apply(_ theme: KeyboardTheme) {
        message.setTitleColor(theme.foreground, for: .normal)
        accessory.tintColor = KeyboardTheme.blue
        logo.backgroundColor = theme.letter; logo.layer.cornerRadius = 15
        mic.apply(theme)
    }
}

final class GusanPanel: UIView {
    var onAction: ((SmartAction) -> Void)?
    var onAsk: (() -> Void)?
    var onClose: (() -> Void)?
    private let ask = UIButton(type: .system)
    private let close = UIButton(type: .system)
    private let preview = ContextLabel()
    private var tiles: [UIButton] = []
    private let actions: [SmartAction] = [.mode(.smartAuto), .rephrase, .fixGrammar, .mode(.writeEmail), .mode(.makeTodoList), .mode(.translate), .mode(.summarize)]
    override init(frame: CGRect) {
        super.init(frame: frame)
        ask.setTitle("Ask Gusan…", for: .normal); ask.setImage(UIImage(systemName: "sparkles"), for: .normal)
        ask.contentHorizontalAlignment = .leading; ask.titleLabel?.font = .systemFont(ofSize: 16, weight: .semibold)
        ask.addTarget(self, action: #selector(askPressed), for: .touchUpInside)
        ask.accessibilityHint = "Type an instruction for the shown text"
        close.setImage(UIImage(systemName: "keyboard"), for: .normal); close.accessibilityLabel = "Return to typing"
        close.accessibilityHint = "Closes Gusan actions"
        close.addTarget(self, action: #selector(closePressed), for: .touchUpInside)
        preview.font = .systemFont(ofSize: 11); preview.numberOfLines = 2; preview.lineBreakMode = .byTruncatingTail
        let icons = ["sparkles", "arrow.triangle.2.circlepath", "checkmark.seal", "envelope", "checklist", "character.bubble", "text.alignleft"]
        for index in actions.indices {
            let tile = UIButton(type: .system); tile.tag = index
            let dot = UILabel(); dot.text = "•"; dot.tag = 100; dot.textColor = KeyboardTheme.blue
            dot.isAccessibilityElement = false; tile.addSubview(dot)
            tile.setImage(UIImage(systemName: icons[index]), for: .normal)
            tile.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
            tile.titleLabel?.adjustsFontSizeToFitWidth = true; tile.titleLabel?.minimumScaleFactor = 0.8
            tile.addTarget(self, action: #selector(tilePressed(_:)), for: .touchUpInside)
            tiles.append(tile); addSubview(tile)
        }
        [ask, close, preview].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(theme: KeyboardTheme, target: String, defaultAction: SmartAction, context: String) {
        let names = ["Smart", "Rephrase", "Fix grammar", "Email", "To-do", "Translate → " + target.uppercased(), "Summarize"]
        for (index, tile) in tiles.enumerated() {
            let selected = actions[index] == defaultAction
            var config = UIButton.Configuration.plain()
            config.image = tile.image(for: .normal)
            config.imagePlacement = .top; config.imagePadding = 4
            config.title = names[index]
            config.baseForegroundColor = theme.dark ? .white : KeyboardTheme.blue
            config.titleTextAttributesTransformer = UIConfigurationTextAttributesTransformer { attributes in
                var result = attributes; result.font = .systemFont(ofSize: 11, weight: .medium); return result
            }
            config.contentInsets = NSDirectionalEdgeInsets(top: 4, leading: 2, bottom: 4, trailing: 2)
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
        preview.textColor = theme.foreground; preview.text = context.isEmpty ? "No text: Ask Gusan will write it" : "Using: “" + context + "”"
        preview.backgroundColor = theme.special; preview.layer.cornerRadius = 10; preview.clipsToBounds = true
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        let width = bounds.width
        ask.frame = CGRect(x: 8, y: 0, width: width - 64, height: 38)
        close.frame = CGRect(x: width - 48, y: 0, width: 40, height: 38)
        let tileWidth = (width - 40) / 4
        let tileHeight = max(44, (bounds.height - 92) / 2)
        for (index, tile) in tiles.enumerated() {
            tile.frame = CGRect(x: 8 + CGFloat(index % 4) * (tileWidth + 8), y: 44 + CGFloat(index / 4) * (tileHeight + 8), width: tileWidth, height: tileHeight)
            if let dot = tile.viewWithTag(100) { dot.frame = CGRect(x: tileWidth - 13, y: 0, width: 10, height: 16); tile.bringSubviewToFront(dot) }
        }
        preview.frame = CGRect(x: 8, y: bounds.height - 36, width: width - 16, height: 32)
    }
    @objc private func askPressed() { onAsk?() }
    @objc private func closePressed() { onClose?() }
    @objc private func tilePressed(_ sender: UIButton) { onAction?(actions[sender.tag]) }
}

final class InstructionHeader: UIView {
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
        NSLayoutConstraint.activate([suggestions.leadingAnchor.constraint(equalTo: scroll.contentLayoutGuide.leadingAnchor), suggestions.trailingAnchor.constraint(equalTo: scroll.contentLayoutGuide.trailingAnchor), suggestions.topAnchor.constraint(equalTo: scroll.contentLayoutGuide.topAnchor), suggestions.bottomAnchor.constraint(equalTo: scroll.contentLayoutGuide.bottomAnchor), suggestions.heightAnchor.constraint(equalTo: scroll.frameLayoutGuide.heightAnchor)])
        for title in ["More formal", "Shorter", "Friendlier", "Fix grammar", "Translate to Armenian"] {
            let chip = UIButton(type: .system); chip.setTitle(title, for: .normal)
            chip.titleLabel?.font = .systemFont(ofSize: 12, weight: .medium)
            chip.accessibilityHint = "Use this instruction"
            chip.addTarget(self, action: #selector(suggestionPressed(_:)), for: .touchUpInside)
            suggestions.addArrangedSubview(chip)
        }
        [field, run, cancel, scroll].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(text: String, theme: KeyboardTheme) {
        field.text = text.isEmpty ? "Tell Gusan what to do… │" : text + "│"
        field.accessibilityValue = text
        field.textColor = text.isEmpty ? .secondaryLabel : theme.foreground
        run.tintColor = KeyboardTheme.blue; cancel.tintColor = theme.foreground
        run.isEnabled = !text.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        cancel.frame = CGRect(x: 4, y: 0, width: 34, height: 38)
        run.frame = CGRect(x: bounds.width - 44, y: 0, width: 40, height: 38)
        field.frame = CGRect(x: 42, y: 0, width: bounds.width - 90, height: 38)
        scroll.frame = CGRect(x: 12, y: 40, width: bounds.width - 24, height: 28)
    }
    @objc private func suggestionPressed(_ sender: UIButton) { if let title = sender.currentTitle { onSuggestion?(title) } }
    @objc private func runPressed() { onRun?() }
    @objc private func cancelPressed() { onCancel?() }
}
