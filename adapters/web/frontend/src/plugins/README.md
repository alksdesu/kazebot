# Frontend plugin SDK

A package declares `sdk_version: 1`, `frontend: "frontend.js"`, optional `styles`, and optional `assets` in `plugin.json`. The frontend entry is a **prebuilt, self-contained ES module** exporting `activate(sdk)`. Use `sdk.React` (the host's React 18); do not bundle another React instance or leave bare/relative imports in the output. JSX/TypeScript must be built before packaging. `examples/plugins/system-workspace` runs without a build step.

The host fetches enabled modules and declared styles using its instance mount prefix and Bearer credential, verifies SHA-256 fingerprints, and imports the entry through a Blob URL. HTTPS or localhost is required for browser integrity verification. The server retains hash-addressed package assets for older open pages. CSS `@import` and relative `url()` resources are unsupported; use `sdk.assets.url(path)` and `sdk.styles.add(css)` for package resources.

## Registration

All declarations belong to the plugin id and generation. Call registrations during `activate`; registration disposers may be called later. Activation can be async and return an async disposer. A candidate is published only after activation succeeds; a failed upgrade retains the previous generation. The host removes old registrations and styles, aborts generation requests, and invokes managed cleanup in reverse order. Activation has a 30-second limit; each cleanup has a five-second limit. These limits cannot interrupt synchronous JavaScript.

Each registry provides `register(id, value, { replace?, priority? })` and `wrap(id, next => replacement, { priority? })`. New ids use `register`; collisions require explicit `replace: true` or `wrap`. Higher priorities form the outer layer, with plugin id as the stable tie breaker. Unloading a layer exposes the next active layer, including the built-in base. Wrapper factories must be pure: the host may resolve them repeatedly with the currently active next layer.

| Registry | Value |
| --- | --- |
| `sdk.views` | `{ id, sidebar(ctx), header(ctx), main(ctx), composer?(ctx), rightTop?(ctx), rightBottom?(ctx), hasRightPanel?(ctx) }` |
| `sdk.workspaces` | `{ id, label, icon?, order? }`; register a matching view to provide its contents |
| `sdk.settings` | `{ id, label, icon?, order, group?, Page, RightPanel? }`; React components for the page and optional panel |
| `sdk.slots` | `(ctx) => ReactNode`; built-in targets are `chat/settings/execution/materials` followed by `.sidebar`, `.header`, `.main`, `.composer`, `.rightTop`, or `.rightBottom` |
| `sdk.slots`, `app.layout` | `(props) => ReactNode`; props include sidebar, header, composer, rightPanel, logPanel, navigationKey, children |
| `sdk.commands` | `(...args) => result` or Promise; `execute(id, ...args)` invokes the current chain |

View and slot contexts include the current session, conversation, messages, task state, and host callbacks; the complete type is `AppViewContext` in `views/viewRegistry.tsx`. Settings groups are `basics`, `bot`, `models`, `engine`, and `platform`. New workspace and setting links survive initial catalog loading. Navigation continues to use the host's unsaved-draft guard.

Built-in command ids:

- `chat.send(text, attachments?)`, `chat.cancel()`, `chat.reset()` (opens the confirmation dialog), `chat.reset.confirm()` (confirms the selected reset).
- `session.select(id)`, `session.create()`, `session.delete(id)`, `session.rename(id, title)`, `session.exitChild()`.
- `navigation.workspace(id)`, `navigation.settings(tab?, section?)`, `navigation.settingsTab(tab, section?)`, `navigation.closeSettings()`.
- `node.switch(sessionId, targetNodeId)` invokes the existing node-switch API.

Handlers already executing retain their chain. A failed command is propagated without retrying the next handler, to avoid submitting a mutation twice. Render errors fall back to the next view/slot/settings layer and appear in the plugin page's browser status.

## Host access and managed resources

- `sdk.http.request('/plugins/my-plugin/status', init?)` returns a Response from this instance's `/v1` API, with authentication and generation cancellation. It also accepts an optional caller AbortSignal.
- `sdk.api` exposes the existing supervisor client functions; functions requiring a token retain their original signatures.
- `sdk.state.chat/settings/preferences/view` each expose `getState()`, `subscribe(listener)` (owned cleanup), and `use(selector)`.
- `sdk.forms.useUnsavedChanges(dirty, message?)` joins the host's navigation and tab-close guards; `confirmNavigation()` checks them.
- `sdk.assets.fetch(path)` returns a Blob for a declared package asset. `sdk.assets.url(path)` returns an owned Blob URL.
- `sdk.styles.add(css)`, `sdk.events.listen(target, event, listener, options?)`, `sdk.timers.interval(callback, ms)`, and `sdk.timers.timeout(callback, ms)` return owned disposers.
- `sdk.lifecycle.onDispose(callback)` registers arbitrary managed cleanup. `sdk.signal` is aborted on rollback, replacement, disable, logout, or token change.
- `sdk.requests.wrap(next => (input, init) => next(input, init))` wraps the shared fetch transport, including feature and upload/download calls that use `fetchWithDeadline`. Plugin management/assets, authentication, runtime recovery, and health requests bypass wrappers so a broken transport can still be disabled. Invocation failures are attributed to the plugin layer that threw. WebSockets are a separate transport. Call `next` to continue a request; recursively calling SDK HTTP from a wrapper re-enters the wrapper.

Plugins are trusted application code with access to the host, not a JavaScript sandbox. Managed registrations/resources are reversible; arbitrary global mutation, module-top-level effects, external writes, and deliberately uncooperative asynchronous work cannot be rolled back. Put setup inside `activate`, use managed resource helpers, and honor `sdk.signal` after awaits. Browser status distinguishes the requested version, active version, registration generation, and activation/render/cleanup errors from backend process state.
