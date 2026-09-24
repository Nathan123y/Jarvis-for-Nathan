import AppKit
import AVFoundation
import Foundation

final class JarvisLauncher: NSObject, NSApplicationDelegate {
    private var child: Process?
    private var statusItem: NSStatusItem?
    private var microphoneItem: NSMenuItem?
    private var statusLabel: NSMenuItem?
    private var announcementsItem: NSMenuItem?
    private let announcer = NotificationAnnouncer()
    private var refreshTimer: Timer?
    private var screenTimer: Timer?
    private var controlDirectory: URL?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        let directory = FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Jarvis/MenuControl")
        do {
            try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                    attributes: [.posixPermissions: 0o700])
            controlDirectory = directory
            announcer.controlDirectory = directory
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("command"))
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("status"))
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("announcing"))
        } catch {
            showError("Could not prepare menu controls: \(error.localizedDescription)")
            return
        }
        setupMenu()
        screenTimer = Timer.scheduledTimer(withTimeInterval: 0.15, repeats: true) { [weak self] _ in
            self?.handleScreenRequest()
        }
        announcer.start()
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
        let main = URL(fileURLWithPath: repo).appendingPathComponent("main.py").path
        guard FileManager.default.fileExists(atPath: main),
              FileManager.default.isExecutableFile(atPath: python) else {
            showError("Jarvis project or Python moved. Run the Jarvis installer again.")
            return
        }
        let logDir = FileManager.default.homeDirectoryForCurrentUser
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
        menu.addItem(NSMenuItem(title: "Open Jarvis", action: #selector(openWindow), keyEquivalent: ""))
        let microphone = NSMenuItem(title: "Mute microphone", action: #selector(toggleMicrophone), keyEquivalent: "")
        menu.addItem(microphone)
        microphoneItem = microphone
        let announcements = NSMenuItem(title: "Announce notifications", action: #selector(toggleAnnouncements), keyEquivalent: "")
        menu.addItem(announcements)
        announcementsItem = announcements
        menu.addItem(.separator())
        menu.addItem(NSMenuItem(title: "Quit Jarvis", action: #selector(quit), keyEquivalent: ""))
        for entry in menu.items where entry.action != nil { entry.target = self }
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
        guard CGPreflightScreenCaptureAccess() else {
            try? "Jarvis needs Screen & System Audio Recording access. Quit and reopen Jarvis after granting it."
                .write(to: errorFile, atomically: true, encoding: .utf8)
            return
        }
        guard let frame = CGDisplayCreateImage(CGMainDisplayID()),
              let png = NSBitmapImageRep(cgImage: frame).representation(using: .png, properties: [:]) else {
            try? "Jarvis could not capture the display."
                .write(to: errorFile, atomically: true, encoding: .utf8)
            return
        }
        do {
            try png.write(to: response, options: .atomic)
        } catch {
            try? "Jarvis could not save the screenshot: \(error.localizedDescription)"
                .write(to: errorFile, atomically: true, encoding: .utf8)
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
        announcementsItem?.state = announcer.enabled ? .on : .off
        announcementsItem?.title = announcer.enabled && !announcer.trusted
            ? "Grant Accessibility to announce" : "Announce notifications"
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
    @objc private func toggleAnnouncements() {
        if announcer.enabled && !announcer.trusted {
            _ = AXIsProcessTrustedWithOptions(["AXTrustedCheckOptionPrompt": true] as CFDictionary)
        } else {
            announcer.setEnabled(!announcer.enabled)
        }
        refreshMenu()
    }
    @objc private func quit() { NSApp.terminate(nil) }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Jarvis could not start"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
        screenTimer?.invalidate()
        announcer.stop()
        if let child = child, child.isRunning { child.terminate() }
        if let directory = controlDirectory {
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("command"))
            try? FileManager.default.removeItem(at: directory.appendingPathComponent("status"))
        }
    }
}

let app = NSApplication.shared
let delegate = JarvisLauncher()
app.delegate = delegate
app.run()
