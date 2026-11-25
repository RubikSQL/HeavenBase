ObjC.import("AppKit");
ObjC.import("Foundation");
ObjC.import("WebKit");

let serverTask = null;
let applicationBundle = null;
let applicationDelegate = null;
let mainWindow = null;

function unwrap(value) {
  return value ? ObjC.unwrap(value) : "";
}

function metadataValue(bundle, key) {
  return unwrap(bundle.infoDictionary.objectForKey(key));
}

function resolveApplicationBundle() {
  if (applicationBundle) return applicationBundle;
  const mainBundle = $.NSBundle.mainBundle;
  if (metadataValue(mainBundle, "HBPythonExecutable")) {
    applicationBundle = mainBundle;
    return applicationBundle;
  }

  const args = $.NSProcessInfo.processInfo.arguments;
  for (let index = 0; index < args.count; index += 1) {
    const executable = unwrap(args.objectAtIndex(index));
    const marker = "/Contents/MacOS/";
    const markerIndex = executable.lastIndexOf(marker);
    if (markerIndex < 0) continue;
    const bundlePath = executable.slice(0, markerIndex);
    if (!bundlePath.endsWith(".app")) continue;
    const candidate = $.NSBundle.bundleWithPath(bundlePath);
    if (candidate && metadataValue(candidate, "HBPythonExecutable")) {
      applicationBundle = candidate;
      return applicationBundle;
    }
  }
  throw new Error("Could not resolve the enclosing HeavenBase.app bundle");
}

function bundleValue(key) {
  return metadataValue(resolveApplicationBundle(), key);
}

function processArguments() {
  const values = [];
  const args = $.NSProcessInfo.processInfo.arguments;
  for (let index = 0; index < args.count; index += 1) values.push(unwrap(args.objectAtIndex(index)));
  return values;
}

function configureTask(executable, args, capture = false) {
  const task = $.NSTask.alloc.init;
  task.executableURL = $.NSURL.fileURLWithPath(executable);
  task.arguments = args;
  if (capture) {
    const pipe = $.NSPipe.pipe;
    task.standardOutput = pipe;
    task.standardError = $.NSFileHandle.fileHandleWithNullDevice;
    return { task, pipe };
  }
  task.standardOutput = $.NSFileHandle.fileHandleWithNullDevice;
  task.standardError = $.NSFileHandle.fileHandleWithNullDevice;
  return { task, pipe: null };
}

function runCaptured(executable, args) {
  const { task, pipe } = configureTask(executable, args, true);
  const error = Ref();
  if (!task.launchAndReturnError(error)) {
    throw new Error(`Could not launch ${executable}: ${unwrap(error[0].localizedDescription)}`);
  }
  task.waitUntilExit;
  const data = pipe.fileHandleForReading.readDataToEndOfFile;
  const output = unwrap($.NSString.alloc.initWithDataEncoding(data, $.NSUTF8StringEncoding));
  if (task.terminationStatus !== 0) {
    throw new Error(`${executable} exited with status ${task.terminationStatus}`);
  }
  return output.trim();
}

function launchTask(executable, args) {
  const { task } = configureTask(executable, args);
  const error = Ref();
  if (!task.launchAndReturnError(error)) {
    throw new Error(`Could not launch ${executable}: ${unwrap(error[0].localizedDescription)}`);
  }
  return task;
}

function isLoopbackUrl(value) {
  const url = $.NSURL.URLWithString(value);
  if (!url) return false;
  return unwrap(url.scheme) === "http" && ["127.0.0.1", "localhost", "::1"].includes(unwrap(url.host));
}

function startServer(python, module, bind) {
  const files = $.NSFileManager.defaultManager;
  const directory = `${unwrap(files.temporaryDirectory.path)}/heavenbase-${unwrap($.NSUUID.UUID.UUIDString)}`;
  const error = Ref();
  if (!files.createDirectoryAtPathWithIntermediateDirectoriesAttributesError(directory, false, null, error)) {
    throw new Error(`Could not prepare dashboard startup: ${unwrap(error[0].localizedDescription)}`);
  }
  const readyPath = `${directory}/ready`;
  try {
    serverTask = launchTask(python, [
      "-m",
      module,
      "--serve",
      "--host",
      bind.host,
      "--port",
      String(bind.port),
      "--ready-file",
      readyPath,
    ]);
    for (let attempt = 0; attempt < 50; attempt += 1) {
      if (!serverTask.running) {
        throw new Error(`HeavenBase dashboard server exited with status ${serverTask.terminationStatus}`);
      }
      if (files.fileExistsAtPath(readyPath)) {
        const ready = unwrap(
          $.NSString.stringWithContentsOfFileEncodingError(readyPath, $.NSUTF8StringEncoding, Ref()),
        );
        // The trailing newline marks a complete write from the child.
        const port = Number(ready);
        if (/^[1-9]\d{0,4}\n$/.test(ready) && port <= 65535 && serverTask.running) return port;
      }
      $.NSThread.sleepForTimeInterval(0.15);
    }
    throw new Error("HeavenBase dashboard server did not become ready");
  } finally {
    files.removeItemAtPathError(directory, Ref());
  }
}

function stopServer() {
  if (!serverTask?.running) return;
  serverTask.terminate;
  for (let attempt = 0; attempt < 20 && serverTask.running; attempt += 1) {
    $.NSThread.sleepForTimeInterval(0.05);
  }
  if (serverTask.running) serverTask.interrupt;
}

ObjC.registerSubclass({
  name: "HeavenBaseBundledAppLifecycle",
  methods: {
    "windowWillClose:": {
      types: ["void", ["id"]],
      implementation: () => {
        stopServer();
        $.NSApplication.sharedApplication.terminate(null);
      },
    },
    "applicationWillTerminate:": {
      types: ["void", ["id"]],
      implementation: stopServer,
    },
  },
});

function showFailure(message) {
  const alert = $.NSAlert.alloc.init;
  alert.messageText = "HeavenBase could not start";
  alert.informativeText = String(message);
  alert.alertStyle = $.NSAlertStyleCritical;
  alert.runModal;
}

/**
 * Run the installed HeavenBase application.
 *
 * A loopback URL argument connects the window to an existing CLI-owned server.
 * A normal Finder launch starts a child server and terminates it when the last
 * application window closes.
 *
 */
function run() {
  const supplied = arguments.length > 0 && Array.isArray(arguments[0]) ? arguments[0] : [];
  const argv = [...processArguments(), ...supplied];
  if (argv.includes("--print-bundle")) return unwrap(resolveApplicationBundle().bundlePath);

  const app = $.NSApplication.sharedApplication;
  applicationDelegate = $.HeavenBaseBundledAppLifecycle.alloc.init;
  $.NSNotificationCenter.defaultCenter.addObserverSelectorNameObject(
    applicationDelegate,
    "applicationWillTerminate:",
    $.NSApplicationWillTerminateNotification,
    null,
  );
  app.setActivationPolicy($.NSApplicationActivationPolicyRegular);

  try {
    const requestedUrl = argv.find((value) => isLoopbackUrl(value));
    let dashboardUrl = requestedUrl || "";
    if (!dashboardUrl) {
      const python = bundleValue("HBPythonExecutable");
      const module = bundleValue("HBPythonModule") || "heavenbase.dashboard.macos";
      if (!python)
        throw new Error(
          "The application bundle has no Python runtime binding; reinstall it with `hb dashboard --install-app`.",
        );
      const bind = JSON.parse(runCaptured(python, ["-m", module, "--print-bind"]));
      const port = startServer(python, module, bind);
      dashboardUrl = `http://${bind.host.includes(":") ? `[${bind.host}]` : bind.host}:${port}`;
    }
    if (!isLoopbackUrl(dashboardUrl)) throw new Error("HeavenBase accepts loopback dashboard URLs only");

    const iconPath = `${unwrap(resolveApplicationBundle().resourcePath)}/HeavenBase.icns`;
    const icon = $.NSImage.alloc.initWithContentsOfFile(iconPath);
    if (icon) app.applicationIconImage = icon;

    const frame = $.NSMakeRect(0, 0, 1280, 800);
    const style =
      $.NSWindowStyleMaskTitled |
      $.NSWindowStyleMaskClosable |
      $.NSWindowStyleMaskMiniaturizable |
      $.NSWindowStyleMaskResizable;
    mainWindow = $.NSWindow.alloc.initWithContentRectStyleMaskBackingDefer(
      frame,
      style,
      $.NSBackingStoreBuffered,
      false,
    );
    mainWindow.title = "HeavenBase";
    mainWindow.minSize = $.NSMakeSize(760, 520);
    mainWindow.releasedWhenClosed = false;
    mainWindow.delegate = applicationDelegate;

    const webView = $.WKWebView.alloc.initWithFrameConfiguration(frame, $.WKWebViewConfiguration.alloc.init);
    webView.autoresizingMask = $.NSViewWidthSizable | $.NSViewHeightSizable;
    webView.loadRequest($.NSURLRequest.requestWithURL($.NSURL.URLWithString(dashboardUrl)));
    mainWindow.contentView = webView;
    mainWindow.center;
    mainWindow.makeKeyAndOrderFront(null);
    app.activateIgnoringOtherApps(true);
  } catch (error) {
    stopServer();
    const message = error instanceof Error ? error.message : String(error);
    $.NSLog(`HeavenBase startup failed: ${message}`);
    showFailure(message);
  }
}
