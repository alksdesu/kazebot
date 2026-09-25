# Clonoth SDK

Pure protocol layer that encapsulates all communication between a Bot adapter and the Clonoth Supervisor. The SDK handles HTTP API calls, WebSocket events, protocol state management, and approval logic — so adapters only need to implement platform-specific operations (sending messages, editing UI, etc.).

The SDK has **zero platform dependencies** (no nonebot, no telegram, etc.). Platform concerns live in the adapter.

## Architecture

Four layers, bottom to top:

| Layer | Class | Role |
|-------|-------|------|
| **HTTP Client** | `ClonothClient` | 11 typed async methods wrapping every Supervisor HTTP endpoint |
| **State** | `SessionState` | Centralized runtime state — replaces scattered global dicts (triggers, session maps, watermarks, task states) |
| **Event Router** | `EventRouter` | Consumes `/v1/ws`, recovers missed replies, and dispatches events to adapter callbacks |
| **Callbacks** | `AdapterCallbacks` | 15-method `Protocol` interface — the adapter implements these to perform platform operations |

Supporting modules: `BotConfig` (configuration injection), `types` (dataclasses for API responses), `approval` (dedup + path classification + auto-approve).

## Reply recovery and upgrades

Upgrade the SDK and Supervisor together. The SDK requires outbound recovery protocol version 1 on `/v1/ws`; an older Supervisor produces an explicit compatibility error instead of silently using an unreliable stream. Recovery requires an admin token. HTTP requests and WebSocket reconnects both read the latest `admin_token_path` value, falling back to the constructor's `admin_token` if the file is unavailable or empty. Both legacy and current `websockets` header argument names are supported.

The first connection of a new outbox starts with new events and does not resend historical replies. Once initialized, its SQLite receive cursor survives restarts. Reconnection recovers only `outbound_message` and `intermediate_reply` in bounded pages, while approvals and progress events remain live only. The Supervisor subscribes atomically with the replay boundary so replies produced during the handshake or catch-up are preserved. A local persistence failure stops consumption before later events can advance the cursor; platform delivery failures remain in the local outbox for retry. Delivery acknowledgements cannot move the receive cursor, and sent deduplication records are retained until receipt is checkpointed.

Recovery covers the Supervisor's retained event logs, including rotated files. If an outage exceeds log retention, the SDK logs `Outbound recovery exceeds retained logs` and recovers the remaining retained replies; deleted logs cannot be reconstructed. Keep the adapter outbox when restarting or upgrading. Existing Web clients that do not request recovery keep their live event behavior.

Adapters may accept `is_final: bool = True` in `send_to_channel`. Intermediate replies, including attachments, pass `is_final=False` so an adapter can keep waiting for the final reply. Callbacks without this optional parameter keep working.

## Two-Layer Hook Architecture

```
  Supervisor event stream
        │
        ▼
  ┌─ Layer 1: on_raw_event hook ──────────────────────┐
  │  Adapter-registered interceptor.                   │
  │  Runs BEFORE SDK default processing.               │
  │  Return 'handled' → SDK skips this event.          │
  │  Return None → SDK continues to Layer 2.           │
  │  Use case: stream_delta animation, custom events.  │
  └────────────────────────────────────────────────────┘
        │
        ▼
  ┌─ SDK protocol processing ─────────────────────────┐
  │  Trigger matching, state updates, dedup,           │
  │  watermark advancement, approval classification.   │
  └────────────────────────────────────────────────────┘
        │
        ▼
  ┌─ Layer 2: AdapterCallbacks ───────────────────────┐
  │  SDK calls the appropriate callback method.        │
  │  Adapter performs platform I/O (send, edit, etc.). │
  └────────────────────────────────────────────────────┘
```

## Quick Start

```python
import asyncio
import sys
sys.path.insert(0, "/www/wwwroot/Clonoth")

from clonoth_sdk import (
    BotConfig, ClonothClient, SessionState,
    EventRouter, AdapterCallbacks,
)

# 1. Configuration
config = BotConfig(
    base_url="http://127.0.0.1:8765",
    entry_node_id="ereuna_main",
    conversation_key_prefix="qq_group",
)

# 2. Core objects
client = ClonothClient(
    config.base_url,
    admin_token_path="/www/wwwroot/Clonoth/data/.admin_token",
)
state = SessionState()

# 3. Implement AdapterCallbacks (all 15 async methods)
class MyAdapter:
    async def send_reply(self, trigger, text, attachments, *, main_state=None):
        print(f"Reply: {text}")
    # ... implement remaining 14 callbacks ...

callbacks = MyAdapter()

# 4. Create router and start event loop
router = EventRouter(client, state, callbacks, config)

# Optional: register Layer 1 hook for stream_delta handling
async def my_hook(event):
    if event.type == "stream_delta":
        # custom animation logic
        return "handled"  # skip SDK default
    return None  # let SDK handle

router.set_raw_event_hook(my_hook)

# 5. Run (blocks until cancelled or router.stop())
asyncio.run(router.run())
```

## File Inventory

| File | Description |
|------|-------------|
| `__init__.py` | Public API surface — re-exports all user-facing symbols |
| `client.py` | `ClonothClient` — async HTTP client for 11 Supervisor API endpoints |
| `state.py` | `SessionState` + dataclasses (`TriggerInfo`, `MainTaskState`, `ChildTaskState`) — centralized runtime state |
| `callbacks.py` | `AdapterCallbacks` — 15-method `typing.Protocol` the adapter implements |
| `event_router.py` | `EventRouter` — WebSocket recovery, event handlers, Layer 1/2 dispatch, `strip_protocol_markers()` |
| `config.py` | `BotConfig` — configuration dataclass injected into router and client |
| `types.py` | `Event`, `InboundResult`, `RunningTask`, `HealthInfo`, `OpenAIConfig` — API response types |
| `approval.py` | `ApprovalTracker` (dedup), `classify_path` / `is_external_operation` (path classification), `auto_approve` (retry logic) |

## SDK Boundary

**Inside SDK:**
- `ClonothClient` — all Supervisor HTTP API communication
- Data types — `InboundResult`, `Event`, `RunningTask`, etc.
- Approval policy — dedup, path classification, auto-approve
- `SessionState` — trigger lifecycle, session mapping, watermarks, task states
- `EventRouter` — WebSocket events and reply recovery, protocol dispatch
- `AdapterCallbacks` — callback protocol definition
- `BotConfig` — configuration injection
- Protocol marker cleanup — `[CLONOTH_TOOL_TRACE]` stripping

**Outside SDK (adapter responsibility):**
- `[SPLIT]` message segmentation, `[REACT:xxx]` reaction extraction, `[BOT_RESTART]` signal handling
- `TextProcessor` / protocol marker display formatting
- Platform libraries (nonebot, telegram, etc.)
- Dot animation, typing throttle, streaming preview — display-layer logic
- Channel history queue management
- UI components (approval buttons, cancel buttons, embeds)

## Restart Scope

SDK implementation changes that preserve the wire protocol only require restarting the **Bot process**. Protocol changes require updating and restarting the affected backend as well. The outbound recovery protocol introduced here requires matching SDK and Supervisor versions: update both, then restart the **Supervisor and Bot processes** while preserving their event logs and outbox. Engine changes require a separate Engine restart.
