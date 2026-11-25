# HeavenBase GUI

`ui/` owns the zero-bundler workbench and `host/` owns the thin system WebView
shell. The Python application service and HTTP adapter live under
`src/heavenbase/api/`; GUI code talks to that boundary only through loopback
JSON routes.

Every owned UI module lives in `ui/src/` as strict TypeScript and compiles to
checked-in ES modules in `ui/js/`. The protocol types in
`ui/src/generated/schema.d.ts` come from the committed dashboard OpenAPI document;
network JSON is runtime-validated before entering typed UI code. Use:

```bash
rtk bun ci
rtk bun run check
```

from this directory. The build emits plain browser modules without a bundler.
CodeMirror is pinned as a development dependency and copied into the
zero-bundler runtime by `bun run vendor:sync`; `bun run check` verifies that the
checked-in editor assets still match the lockfile version.
Python wheel builds install the distributable UI, raw macOS host, packaged-app
host, and `.icns` file under `share/heavenbase/gui/`.

On macOS, `hb dashboard --install-app` creates the user-local
`~/Applications/HeavenBase.app`. Finder launch owns its loopback server child;
closing the last window ends that server. Re-running `--install-app` confirms
before replacing an existing bundle; `--yes`/`-y` skips that prompt. Appearance
preferences and the last successfully opened workspace live in the shared
HeavenBase profile rather than WebView-local storage. The workspace picker
lists only real registrations and exposes confirmed unregister actions that
never delete backing database data. Installation compiles the packaged
JavaScript host as a stay-open applet and applies an ad-hoc signature after
injecting the runtime binding. Developer-identity signing, notarization, and
Windows packaging remain outside this slice.
