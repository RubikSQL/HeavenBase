ObjC.import("AppKit");
ObjC.import("Foundation");
ObjC.import("WebKit");

ObjC.registerSubclass({
  name: "HeavenBaseAppDelegate",
  methods: {
    "applicationShouldTerminateAfterLastWindowClosed:": {
      types: ["bool", ["id"]],
      implementation: () => true,
    },
  },
});

/**
 * Opens the loopback dashboard in a native WKWebView window.
 *
 * The host owns only the window lifecycle. All application behavior remains
 * behind the local JSON API served by `heavenbase.api`.
 *
 * @param {string[]} argv - Dashboard URL followed by an optional app icon path.
 */
function run(argv) {
  if (argv.length < 1) {
    throw new Error("HeavenBase WebView host requires a dashboard URL");
  }

  const url = $.NSURL.URLWithString(argv[0]);
  if (
    !url ||
    ObjC.unwrap(url.scheme) !== "http" ||
    !["127.0.0.1", "localhost", "::1"].includes(ObjC.unwrap(url.host))
  ) {
    throw new Error("HeavenBase WebView host accepts loopback HTTP URLs only");
  }

  const app = $.NSApplication.sharedApplication;
  const delegate = $.HeavenBaseAppDelegate.alloc.init;
  app.delegate = delegate;
  app.setActivationPolicy($.NSApplicationActivationPolicyRegular);

  if (argv[1]) {
    const icon = $.NSImage.alloc.initWithContentsOfFile(argv[1]);
    if (icon) app.applicationIconImage = icon;
  }

  const frame = $.NSMakeRect(0, 0, 1280, 800);
  const style =
    $.NSWindowStyleMaskTitled |
    $.NSWindowStyleMaskClosable |
    $.NSWindowStyleMaskMiniaturizable |
    $.NSWindowStyleMaskResizable;
  const window = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
    frame,
    style,
    $.NSBackingStoreBuffered,
    false,
  );
  window.title = "HeavenBase";
  window.minSize = $.NSMakeSize(760, 520);
  window.releasedWhenClosed = false;

  const webView = $.WKWebView.alloc.initWithFrameConfiguration(frame, $.WKWebViewConfiguration.alloc.init);
  webView.autoresizingMask = $.NSViewWidthSizable | $.NSViewHeightSizable;
  webView.loadRequest($.NSURLRequest.requestWithURL(url));
  window.contentView = webView;
  window.center;
  window.makeKeyAndOrderFront(null);
  app.activateIgnoringOtherApps(true);
  app.run;
}
