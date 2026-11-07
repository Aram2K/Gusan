import UIKit
import GusanKit

final class VoicePanel: UIVisualEffectView {
    let waveform = VoiceWaveform()
    let status = UILabel()
    let stop = UIButton(type: .system)
    let cancel = UIButton(type: .system)
    let undo = UIButton(type: .system)
    init() {
        let effect: UIVisualEffect
        if #available(iOS 26.0, *) { effect = UIGlassEffect(style: .regular) }
        else { effect = UIBlurEffect(style: .systemUltraThinMaterial) }
        super.init(effect: effect)
        isHidden = true
        tintColor = KeyboardTheme.blue
        contentView.backgroundColor = UIAccessibility.isReduceTransparencyEnabled ? .systemBackground : .clear
        status.font = .systemFont(ofSize: 20, weight: .semibold); status.numberOfLines = 3
        status.textAlignment = .center; status.textColor = .label
        stop.setTitle("Stop", for: .normal); stop.titleLabel?.font = .systemFont(ofSize: 22, weight: .semibold)
        stop.backgroundColor = KeyboardTheme.blue; stop.setTitleColor(.white, for: .normal); stop.layer.cornerRadius = 32
        stop.heightAnchor.constraint(equalToConstant: 64).isActive = true
        stop.widthAnchor.constraint(greaterThanOrEqualToConstant: 140).isActive = true
        stop.accessibilityHint = "Stops recording, or cancels voice processing"
        cancel.setTitle("Cancel", for: .normal); cancel.tintColor = KeyboardTheme.blue
        cancel.heightAnchor.constraint(greaterThanOrEqualToConstant: 44).isActive = true
        undo.setTitle("↶ Undo", for: .normal); undo.accessibilityLabel = "Undo voice result"
        undo.tintColor = KeyboardTheme.blue; undo.backgroundColor = KeyboardTheme.blue.withAlphaComponent(0.12)
        undo.layer.cornerRadius = 22
        undo.heightAnchor.constraint(equalToConstant: 44).isActive = true
        undo.widthAnchor.constraint(greaterThanOrEqualToConstant: 120).isActive = true
        let controls = UIStackView(arrangedSubviews: [stop, undo, cancel]); controls.spacing = 16; controls.alignment = .center
        let stack = UIStackView(arrangedSubviews: [waveform, status, controls]); stack.axis = .vertical
        stack.spacing = 10; stack.alignment = .center; stack.translatesAutoresizingMaskIntoConstraints = false
        contentView.addSubview(stack)
        NSLayoutConstraint.activate([
            stack.centerYAnchor.constraint(equalTo: contentView.centerYAnchor),
            stack.leadingAnchor.constraint(equalTo: contentView.leadingAnchor, constant: 16),
            stack.trailingAnchor.constraint(equalTo: contentView.trailingAnchor, constant: -16),
            stack.topAnchor.constraint(greaterThanOrEqualTo: contentView.topAnchor, constant: 12),
            stack.bottomAnchor.constraint(lessThanOrEqualTo: contentView.bottomAnchor, constant: -12),
            waveform.widthAnchor.constraint(equalTo: stack.widthAnchor), waveform.heightAnchor.constraint(equalToConstant: 60),
            status.widthAnchor.constraint(equalTo: stack.widthAnchor)
        ])
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }
    func update(phase: VoicePhase, message: String, level: Float, hasUndo: Bool) {
        status.text = message
        let busy = [.recording, .transcribing, .thinking].contains(phase)
        stop.isHidden = !busy; undo.isHidden = !hasUndo
        cancel.setTitle(busy ? "Cancel" : "Keyboard", for: .normal)
        waveform.update(level: level, animated: busy)
    }
}
