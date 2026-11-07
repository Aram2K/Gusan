import UIKit

/// Hosts `KeyboardViewController` inside a plain app so the keyboard can be screenshotted in the Simulator,
/// where third-party keyboards cannot be enabled. Launch arguments: `--screen` arms the Screen chip.
@main
final class PreviewAppDelegate: UIResponder, UIApplicationDelegate {
    var window: UIWindow?
    func application(_ application: UIApplication, didFinishLaunchingWithOptions options: [UIApplication.LaunchOptionsKey: Any]? = nil) -> Bool {
        let args = ProcessInfo.processInfo.arguments
        UserDefaults.standard.set(args.contains("--screen"), forKey: "debugScreenPreview")
        UserDefaults(suiteName: "group.com.aram2k.gusan")?.set(args.contains("--screen"), forKey: "debugScreenPreview")
        window = UIWindow(frame: UIScreen.main.bounds)
        window?.rootViewController = PreviewHostController()
        window?.makeKeyAndVisible()
        return true
    }
}

final class PreviewHostController: UIViewController {
    private let keyboard = KeyboardViewController()
    private let sample = UITextView()
    override func viewDidLoad() {
        super.viewDidLoad()
        view.backgroundColor = .systemBackground
        sample.text = "Hey, are we still on for the launch review tomorrow at 10? Let me know if the time works."
        sample.font = .preferredFont(forTextStyle: .body); sample.isEditable = false
        sample.backgroundColor = .secondarySystemBackground; sample.layer.cornerRadius = 14
        sample.textContainerInset = UIEdgeInsets(top: 12, left: 12, bottom: 12, right: 12)
        view.addSubview(sample)
        addChild(keyboard); view.addSubview(keyboard.view); keyboard.didMove(toParent: self)
    }
    override func viewDidLayoutSubviews() {
        super.viewDidLayoutSubviews()
        let safe = view.safeAreaInsets
        let height: CGFloat = 300
        keyboard.view.frame = CGRect(x: 0, y: view.bounds.height - safe.bottom - height, width: view.bounds.width, height: height)
        sample.frame = CGRect(x: 16, y: safe.top + 16, width: view.bounds.width - 32, height: 140)
    }
}
