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

final class GusanStrip: UIView {
    let mode = ToolbarButton(type: .system)
    let screen = ToolbarButton(type: .system)
    let mic = ToolbarButton(type: .system)
    let cancel = ToolbarButton(type: .system)
    let action = UIButton(type: .system)
    let status = UIButton(type: .system)
    private let dot = UIView()
    private let waveform = VoiceWaveform()
    private let context = UILabel()
    private let spinner = UIActivityIndicatorView(style: .medium)
    private var listening = false
    private var working = false
    private var agentPill = false
    private var pendingOffer = false
    override init(frame: CGRect) {
        super.init(frame: frame)
        mode.titleLabel?.font = .systemFont(ofSize: 12, weight: .semibold)
        mode.layer.cornerRadius = 12
        screen.layer.cornerRadius = 16; mic.layer.cornerRadius = 16; cancel.layer.cornerRadius = 16
        mic.backgroundColor = UIColor(red: 10/255, green: 132/255, blue: 1, alpha: 1)
        mic.tintColor = .white; mic.setImage(UIImage(systemName: "mic.fill"), for: .normal)
        mic.accessibilityLabel = "Start voice instruction"
        screen.accessibilityLabel = "Use Gusan Screen"
        cancel.setImage(UIImage(systemName: "xmark"), for: .normal); cancel.accessibilityLabel = "Cancel voice instruction"
        status.contentHorizontalAlignment = .leading
        status.titleLabel?.font = .systemFont(ofSize: 12)
        status.titleLabel?.lineBreakMode = .byTruncatingHead
        context.font = .systemFont(ofSize: 10, weight: .medium)
        action.titleLabel?.font = .systemFont(ofSize: 12, weight: .semibold)
        dot.layer.cornerRadius = 4
        [waveform, mode, context, status, dot, spinner, screen, cancel, mic, action].forEach { addSubview($0) }
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(agent: Bool, screenOn: Bool, live: Bool, context title: String, phase: VoicePhase, level: Float, message: String?, action titleAction: String?, theme: KeyboardTheme, pendingInsert: Bool = false) {
        listening = phase == .recording; working = phase == .thinking || phase == .transcribing
        pendingOffer = pendingInsert
        agentPill = agent && !listening && !working && message != nil && titleAction == nil && !pendingInsert
        let accent = theme.dark ? UIColor(red: 77/255, green: 163/255, blue: 1, alpha: 1) : UIColor(red: 0, green: 88/255, blue: 184/255, alpha: 1)
        let indigo = UIColor(red: 94/255, green: 92/255, blue: 230/255, alpha: 1)
        let indigoText = theme.dark ? UIColor(red: 132/255, green: 130/255, blue: 1, alpha: 1) : indigo
        dot.backgroundColor = agent ? indigo : accent
        mode.setTitle(agent ? "Agent" : "Smart", for: .normal)
        mode.setTitleColor(agent ? indigoText : theme.foreground, for: .normal)
        mode.backgroundColor = agent ? indigo.withAlphaComponent(0.12) : theme.letter.withAlphaComponent(0.6)
        mode.accessibilityLabel = "Mode: " + (agent ? "Agent" : "Smart")
        mode.accessibilityHint = "Switches between Smart and Agent"
        screen.setImage(UIImage(systemName: screenOn ? "iphone" : "iphone.slash"), for: .normal)
        screen.tintColor = screenOn ? accent : .secondaryLabel
        screen.backgroundColor = screenOn ? accent.withAlphaComponent(0.16) : .clear
        screen.accessibilityValue = screenOn ? "On" : "Off"
        context.text = (live ? "● " : "") + title + (screenOn && live ? " + Screen" : "")
        context.textColor = live ? .systemRed : .secondaryLabel
        context.isHidden = title.isEmpty || working || (!listening && message != nil)
        mode.isHidden = listening || working || (message != nil && !agentPill)
        waveform.isHidden = !listening
        waveform.update(level: level, animated: listening)
        status.isHidden = listening || message == nil
        status.setTitle(message, for: .normal); status.setTitleColor(message == "Inserted" || pendingInsert ? .systemGreen : agent ? indigoText : theme.foreground, for: .normal)
        status.backgroundColor = agentPill ? indigo.withAlphaComponent(0.12) : .clear
        status.layer.cornerRadius = 12
        cancel.isHidden = !listening && !working && !pendingInsert; cancel.tintColor = theme.foreground; cancel.backgroundColor = theme.special
        cancel.accessibilityLabel = pendingInsert ? "Dismiss result" : "Cancel voice instruction"
        screen.isHidden = listening || working || titleAction != nil
        mic.isHidden = working || pendingInsert
        mic.accessibilityLabel = listening ? "Finish and send voice instruction" : "Start voice instruction"
        action.setTitle(titleAction, for: .normal); action.tintColor = pendingInsert ? .systemGreen : accent; action.isHidden = titleAction == nil || listening || working
        working ? spinner.startAnimating() : spinner.stopAnimating()
        setNeedsLayout()
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        let w = bounds.width
        dot.frame = CGRect(x: 8, y: 19, width: 8, height: 8)
        mode.frame = CGRect(x: 22, y: 10, width: 62, height: 26)
        mic.frame = CGRect(x: w - 40, y: 6.5, width: 32, height: 32)
        screen.frame = CGRect(x: w - 85, y: 6.5, width: 32, height: 32)
        cancel.frame = CGRect(x: w - (working || pendingOffer ? 40 : 85), y: 6.5, width: 32, height: 32)
        spinner.frame = CGRect(x: 22, y: 13, width: 20, height: 20)
        let actionWidth = min(125, max(44, action.intrinsicContentSize.width + 8))
        action.frame = CGRect(x: w - 48 - actionWidth, y: 1, width: actionWidth, height: 44)
        let end = working ? w - 50 : action.isHidden ? w - 96 : action.frame.minX - 4
        let start: CGFloat = working ? 48 : agentPill ? 90 : 22
        status.frame = CGRect(x: start, y: agentPill ? 9 : 0.5, width: max(0, end - start), height: agentPill ? 27 : 44)
        context.frame = CGRect(x: listening ? 22 : 90, y: listening ? 29 : 1, width: max(0, w - (listening ? 120 : 186)), height: listening ? 14 : 44)
        waveform.frame = CGRect(x: 22, y: 3, width: max(0, w - 120), height: 24)
    }
}
