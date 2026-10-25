import AppKit
import AVFoundation
import Foundation
import ScreenCaptureKit

/// The home folder, honouring $HOME. macOS sets it to the real home when the
/// app is opened normally; the install test points it at a temporary folder,
/// and homeDirectoryForCurrentUser alone would ignore that and write the test
/// run's log and menu-bar control files into the real ones.
private func userHome() -> URL {
    if let home = ProcessInfo.processInfo.environment["HOME"], !home.isEmpty {
        return URL(fileURLWithPath: home, isDirectory: true)
    }
    return FileManager.default.homeDirectoryForCurrentUser
}

final class JarvisLauncher: NSObject, NSApplicationDelegate, NSMenuDelegate {
    private var child: Process?
    private var statusItem: NSStatusItem?
    private var microphoneItem: NSMenuItem?
    private var statusLabel: NSMenuItem?
    private var moneyItem: NSMenuItem?
    private var repoPath: String?
    private var refreshTimer: Timer?
    private var screenTimer: DispatchSourceTimer?
    private var controlDirectory: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let directory = userHome()
            .appendingPathComponent("Library/Application Support/Jarvis/MenuControl")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            controlDirectory = directory
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("command"))
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("status"))
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("announcing"))
            // Remove screenshots and requests left by a previous app session.
            if let files = try? FileManager.default.contentsOfDirectory(at: directory,
                                                                         includingPropertiesForKeys: nil) {
                for file in files where file.lastPathComponent == "screen-request" ||
                    (file.lastPathComponent.hasPrefix("screen-") &&
                     (file.pathExtension == "png" || file.pathExtension == "error")) {
                    try? FileManager.default.removeItem(at: file)
                }
            }
        } catch {
            showError("Could not prepare menu controls: \(error.localizedDescription)")
            return
        }
        setupMenu()
        // Screen requests must continue while AppKit is handling a menu, dialog,
        // or microphone permission prompt on the main run loop.
        let timer = DispatchSource.makeTimerSource(queue: DispatchQueue(label: "com.nathan.jarvis.screen"))
        timer.schedule(deadline: .now(), repeating: .milliseconds(150))
        timer.setEventHandler { [weak self] in
            self?.handleScreenRequest()
        }
        screenTimer = timer
        timer.resume()
        AVCaptureDevice.requestAccess(for: .audio) { allowed in
            DispatchQueue.main.async {
                if allowed {
                    self.launchPython()
                } else {
                    let alert = NSAlert()
                    alert.messageText = "Jarvis needs microphone access"
                    alert.informativeText = "Enable Jarvis in System Settings → Privacy & Security → Microphone, then reopen it."
                    alert.runModal()
                    NSApp.terminate(nil)
                }
            }
        }
    }

    private func launchPython() {
        guard let url = Bundle.main.url(forResource: "launch", withExtension: "plist"),
              let settings = NSDictionary(contentsOf: url),
              let repo = settings["Repo"] as? String,
              let python = settings["Python"] as? String,
              let arch = settings["Arch"] as? String else {
            showError("Launcher settings are missing. Run the Jarvis installer again.")
            return
        }
        repoPath = repo
        let main = URL(fileURLWithPath: repo).appendingPathComponent("main.py").path
        guard FileManager.default.fileExists(atPath: main),
              FileManager.default.isExecutableFile(atPath: python) else {
            showError("Jarvis project or Python moved. Run the Jarvis installer again.")
            return
        }
        let logDir = userHome()
            .appendingPathComponent("Library/Logs/Jarvis")
        do {
            try FileManager.default.createDirectory(at: logDir, withIntermediateDirectories: true)
            let log = logDir.appendingPathComponent("launch.log")
            if !FileManager.default.fileExists(atPath: log.path) {
                FileManager.default.createFile(atPath: log.path, contents: nil)
            }
            let handle = try FileHandle(forWritingTo: log)
            handle.seekToEndOfFile()
            let process = Process()
            process.currentDirectoryURL = URL(fileURLWithPath: repo)
            process.executableURL = URL(fileURLWithPath: arch.isEmpty ? python : "/usr/bin/arch")
            process.arguments = arch.isEmpty ? [main] : ["-\(arch)", python, main]
            var environment = ProcessInfo.processInfo.environment
            environment["PATH"] = URL(fileURLWithPath: python).deletingLastPathComponent().path
                + ":/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
            environment["JARVIS_MENU_CONTROL_DIR"] = controlDirectory?.path
            process.environment = environment
            process.standardOutput = handle
            process.standardError = handle
            process.terminationHandler = { process in
                DispatchQueue.main.async {
                    handle.closeFile()
                    self.refreshMenu()
                    if process.terminationStatus != 0 {
                        self.showError("Jarvis stopped. See ~/Library/Logs/Jarvis/launch.log for details.")
                    } else {
                        NSApp.terminate(nil)
                    }
                }
            }
            child = process
            try process.run()
            refreshMenu()
        } catch {
            showError("Jarvis could not start: \(error.localizedDescription)")
        }
    }

    private func setupMenu() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.image = NSImage(systemSymbolName: "waveform.circle", accessibilityDescription: "Jarvis")
        if item.button?.image == nil { item.button?.title = "Jarvis" }
        let menu = NSMenu()
        let status = NSMenuItem(title: "Starting Jarvis…", action: nil, keyEquivalent: "")
        status.isEnabled = false
        menu.addItem(status)
        statusLabel = status
        let money = NSMenuItem(title: "💰 Checking the money…", action: #selector(openShop), keyEquivalent: "")
        menu.addItem(money)
        moneyItem = money
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Open Jarvis", action: #selector(openWindow), keyEquivalent: ""))
        let microphone = NSMenuItem(title: "Mute microphone", action: #selector(toggleMicrophone), keyEquivalent: "")
        menu.addItem(microphone)
        microphoneItem = microphone
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Restart Jarvis", action: #selector(restart), keyEquivalent: ""))
        for entry in menu.items where entry.action != nil { entry.target = self }
        menu.delegate = self
        item.menu = menu
        statusItem = item
        refreshTimer = Timer.scheduledTimer(withTimeInterval: 0.5, repeats: true) { [weak self] _ in
            self?.refreshMenu()
        }
    }

    // Capture in the signed Jarvis process so macOS checks Jarvis's screen permission,
    // rather than the separate Python interpreter that handles the voice session.
    private func handleScreenRequest() {
        guard let directory = controlDirectory else { return }
        let request = directory.appendingPathComponent("screen-request")
        guard let identifier = try? String(contentsOf: request, encoding: .utf8)
            .trimmingCharacters(in: .whitespacesAndNewlines),
              UUID(uuidString: identifier) != nil else { return }
        try? FileManager.default.removeItem(at: request)
        let response = directory.appendingPathComponent("screen-\(identifier).png")
        let errorFile = directory.appendingPathComponent("screen-\(identifier).error")
        if #available(macOS 14.0, *) {
            // ScreenCaptureKit checks its own authorization. The older Core
            // Graphics preflight can report false after an app reinstall even
            // when ScreenCaptureKit is able to capture.
            Task.detached(priority: .userInitiated) {
                do {
                    let content = try await SCShareableContent.excludingDesktopWindows(
                        false, onScreenWindowsOnly: true)
                    guard !content.displays.isEmpty else {
                        throw NSError(domain: "Jarvis", code: 1,
                                      userInfo: [NSLocalizedDescriptionKey: "No display available for capture."])
                    }
                    // Capture every display and place each where macOS arranges it, so an
                    // external monitor is seen too, not only the screen Jarvis is on.
                    let bounds = content.displays.map { CGDisplayBounds($0.displayID) }
                    let canvas = bounds.dropFirst().reduce(bounds[0]) { $0.union($1) }
                    let scale = min(1.0, 1280.0 / (bounds.map(\.width).max() ?? 1280.0))
                    let width = Int((canvas.width * scale).rounded())
                    let height = Int((canvas.height * scale).rounded())
                    guard let context = CGContext(
                        data: nil, width: width, height: height, bitsPerComponent: 8, bytesPerRow: 0,
                        space: CGColorSpaceCreateDeviceRGB(),
                        bitmapInfo: CGImageAlphaInfo.premultipliedLast.rawValue) else {
                        throw NSError(domain: "Jarvis", code: 2,
                                      userInfo: [NSLocalizedDescriptionKey: "Could not encode screenshot."])
                    }
                    context.setFillColor(CGColor(gray: 0, alpha: 1))
                    context.fill(CGRect(x: 0, y: 0, width: width, height: height))
                    for (display, rect) in zip(content.displays, bounds) {
                        let filter = SCContentFilter(display: display, excludingWindows: [])
                        let config = SCStreamConfiguration()
                        config.width = Int((rect.width * scale).rounded())
                        config.height = Int((rect.height * scale).rounded())
                        let frame = try await SCScreenshotManager.captureImage(
                            contentFilter: filter, configuration: config)
                        // Display bounds are top-left based; Core Graphics draws from the bottom left.
                        context.draw(frame, in: CGRect(x: (rect.minX - canvas.minX) * scale,
                                                       y: (canvas.maxY - rect.maxY) * scale,
                                                       width: rect.width * scale,
                                                       height: rect.height * scale))
                    }
                    guard let frame = context.makeImage(),
                          let png = NSBitmapImageRep(cgImage: frame).representation(
                        using: .png, properties: [:]) else {
                        throw NSError(domain: "Jarvis", code: 2,
                                      userInfo: [NSLocalizedDescriptionKey: "Could not encode screenshot."])
                    }
                    try png.write(to: response, options: .atomic)
                } catch {
                    NSLog("Jarvis screen capture failed: %@", error.localizedDescription)
                    try? error.localizedDescription.write(to: errorFile, atomically: true, encoding: .utf8)
                }
            }
        } else {
            // ScreenCaptureKit's still image API requires macOS 14.
            guard CGPreflightScreenCaptureAccess() else {
                try? "Jarvis needs Screen & System Audio Recording access. Quit and reopen Jarvis after granting it."
                    .write(to: errorFile, atomically: true, encoding: .utf8)
                return
            }
            let capture = Process()
            capture.executableURL = URL(fileURLWithPath: "/usr/sbin/screencapture")
            capture.arguments = ["-x", "-t", "png", response.path]
            do {
                try capture.run()
                capture.waitUntilExit()
                if capture.terminationStatus != 0 {
                    throw NSError(domain: "Jarvis", code: 3,
                                  userInfo: [NSLocalizedDescriptionKey: "Screen capture failed."])
                }
            } catch {
                try? error.localizedDescription.write(to: errorFile, atomically: true, encoding: .utf8)
            }
        }
    }

    private func refreshMenu() {
        let running = child?.isRunning == true
        let state = controlDirectory.flatMap {
            try? String(contentsOf: $0.appendingPathComponent("status"), encoding: .utf8)
        }?.trimmingCharacters(in: .whitespacesAndNewlines)
        let muted = state == "muted"
        statusLabel?.title = running ? (state == nil ? "Starting Jarvis…" :
            (muted ? "Microphone muted" : "Microphone active")) : "Jarvis stopped"
        microphoneItem?.title = muted ? "Unmute microphone" : "Mute microphone"
        microphoneItem?.isEnabled = running && state != nil
        if let button = statusItem?.button {
            button.image = NSImage(systemSymbolName: muted ? "mic.slash.circle" : "waveform.circle",
                                   accessibilityDescription: muted ? "Jarvis muted" : "Jarvis")
        }
    }

    private func sendCommand(_ command: String) {
        guard child?.isRunning == true, let directory = controlDirectory else { return }
        try? command.write(to: directory.appendingPathComponent("command"),
                           atomically: true, encoding: .utf8)
    }

    @objc private func openWindow() { sendCommand("open") }
    @objc private func toggleMicrophone() {
        sendCommand(microphoneItem?.title == "Unmute microphone" ? "unmute" : "mute")
    }
    @objc private func openShop() {
        if let url = URL(string: "https://www.etsy.com/your/shops/me/dashboard") { NSWorkspace.shared.open(url) }
    }
    /// Quits, waits for this app and its Python session to exit, then opens Jarvis again.
    @objc private func restart() {
        var pids = ["\(getpid())"]
        if let child = child, child.isRunning { pids.append("\(child.processIdentifier)") }
        let relaunch = Process()
        relaunch.executableURL = URL(fileURLWithPath: "/bin/sh")
        relaunch.arguments = ["-c", "for p in \(pids.joined(separator: " ")); do "
            + "while kill -0 $p 2>/dev/null; do sleep 0.2; done; done; /usr/bin/open \"$0\"",
            Bundle.main.bundlePath]
        try? relaunch.run()
        NSApp.terminate(nil)
    }

    func menuWillOpen(_ menu: NSMenu) { moneyItem?.title = moneyTitle() }

    /// Today's money at a glance: the memecoin bot's paper account and the shop's sales.
    private func moneyTitle() -> String {
        var parts: [String] = []
        let status = userHome().appendingPathComponent("optionsbots/data/memebot/status.json")
        if let data = try? Data(contentsOf: status),
           let bot = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
           let equity = (bot["equity"] as? NSNumber)?.doubleValue {
            let change = equity - ((bot["day_start_equity"] as? NSNumber)?.doubleValue ?? equity)
            var line = String(format: "🪙 $%.2f (%@$%.2f today)", equity, change < 0 ? "−" : "+", abs(change))
            if let beat = bot["heartbeat"] as? String,
               let at = ISO8601DateFormatter().date(from: beat), Date().timeIntervalSince(at) > 600 {
                line += " ⚠️ bot stopped"
            }
            parts.append(line)
        }
        if let repo = repoPath,
           let data = try? Data(contentsOf: URL(fileURLWithPath: repo).appendingPathComponent("memory/etsy_shop.json")),
           let shop = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] {
            let since = Calendar.current.startOfDay(for: Date()).timeIntervalSince1970
            let sales = (shop["sales_log"] as? [[String: Any]] ?? [])
                .filter { (($0["at"] as? NSNumber)?.doubleValue ?? 0) >= since }
            let revenue = sales.reduce(0.0) { $0 + (($1["price"] as? NSNumber)?.doubleValue ?? 0) }
            parts.append("🛍️ \(sales.count) sale\(sales.count == 1 ? "" : "s") today"
                + (revenue > 0 ? String(format: " ($%.2f)", revenue) : ""))
        }
        return parts.isEmpty ? "💰 No money data yet" : parts.joined(separator: "   ")
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Jarvis could not start"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
        screenTimer?.cancel()
        if let child = child, child.isRunning { child.terminate() }
        if let directory = controlDirectory {
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("command"))
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("status"))
        }
    }
}

@main
struct JarvisApplication {
    static func main() {
        let app = NSApplication.shared
        let delegate = JarvisLauncher()
        app.delegate = delegate
        app.run()
    }
}
