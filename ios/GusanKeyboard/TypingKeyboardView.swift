import UIKit

private final class KeyCap: UILabel {
    var onActivate: (() -> Void)?
    override func accessibilityActivate() -> Bool { onActivate?(); return true }
}

final class TypingKeyboardView: UIView, UIInputViewAudioFeedback {
    enum Layer { case letters, numbers, symbols }
    private enum Shift { case off, once, locked }
    private enum Key: Equatable {
        case text(String), shift, delete, numbers, symbols, letters, globe, space, enter
    }
    private struct KeySlot {
        let key: Key
        let area: CGRect
        let cap: UILabel
    }
    var onInsert: ((String) -> Void)?
    var onDelete: (() -> Void)?
    var onReturn: (() -> Void)?
    var onDoubleSpace: (() -> Void)?
    var returnTitle: String?
    var inputEnabled = true {
        didSet { if !inputEnabled { cancelTouches() } }
    }
    var keyClicks = true
    var doubleSpacePeriod = true
    var enableInputClicksWhenVisible: Bool { keyClicks }
    let globeButton = UIButton(type: .system)
    private(set) var layerMode: Layer = .letters
    private var shift: Shift = .off
    private var manualShift = false
    private var lastShiftTap: TimeInterval = 0
    private var lastSpaceTap: TimeInterval = 0
    private var before = ""
    private var capitalization: UITextAutocapitalizationType = .sentences
    private var returnType: UIReturnKeyType = .default
    private var showGlobe = false
    private var theme = KeyboardTheme(appearance: nil, traits: UITraitCollection())
    private var slots: [KeySlot] = []
    private var caps: [KeyCap] = []
    private var renderedShift: Shift?
    private var renderedDark: Bool?
    private var renderedReturnType: UIReturnKeyType?
    private var renderedReturnTitle: String?
    private var active: [UITouch: Int] = [:]
    private var previews: [UITouch: UILabel] = [:]
    private var deleteTimer: Timer?
    private var deleteStarted: TimeInterval = 0

    override init(frame: CGRect) {
        super.init(frame: frame)
        isMultipleTouchEnabled = true
        clipsToBounds = false
        globeButton.setImage(UIImage(systemName: "globe"), for: .normal)
        globeButton.accessibilityLabel = "Next keyboard"
        addSubview(globeButton)
    }
    required init?(coder: NSCoder) { fatalError("init(coder:) has not been implemented") }

    func updateContext(before: String?, capitalization: UITextAutocapitalizationType?, returnType: UIReturnKeyType?, appearance: UIKeyboardAppearance?, needsGlobe: Bool) {
        self.before = before ?? ""
        self.capitalization = capitalization ?? .sentences
        self.returnType = returnType ?? .default
        showGlobe = needsGlobe
        theme = KeyboardTheme(appearance: appearance, traits: traitCollection)
        updateAutomaticShift()
        setNeedsLayout()
    }
    private func updateAutomaticShift() {
        guard shift != .locked, !manualShift else { return }
        let shouldCapitalize: Bool
        switch capitalization {
        case .none: shouldCapitalize = false
        case .allCharacters: shouldCapitalize = true
        case .words: shouldCapitalize = before.isEmpty || before.last?.isWhitespace == true
        case .sentences:
            let trimmed = before.trimmingCharacters(in: .whitespaces)
            shouldCapitalize = trimmed.isEmpty || trimmed.last == "\n"
                || (before.last?.isWhitespace == true && trimmed.last.map { ".!?".contains($0) } == true)
        @unknown default: shouldCapitalize = false
        }
        shift = shouldCapitalize ? .once : .off
    }
    func applyTheme(_ value: KeyboardTheme) {
        theme = value
        setNeedsLayout()
    }
    private var rows: [[Key]] {
        let top: [Key]
        let middle: [Key]
        let lower: [Key]
        switch layerMode {
        case .letters:
            top = "qwertyuiop".map { .text(String($0)) }
            middle = "asdfghjkl".map { .text(String($0)) }
            lower = [.shift] + "zxcvbnm".map { .text(String($0)) } + [.delete]
        case .numbers:
            top = "1234567890".map { .text(String($0)) }
            middle = "-/:;()$&@\"".map { .text(String($0)) }
            lower = [.symbols] + ".,?!'".map { .text(String($0)) } + [.delete]
        case .symbols:
            top = "[]{}#%^*+=".map { .text(String($0)) }
            middle = "_\\|~<>€£¥•".map { .text(String($0)) }
            lower = [.numbers] + ".,?!'".map { .text(String($0)) } + [.delete]
        }
        return [top, middle, lower, [layerMode == .letters ? .numbers : .letters] + (showGlobe ? [.globe] : []) + [.space, .enter]]
    }
    override func layoutSubviews() {
        super.layoutSubviews()
        var nextSlots: [KeySlot] = []
        let appearanceChanged = renderedDark != theme.dark
        let shiftChanged = renderedShift != shift
        let returnChanged = renderedReturnType != returnType || renderedReturnTitle != returnTitle
        backgroundColor = theme.background
        let pitch = bounds.height / 4
        let unit = bounds.width / 10
        for (rowIndex, keys) in rows.enumerated() {
            let inset: CGFloat = layerMode == .letters && rowIndex == 1 ? unit / 2 : 0
            var widths = [CGFloat](repeating: (bounds.width - inset * 2) / CGFloat(keys.count), count: keys.count)
            if rowIndex == 2 {
                let side = unit * 1.3
                widths = [side] + Array(repeating: (bounds.width - side * 2) / CGFloat(keys.count - 2), count: keys.count - 2) + [side]
            } else if rowIndex == 3 {
                let side = unit * (showGlobe ? 1.25 : 1.6)
                widths = showGlobe ? [side, side, bounds.width - side * 2 - unit * 2.5, unit * 2.5]
                    : [side, bounds.width - side - unit * 2.5, unit * 2.5]
            }
            var x = inset
            for (index, key) in keys.enumerated() {
                let area = CGRect(x: x, y: CGFloat(rowIndex) * pitch, width: widths[index], height: pitch)
                let slotIndex = nextSlots.count
                if slotIndex == caps.count {
                    let cap = KeyCap()
                    cap.textAlignment = .center; cap.layer.cornerRadius = 5
                    cap.layer.shadowOffset = CGSize(width: 0, height: 1)
                    cap.layer.shadowOpacity = 1; cap.layer.shadowRadius = 0
                    cap.onActivate = { [weak self] in
                        guard let self, self.inputEnabled, self.slots.indices.contains(slotIndex) else { return }
                        let current = self.slots[slotIndex].key
                        if self.keyClicks { UIDevice.current.playInputClick() }
                        if current == .delete { self.onDelete?() } else { self.press(current) }
                    }
                    caps.append(cap); addSubview(cap)
                }
                let cap = caps[slotIndex]
                let keyChanged = !slots.indices.contains(slotIndex) || slots[slotIndex].key != key
                let shifted = key == .shift || (layerMode == .letters && !isSpecial(key) && key != .space)
                if keyChanged || appearanceChanged || (shiftChanged && shifted) || (returnChanged && key == .enter) {
                    updateCap(cap, key: key)
                }
                cap.isHidden = false; cap.frame = area.insetBy(dx: 3, dy: 6)
                nextSlots.append(KeySlot(key: key, area: area, cap: cap))
                if key == .globe { globeButton.frame = area; globeButton.tintColor = theme.foreground }
                x += widths[index]
            }
        }
        caps.dropFirst(nextSlots.count).forEach { $0.isHidden = true }
        slots = nextSlots
        renderedShift = shift; renderedDark = theme.dark
        renderedReturnType = returnType; renderedReturnTitle = returnTitle
        globeButton.isHidden = !showGlobe
        bringSubviewToFront(globeButton)
        for preview in previews.values { bringSubviewToFront(preview) }
        updateHighlights()
    }
    private func updateCap(_ cap: KeyCap, key: Key) {
        cap.layer.shadowColor = theme.shadow.cgColor
        cap.layer.backgroundColor = (isSpecial(key) ? theme.special : theme.letter).cgColor
        cap.textColor = theme.foreground
        cap.font = .systemFont(ofSize: isSpecial(key) || key == .space ? 16 : 22)
        cap.attributedText = nil; cap.text = title(key)
        cap.isAccessibilityElement = key != .globe
        cap.accessibilityLabel = accessibilityTitle(key)
        cap.accessibilityTraits = .keyboardKey
        cap.accessibilityHint = key == .shift ? "Double tap quickly to lock capitals" : nil
        if key == .shift && shift != .off {
            cap.accessibilityTraits.insert(.selected)
            cap.layer.backgroundColor = UIColor.white.cgColor; cap.textColor = .black
        }
        if key == .enter && returnType != .default { cap.layer.backgroundColor = KeyboardTheme.blue.cgColor; cap.textColor = .white }
        if key == .shift || key == .delete {
            let symbol = key == .delete ? "delete.left" : shift == .locked ? "capslock.fill" : shift == .once ? "shift.fill" : "shift"
            let attachment = NSTextAttachment()
            attachment.image = UIImage(systemName: symbol)?.withTintColor(cap.textColor, renderingMode: .alwaysOriginal)
            attachment.bounds = CGRect(x: 0, y: -3, width: 23, height: 20)
            cap.attributedText = NSAttributedString(attachment: attachment)
        }
    }
    private func isSpecial(_ key: Key) -> Bool {
        switch key { case .text, .space: return false; default: return true }
    }
    private func title(_ key: Key) -> String {
        switch key {
        case .text(let text): return layerMode == .letters && shift != .off ? text.uppercased() : text
        case .shift: return shift == .locked ? "⇪" : "⇧"
        case .delete: return "⌫"
        case .numbers: return "123"
        case .symbols: return "#+="
        case .letters: return "ABC"
        case .globe: return ""
        case .space: return "space"
        case .enter:
            if let returnTitle { return returnTitle }
            switch returnType {
            case .go: return "go"
            case .google, .search, .yahoo: return "search"
            case .send: return "send"
            case .done: return "done"
            case .next: return "next"
            case .join: return "join"
            case .route: return "route"
            case .emergencyCall: return "emergency"
            case .continue: return "continue"
            default: return "return"
            }
        }
    }
    private func accessibilityTitle(_ key: Key) -> String {
        switch key {
        case .shift: return shift == .locked ? "Caps lock" : "Shift"
        case .delete: return "Delete"
        default: return title(key)
        }
    }
    private func nearest(_ point: CGPoint) -> Int? {
        slots.indices.min {
            distance(point, slots[$0].area) < distance(point, slots[$1].area)
        }
    }
    private func distance(_ point: CGPoint, _ rect: CGRect) -> CGFloat {
        let dx = max(rect.minX - point.x, 0, point.x - rect.maxX)
        let dy = max(rect.minY - point.y, 0, point.y - rect.maxY)
        return dx * dx + dy * dy
    }
    override func touchesBegan(_ touches: Set<UITouch>, with event: UIEvent?) {
        guard inputEnabled else { return }
        for touch in touches {
            guard let index = nearest(touch.location(in: self)) else { continue }
            active[touch] = index
            if keyClicks { UIDevice.current.playInputClick() }
            if slots[index].key == .delete { onDelete?(); startDeleteRepeat() }
            showPreview(touch, index: index)
        }
        updateHighlights()
    }
    override func touchesMoved(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches where active[touch] != nil {
            guard let index = nearest(touch.location(in: self)), active[touch] != index else { continue }
            if let previous = active[touch], slots[previous].key == .delete { deleteTimer?.invalidate() }
            active[touch] = index
            if slots[index].key == .delete { onDelete?(); startDeleteRepeat() }
            showPreview(touch, index: index)
        }
        updateHighlights()
    }
    override func touchesEnded(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            guard let index = active.removeValue(forKey: touch) else { continue }
            previews.removeValue(forKey: touch)?.removeFromSuperview()
            let key = slots[index].key
            if key == .delete { deleteTimer?.invalidate() }
            else if inputEnabled { press(key) }
        }
        updateHighlights()
    }
    override func touchesCancelled(_ touches: Set<UITouch>, with event: UIEvent?) {
        for touch in touches {
            if let index = active.removeValue(forKey: touch), slots[index].key == .delete { deleteTimer?.invalidate() }
            previews.removeValue(forKey: touch)?.removeFromSuperview()
        }
        updateHighlights()
    }
    func cancelTouches() {
        deleteTimer?.invalidate(); deleteTimer = nil
        active.removeAll(); previews.values.forEach { $0.removeFromSuperview() }; previews.removeAll()
        updateHighlights()
    }
    private func updateHighlights() {
        for (index, slot) in slots.enumerated() { slot.cap.alpha = active.values.contains(index) ? 0.65 : 1 }
    }
    private func showPreview(_ touch: UITouch, index: Int) {
        previews.removeValue(forKey: touch)?.removeFromSuperview()
        guard case .text = slots[index].key, layerMode == .letters else { return }
        let cap = slots[index].cap
        let preview = UILabel(frame: CGRect(x: max(2, min(bounds.width - 58, cap.frame.midX - 28)), y: cap.frame.minY - 53, width: 56, height: 68))
        preview.backgroundColor = theme.letter; preview.textColor = theme.foreground
        preview.font = .systemFont(ofSize: 34); preview.textAlignment = .center; preview.text = cap.text
        preview.layer.cornerRadius = 9; preview.clipsToBounds = true
        preview.layer.borderColor = theme.shadow.cgColor; preview.layer.borderWidth = 0.5
        preview.isUserInteractionEnabled = false
        addSubview(preview); previews[touch] = preview
    }
    private func press(_ key: Key) {
        if key != .space { lastSpaceTap = 0 }
        if key != .shift { lastShiftTap = 0 }
        switch key {
        case .numbers, .symbols, .letters: cancelTouches()
        default: break
        }
        switch key {
        case .text(let value):
            let text = layerMode == .letters && shift != .off ? value.uppercased() : value
            if shift != .locked { shift = .off; manualShift = false }
            onInsert?(text)
        case .shift:
            let now = Date.timeIntervalSinceReferenceDate
            if now - lastShiftTap < 0.35 { shift = .locked }
            else { shift = shift == .off ? .once : .off }
            manualShift = true; lastShiftTap = now
        case .numbers: layerMode = .numbers
        case .symbols: layerMode = .symbols
        case .letters: layerMode = .letters; updateAutomaticShift()
        case .space:
            let now = Date.timeIntervalSinceReferenceDate
            if doubleSpacePeriod && now - lastSpaceTap < 0.35 && before.last == " " && before.dropLast().last.map({ $0.isLetter || $0.isNumber }) == true {
                onDoubleSpace?(); lastSpaceTap = 0
            } else { onInsert?(" "); lastSpaceTap = now }
            if layerMode != .letters { layerMode = .letters; updateAutomaticShift() }
        case .enter: onReturn?(); manualShift = false; updateAutomaticShift()
        case .delete, .globe: break
        }
        setNeedsLayout()
    }
    private func startDeleteRepeat() {
        deleteTimer?.invalidate(); deleteStarted = Date.timeIntervalSinceReferenceDate
        var ticks = 0
        deleteTimer = Timer.scheduledTimer(withTimeInterval: 0.05, repeats: true) { [weak self] _ in
            guard let self, self.inputEnabled else { return }
            let elapsed = Date.timeIntervalSinceReferenceDate - self.deleteStarted
            guard elapsed > 0.4 else { return }
            ticks += 1
            if elapsed > 1 || ticks % 2 == 0 { self.onDelete?(); if self.keyClicks { UIDevice.current.playInputClick() } }
        }
    }
}
