import AppKit
import AVFoundation
import Foundation

final class JarvisLauncher: NSObject, NSApplicationDelegate {
    private var child: Process?

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
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
            process.environment = environment
            process.standardOutput = handle
            process.standardError = handle
            process.terminationHandler = { process in
                DispatchQueue.main.async {
                    handle.closeFile()
                    if process.terminationStatus != 0 {
                        self.showError("Jarvis stopped. See ~/Library/Logs/Jarvis/launch.log for details.")
                    } else {
                        NSApp.terminate(nil)
                    }
                }
            }
            child = process
            try process.run()
        } catch {
            showError("Jarvis could not start: \(error.localizedDescription)")
        }
    }

    private func showError(_ message: String) {
        let alert = NSAlert()
        alert.messageText = "Jarvis could not start"
        alert.informativeText = message
        alert.runModal()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        if let child = child, child.isRunning { child.terminate() }
    }
}

let app = NSApplication.shared
let delegate = JarvisLauncher()
app.delegate = delegate
app.run()
