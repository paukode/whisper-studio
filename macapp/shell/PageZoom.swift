// Whisper Studio — page zoom for the app window, as a browser does it.
//
// View > Actual Size (Cmd 0), Zoom In (Cmd + or Cmd =) and Zoom Out (Cmd -)
// step WKWebView.pageZoom through the browser zoom levels. Page zoom reflows
// the layout rather than magnifying pixels, so everything scales together:
// text, images, charts, the terminal and the editors. Web content keeps
// consistent CSS pixel geometry, so drag handles and popups stay aligned.
//
// The level is remembered in UserDefaults, so the app reopens at the size the
// user left it. The menu items are the only surface. Their shortcuts work
// wherever focus is in the window, a chart's iframe included, because the app
// handles them rather than page script.

import AppKit
import WebKit

final class PageZoom: NSObject, NSMenuItemValidation {

    /// The steps Chrome and Safari use between 50% and 200%.
    static let levels: [CGFloat] = [0.5, 0.67, 0.75, 0.8, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0]
    static let defaultsKey = "PageZoom"

    private let defaults: UserDefaults
    private(set) var level: CGFloat

    /// The zoomed view. Setting it applies the remembered level at once, so
    /// the first paint is already at the user's size.
    weak var webView: WKWebView? {
        didSet { webView?.pageZoom = level }
    }

    init(defaults: UserDefaults = .standard) {
        self.defaults = defaults
        let saved = CGFloat(defaults.double(forKey: Self.defaultsKey))
        // 0 means never set. A value from elsewhere snaps to the nearest step
        // so Zoom In and Zoom Out always land on the same ladder.
        level = saved > 0 ? Self.nearestLevel(to: saved) : 1.0
        super.init()
    }

    static func nearestLevel(to value: CGFloat) -> CGFloat {
        levels.min(by: { abs($0 - value) < abs($1 - value) }) ?? 1.0
    }

    // MARK: Menu

    /// Actual Size, Zoom In and Zoom Out for the View menu. Zoom In answers
    /// both Cmd + (Cmd Shift =) and plain Cmd =, the way browsers do; the
    /// second key lives on a hidden item that only takes the shortcut.
    func menuItems() -> [NSMenuItem] {
        let actual = item("Actual Size", #selector(actualSize(_:)), "0")
        let zoomIn = item("Zoom In", #selector(zoomIn(_:)), "+")
        let zoomInEquals = item("Zoom In", #selector(zoomIn(_:)), "=")
        zoomInEquals.isHidden = true
        zoomInEquals.allowsKeyEquivalentWhenHidden = true
        let zoomOut = item("Zoom Out", #selector(zoomOut(_:)), "-")
        return [actual, zoomIn, zoomInEquals, zoomOut]
    }

    private func item(_ title: String, _ action: Selector, _ key: String) -> NSMenuItem {
        let menuItem = NSMenuItem(title: title, action: action, keyEquivalent: key)
        menuItem.keyEquivalentModifierMask = [.command]
        menuItem.target = self
        return menuItem
    }

    func validateMenuItem(_ menuItem: NSMenuItem) -> Bool {
        guard webView != nil else { return false }
        switch menuItem.action {
        case #selector(zoomIn(_:)): return level < Self.levels[Self.levels.count - 1]
        case #selector(zoomOut(_:)): return level > Self.levels[0]
        case #selector(actualSize(_:)): return level != 1.0
        default: return true
        }
    }

    // MARK: Actions

    @objc func zoomIn(_ sender: Any?) {
        if let next = Self.levels.first(where: { $0 > level }) { set(next) }
    }

    @objc func zoomOut(_ sender: Any?) {
        if let next = Self.levels.last(where: { $0 < level }) { set(next) }
    }

    @objc func actualSize(_ sender: Any?) {
        set(1.0)
    }

    private func set(_ value: CGFloat) {
        level = value
        webView?.pageZoom = value
        defaults.set(Double(value), forKey: Self.defaultsKey)
    }
}
