import AppKit
import ApplicationServices

// Accessibility exposes only notification UI that macOS actually displays.
// Hidden previews, Focus-suppressed alerts and app-internal events are unavailable.
final class NotificationAnnouncer: NSObject, NSSpeechSynthesizerDelegate {
    private let speech = NSSpeechSynthesizer()
    private var timer: Timer?
    private var seen = [String: Date]()
    private var primed = false
    private var queue = [String]()
    private let enabledKey = "announceVisibleNotifications"
    var controlDirectory: URL?

    var enabled: Bool { UserDefaults.standard.bool(forKey: enabledKey) }
    var trusted: Bool { AXIsProcessTrusted() }

    override init() {
        super.init()
        speech.delegate = self
    }

    func setEnabled(_ value: Bool) {
        UserDefaults.standard.set(value, forKey: enabledKey)
        stop()
        if value {
            guard trusted else {
                // The choice is saved; after granting Accessibility, the user
                // can restart Jarvis without repeating the setup.
                _ = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary)
                return
            }
            start()
        }
    }

    func start() {
        guard enabled, trusted, timer == nil else { return }
        primed = false
        seen.removeAll()
        timer = Timer.scheduledTimer(withTimeInterval: 0.6, repeats: true) { [weak self] _ in
            self?.scan()
        }
        scan()
    }

    func stop() {
        timer?.invalidate()
        timer = nil
        queue.removeAll()
        speech.stopSpeaking()
        removeSpeakingMarker()
    }

    private func attribute(_ element: AXUIElement, _ name: String) -> CFTypeRef? {
        var result: CFTypeRef?
        return AXUIElementCopyAttributeValue(element, name as CFString, &result) == .success ? result : nil
    }

    private func children(_ element: AXUIElement, _ name: String) -> [AXUIElement] {
        (attribute(element, name) as? [AXUIElement]) ?? []
    }

    private func visibleBanner(_ element: AXUIElement) -> Bool {
        guard let positionValue = attribute(element, kAXPositionAttribute as String),
              let sizeValue = attribute(element, kAXSizeAttribute as String),
              CFGetTypeID(positionValue) == AXValueGetTypeID(),
              CFGetTypeID(sizeValue) == AXValueGetTypeID() else { return false }
        let position = unsafeBitCast(positionValue, to: AXValue.self)
        let size = unsafeBitCast(sizeValue, to: AXValue.self)
        var point = CGPoint.zero
        var bounds = CGSize.zero
        guard AXValueGetValue(position, .cgPoint, &point),
              AXValueGetValue(size, .cgSize, &bounds) else { return false }
        // Check each screen independently. Notification Center's large panel
        // and the Dock are not transient top-right banners.
        return NSScreen.screens.contains { screen in
            let frame = screen.frame
            return point.x >= frame.minX + frame.width * 0.55 &&
                point.x < frame.maxX && point.y >= frame.minY &&
                point.y < frame.minY + frame.height * 0.30 &&
                bounds.width >= 180 && bounds.width < frame.width * 0.55 &&
                bounds.height >= 35 && bounds.height < frame.height * 0.30
        }
    }

    private func text(in element: AXUIElement, depth: Int = 0) -> [String] {
        guard depth < 7 else { return [] }
        let role = attribute(element, kAXRoleAttribute as String) as? String ?? ""
        var lines = [String]()
        if role == (kAXStaticTextRole as String),
           let value = attribute(element, kAXValueAttribute as String) as? String {
            let clean = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !clean.isEmpty { lines.append(clean) }
        }
        for child in children(element, kAXChildrenAttribute as String).prefix(25) {
            lines.append(contentsOf: text(in: child, depth: depth + 1))
        }
        return lines
    }

    private func scan() {
        guard enabled, trusted else { return }
        let now = Date()
        seen = seen.filter { now.timeIntervalSince($0.value) < 120 }
        var current = [String]()
        for app in NSWorkspace.shared.runningApplications where
            (app.bundleIdentifier?.lowercased().contains("notificationcenter") == true ||
             app.localizedName == "NotificationCenter") {
            let root = AXUIElementCreateApplication(app.processIdentifier)
            for window in children(root, kAXWindowsAttribute as String) where visibleBanner(window) {
                // Preserve order: app name, sender/title, message body.
                var lines = [String]()
                for line in text(in: window) where !lines.contains(line) { lines.append(line) }
                guard !lines.isEmpty else { continue }
                current.append(lines.prefix(5).joined(separator: ". "))
            }
        }
        // Existing banners at launch are not new notifications.
        if !primed {
            for message in current { seen[message] = now }
            primed = true
            return
        }
        for message in current where seen[message] == nil {
            seen[message] = now
            queue.append(message)
        }
        speakNext()
    }

    private func speakNext() {
        guard !speech.isSpeaking, !queue.isEmpty else { return }
        if let directory = controlDirectory,
           let status = try? String(contentsOf: directory.appendingPathComponent("status"), encoding: .utf8),
           status == "speaking" { return }
        let message = queue.removeFirst()
        if let directory = controlDirectory {
            try? "1".write(to: directory.appendingPathComponent("announcing"),
                           atomically: true, encoding: .utf8)
        }
        speech.startSpeaking("Notification. \(message)")
    }

    func speechSynthesizer(_ sender: NSSpeechSynthesizer, didFinishSpeaking finishedSpeaking: Bool) {
        // Leave a brief tail while the speakers finish emitting the last samples.
        Timer.scheduledTimer(withTimeInterval: 0.6, repeats: false) { [weak self] _ in
            self?.removeSpeakingMarker()
            self?.speakNext()
        }
    }

    private func removeSpeakingMarker() {
        guard let directory = controlDirectory else { return }
        try? FileManager.default.removeItem(at: directory.appendingPathComponent("announcing"))
    }
}
