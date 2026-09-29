<div align="center">

# Clonoth

**An AI bot built to live in a group chat.**

[![License](https://img.shields.io/badge/License-GPL--3.0-1f6feb?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-1f6feb?style=flat-square)](#quick-start)
[![OneBot](https://img.shields.io/badge/OneBot-11-1f6feb?style=flat-square)](#features)
[![Tests](https://img.shields.io/badge/tests-pytest%20%2B%20Vitest-1f6feb?style=flat-square)](#development)

<a href="README.md"><img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-57606a?style=for-the-badge" alt="简体中文"></a>
<a href="README.en.md"><img src="https://img.shields.io/badge/English-1f6feb?style=for-the-badge" alt="English"></a>

</div>

---

It reads the images and files people drop in chat, expands forwarded message trees in full, collects its own sticker library, writes its own tools, schedules its own cron jobs, and remembers who it talked to about what.

Anyone in the group can ask it to search, draw, or work something out — whatever comes up in conversation. General server operations, such as editing files or running shell commands, require administrator access; a restricted channel allows read-only `curl` requests to public URLs.

You mostly won't touch YAML — there's a web console, with live updates for models, persona, and message settings. The QQ page shows whether the adapter has loaded the latest configuration.

## Contents

- [Features](#features)
- [Architecture](#architecture)
- [Permissions and security](#permissions-and-security)
- [Quick start](#quick-start)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Development](#development)
- [Acknowledgements](#acknowledgements)
- [License](#license)

## Features

### In the group

**When it speaks up** — by default, only when someone @s it. You can also let it respond to its own name, to keywords, to a random dice roll, or to everything. When none of those match, an optional cheap model call decides whether the message actually wants a reply. If it gets chatty, add a cooldown, per group or per person.

The console lets you **audition** the rules: feed in a few fake messages and watch whether it would reply and which rule stopped it. That's the same logic production runs, not a separate simulation.

**What it can read** — images, files, and forwarded messages. Forwards expand all the way down: nested cards, and the images and files tucked inside them, all come through. When someone dumps a chat log and asks "what's this about," it can actually see the contents. Quoted replies are resolved too. QQ likes to split "a sentence plus a picture" into two events, so it waits two seconds and stitches them back together.

**How it talks** — write a marker mid-reply and it splits into separate messages. It quotes the message that triggered it. While it works, it sticks reactions on your message to show progress (got it → thinking → looking things up → writing), then clears them when it's done. To @ someone it just writes their name; the real mention gets substituted on the way out.

**Stickers** — it collects them from the group. New images land in a review queue, a vision model tags them, and they only join the library once you approve them in the console. After that, each turn has a chance of showing the model the whole tagged library so it can pick one itself — or not. Anything sent in the last half hour is held back. Multiple bot instances share one library.

**Also** — rule-based meme-following that costs no model calls, admin-triggered outbound messages, the model forwarding chat history to you on request, per-person memory (mention someone and everything it knows about them gets pulled in), and twenty-odd slash commands intercepted before the model so they don't cost tokens.

### What it can do on its own

| | |
|---|---|
| **Search** | Web search, page fetch, image captioning. Prefers the model's own browsing, falls back to Exa |
| **Draw** | NovelAI (with style presets switchable from chat), Gemini, GPT Image |
| **Compute** | Symbolic math from LaTeX input, stock quotes and candles |
| **Files and shell** | Read, write, patch, grep, run commands |
| **Scheduling** | Five-field cron. Send itself a message at a time, or run a script and feed itself the result |
| **Memory** | Saves, recalls, and deletes on its own. A nightly pass merges duplicates and prunes stale entries |
| **Self-extension** | Write a new tool and load it immediately, spin off a specialized child agent from a template, connect MCP servers |

When the context fills up it compacts itself, keeping recent turns verbatim and folding older ones into a summary. Large jobs get split into subtasks and handed to other nodes.

### Web console

React, served by supervisor, open it in a browser.

The **chat view** lets you talk to it directly and watch every step: which tool it called, what came back, whether it's waiting on an approval. Dispatched subtasks are clickable.

The **settings view** separates browser access from bot configuration:

- **General** shows the console's connection to supervisor and manages the current browser's administrator login. Client preferences have their own page.
- **Bot** includes **Account & connections** as its own page alongside **Persona**, global defaults, memory, stickers, community collaboration, conversations and replies, and reminders. Account & connections manages instances, shared configuration, the current instance's bot login, account switching, and memory ownership.
- **Models and providers** covers channels, credentials, fallback chains, vision models, and per-provider parameters.
- **Engine** covers runtime diagnostics, approvals, nodes, tool authorization, drawing, skills, MCP, and scheduled jobs.
- **Platform** holds the current platform's behavior and overrides: for QQ, group allowlists, reply triggers, permissions, message handling, and connection diagnostics.

### Multiple accounts

**One instance, one platform, one account.** The current deployment and instance provisioning flows use QQ + NapCat, with up to nine instances on one machine, including the main instance. After enabling instance management, add one from **Bot → Account & connections**. Deleting an instance archives its workspace by renaming rather than removing it.

By default, each instance has its own configuration, sessions, and memory. Instances can join a **shared configuration group**, which shares the entire `config/` directory: model channels and API keys, persona and nodes, skills, custom tools, MCP connections, platform settings, and permission policy. A change affects every member of that group. Their `data/` directories, including sessions and memory, stay separate; `.env` and `data/platform_secrets.yaml` keep each instance's identity and platform credentials separate. The sticker library is shared across the host independently of configuration groups.

Joining archives the instance's previous `config/`. Leaving copies the group's current configuration into a private `config/`; it does not restore the configuration from before joining.

A single instance can also swap accounts — memory isolates automatically when you switch away, and reconnects when you switch back.

## Architecture

Three layers, each its own process. **Supervisor** owns sessions, tasks, policy, approvals, and events. **Engine** runs the inference loop and the tools. **Adapter** bridges the platform.

```
                      QQ servers
                            │
                  ┌─────────▼──────────┐
                  │ NapCat (Docker)    │  OneBot 11 impl
                  └─────────┬──────────┘
                   reverse WebSocket
                            │
┌───────────────────────────▼──────────────────────────┐
│ bot.py  — NoneBot2 + adapters/onebot        :8080    │
│   allowlist → trigger → anonymize → stage → submit   │
│   consumes events; sends messages, reactions, cards  │
└──┬───────────────────────────────────────────────────┘
   │ HTTP to submit · WebSocket to receive events
┌──▼───────────────────────────────────────────────────┐
│ supervisor (FastAPI)                        :8765    │
│   sessions · tasks · routing · policy · approvals    │
│   events · scheduler · console mounted at /web ──────┼→ browser
└──┬───────────────────────────────────────────────────┘
   │ engine pulls work
┌──▼──────────────────────────────────┐
│ engine worker ×2                    │
│   take task → build prompt → model  │
│   → tools → write back → loop → done│
└──┬──────────────────────────────────┘
   ├─→ tools/*.py     one subprocess per call
   └─→ MCP servers
```

The full path of one group message: allowlist → decide whether to reply → swap QQ numbers for aliases like `UserA` → stage images and files → hand off to supervisor → create a task → engine picks it up → inject memory and skills → a few rounds of model and tools → sanitize the output → send it back.

Main directories:

| Path | What's in it |
|---|---|
| `supervisor/` | Sessions, tasks, policy, approvals, event log, scheduler, process management |
| `engine/` | Inference loop, hooks, memory, context compaction, tool execution |
| `adapters/onebot/` | QQ / OneBot messaging, login, and platform configuration |
| `adapters/_shared/` | Shared adapter logic, including configuration loading and trigger rules |
| `adapters/web/` | Console frontend |
| `platforms/` | Platform descriptions, capabilities, and runtime hook registration |
| `providers/` | Model backends: OpenAI / Responses / Anthropic / Gemini / DeepSeek |
| `toolbox/` `tools/` | Tool runtime and external tool scripts |
| `stickers/` | Sticker library |
| `defaults/` | Bundled defaults and configuration templates, versioned with the code |
| `config/` | Instance configuration and overrides; a shared group's members link to the same directory |
| `data/` | Private runtime data, sessions, memory, and platform credentials; never committed |
| `deploy/` | systemd units and deploy scripts |

Adding a model backend means adding one `providers/*.py`; it gets discovered automatically. Bundled tools live in `tools/`, and instance tools live in `config/tools/`; an instance file overrides the bundled file at the same relative path. Each tool declares which permission gate it has to clear.

## Permissions and security

**Everyone can use the chat features; server operations are restricted.** Web search, image generation, math, vision, and stickers are available to ordinary group members. General server operations — reading or writing local files, running commands, and restarting processes — are denied to non-admins. The exception is a tightly validated, read-only `curl` channel for public URLs. Images and files people send use the attachment path.

**Admin operations that matter pop a card.** When something needs clearance, the bot DMs an approval card; quote it and reply "approve." If the admin list is empty, every approval is auto-denied rather than quietly allowed.

**Policy decides what is allowed.** The bundled `defaults/policy.example.yaml` already denies or requires approval for access to secrets, node definitions, and configuration files. Administrator status does not bypass explicit deny rules. Review the instance's policy under **Engine → Tools and permissions**.

**The server layer also limits access.** In production, systemd runs the services as a dedicated user with `ProtectSystem=strict`; writable locations come from each unit's `ReadWritePaths`. The main instance's workspace is also the code directory, so the bundled main service leaves `/opt/kazebot` writable. Check the effective units and filesystem permissions when deciding what a tool can change.

A few other things that are on by default: secrets stay out of the repo (`.env`, `config/`, and `data/` are gitignored — only templates ship); the model only ever sees aliases like `GroupA` and `UserB`, with the real numbers in a local lookup table; logs are redacted before they leave; and `.env`, config files, and private keys can't be sent out over QQ, not even by an admin.

## Quick start

You'll need Python 3.11+, Node.js 18+, and an OneBot 11 implementation — NapCat is the one to use.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

mkdir -p config
cp defaults/models.example.yaml config/models.yaml    # model channels
cp defaults/policy.example.yaml config/policy.yaml    # permission policy
cp .env.example .env                                  # instance environment
cp defaults/qq.example.yaml config/qq.yaml             # QQ settings

cd adapters/web/frontend && npm ci && npm run build && cd -

python main.py     # supervisor + engine
python bot.py      # QQ adapter, in a second terminal
```

On first start, supervisor logs a console URL with a token in it — open that and configure the rest from there.

**The group allowlist and administrator list start empty.** Set `channels.allowed_groups` in `config/qq.yaml` to let it respond in those groups. `permissions.admin_users` grants administrator access and approval handling; leaving it empty auto-denies approvals but does not disable ordinary chat. Group-owner privileges are controlled separately.

## Configuration

`defaults/` ships with the repository. `config/` holds the instance's own settings: runtime values override the bundled defaults key by key, and a node file overrides the bundled file with the same name. `data/` holds private runtime state. Make local changes in `config/` rather than editing `defaults/`.

| File | Covers | Applies |
|---|---|---|
| `config/models.yaml` | Model channels and API keys, fallback chains, vision and compaction channels | On the next configuration read |
| `config/policy.yaml` | Permission policy | On the next policy decision |
| `config/features.yaml` | Platform-neutral settings: triggers, approvals, echo, image and file input, queue, stickers | Subsequent messages or adapter polling load changes; the console shows loading status |
| `config/qq.yaml` | QQ settings and overrides for platform-neutral settings | Subsequent messages or adapter polling load changes; the console shows loading status |
| `config/runtime.yaml` | Engine tuning, compaction thresholds, memory budget, cleanup | Some values apply when next read; concurrency, polling intervals, and HTTP timeouts require an engine restart |
| `config/nodes/*.yaml` | Node definitions, overriding bundled files with the same name | Loaded for new tasks |
| `config/skills/`, `config/drawtools/` | Skills and drawing configuration | Subsequent loads or calls use the updated configuration |
| `config/tools/` | Instance tools and overrides of bundled scripts | Detected and reloaded by subsequent tool polling |
| `config/mcp.yaml` | MCP client configuration | Explicitly reload MCP or restart the engine to refresh its tool list; ordinary tool reload is not enough |
| `config/secrets.env` | API keys that can travel with a shared configuration group | On the next secret read |
| `data/platform_secrets.yaml` | This instance's platform connection credentials | **Restart the affected processes** |
| `.env` | Instance ports, hosts, paths, and environment credentials | **Needs a restart** |

Don't copy `defaults/features.example.yaml` wholesale into `config/features.yaml` — take only the lines you want to change. It outranks `.env`, so a full copy silently overrides every matching setting there.

For shareable credentials, `.env` takes precedence over the same key in `config/secrets.env`. Platform credentials have their own lookup: the platform's entry in `data/platform_secrets.yaml` takes precedence over its declared environment variable. Keep instance identity out of shared configuration.

Breaking the feature or QQ settings file won't take the bot down — on a parse failure it keeps running the last good copy and logs the error. To see what's actually in effect inside the process, and which file each value came from, check the authenticated `GET /v1/platforms/qq/config/state` endpoint.

Fresh installations and new instances start with these **Bot → Conversations and replies** settings:

| Policy | Default |
|---|---|
| Unified choice between ignoring, reacting, brief replies and tasks | Off |
| Continue topics using quoted messages and participants | On |
| Wait for short messages from the same person | 8 seconds |
| Maximum wait while more messages arrive | 10 seconds |
| Autonomous reply limit per minute | 6 |

The 8/10-second windows wait for more input from the same person and combine it into one request; they do not control the delay between outgoing messages. The autonomous reply limit applies when the unified response policy is enabled.

Upgrading preserves each existing instance's effective conversation defaults and per-conversation overrides rather than forcing the new defaults onto it. Both stay in that instance's `data/`, separate from shared configuration; restoring inheritance for a conversation uses that instance's conversation defaults.

On the first start after upgrading an older installation, `data/config.yaml`, `data/policy.yaml`, `data/mcp_clients.yaml`, `skills/`, and drawing configuration are copied into `config/`. The `platforms` section of `data/config.yaml` moves into `data/platform_secrets.yaml`; existing destination files are not overwritten. The old files remain but do not receive later changes, so they cannot simply be used to roll back live data. Follow the [upgrade and rollback guide](deploy/UPGRADE-MULTI-PLATFORM.md) for backups, migration, and data checks.

## Deployment

### Before you start

Three things:

- A fresh Debian 12 / Ubuntu 22.04+ server with root access, 1 vCPU and 2 GB is enough
- A QQ account for the bot (you'll log in by scanning a QR code — don't use your personal account)
- A model API key, official or from a proxy

### Install

```bash
git clone <repo-url> /opt/kazebot
sudo /opt/kazebot/deploy/bootstrap.sh
```

The path has to be `/opt/kazebot` — it's hardcoded in the systemd units, and the script refuses to run from anywhere else.

It asks you nothing and runs straight through: dependencies and Docker, the dedicated user, the venv, config templates, the frontend build, the NapCat container, the systemd units, startup, and a final check that the engine really is alive. Five to ten minutes depending on your connection.

### Fill in the rest from the console

When it finishes it prints a URL with a token. The console only listens on localhost, so tunnel in from your own machine:

```bash
ssh -L 8765:127.0.0.1:8765 <your-user>@<server-ip>
```

Open the `http://127.0.0.1:8765/web/?token=...` it gave you and fill in four things, in order:

| | Page | What to enter | If you skip it |
|---|---|---|---|
| 1 | **Models and providers → Providers** | Model base_url, API key, model name | It won't talk |
| 2 | **Bot → Account & connections** | Scan the QR code to log the bot's QQ account in | No connection to QQ |
| 3 | **Platform → QQ → Permissions** | Your own QQ number, as an admin | Approvals are auto-denied and no administrator privileges are granted |
| 4 | **Platform → QQ → Channels** | The groups it's allowed to sit in | It won't speak in any group |

Saved or applied settings update live; the QQ page shows whether they have taken effect. Switching accounts restarts NapCat and temporarily disconnects QQ. Then go @ it in a group.

None of this is asked on the command line because the console validates it, shows you the values actually in effect, and lets you fix a mistake — whereas an API key typed into a shell also lands in your history.

### After it's up

```bash
systemctl status kazebot kazebot-qq     # both should be running
journalctl -u kazebot -f                # live logs
docker logs --tail 50 napcat            # the QQ side
```

If @-ing it in a group does nothing, check the allowlist under **Platform → QQ → Channels**, then check connection and configuration loading status.

For an existing deployment, start with the [upgrade and rollback guide](deploy/UPGRADE-MULTI-PLATFORM.md). It covers configuration migration, backups, instance identity, host files, and data verification.

For routine updates, `deploy/deploy.sh` fetches and runs **`git reset --hard origin/master`** by default. It backs up tracked local changes, but only restores files the target no longer tracks; local patches to files that remain tracked are not restored automatically. Preserve and review those patches before using it.

When `node_modules` exists, the script builds the frontend on every run; a lockfile change controls whether it runs `npm ci` first. If dependencies are absent, it warns and skips the build. It then restarts the main service and waits **up to 90 seconds for an engine process**. Verify each instance and its QQ adapter after the update: a running supervisor or the presence of an engine process alone does not prove that messages, history, and memory work.

The script does not install Python dependencies, update systemd units, restart every instance, or back up all runtime data; it is not a complete upgrade procedure.

To run more instances (up to nine per machine, one account each):

```bash
sudo /opt/kazebot/deploy/install_provision.sh <main instance name> <main instance QQ number>
```

The main instance is the one you already have; you pick its name. The old form with only a QQ number still works, and that number becomes the instance name. After that, use **Bot → Account & connections** to manage instances and shared configuration groups.

### What you end up with

```
/opt/kazebot/                 code, and also the main instance's workspace
/opt/kazebot-data/
  ├ instances.yaml            instance registry
  ├ stickers/                 sticker library shared across instances
  ├ shared/<group>/config/     configuration shared by a group's members
  └ <instance>/               one workspace per additional instance
      ├ config/               private directory or link to a shared group
      └ data/                 this instance's sessions, memory, and runtime data
```

Each workspace keeps its own `.env` and `data/`. Its `config/` is either a private directory or a link to its shared group's directory.

Production runs on systemd; Docker only hosts NapCat. Ports `8765` (console), `8080` (NoneBot), `8769` (forward bridge), and `6099` (NapCat) all listen on localhost only — **your firewall only needs to allow 22**.

Troubleshooting, unattended installs, and the manual steps if you'd rather not use the script are all in [deploy/INSTALL.md](deploy/INSTALL.md).

For instance index `N` (0–8), ports are supervisor `8765+10N`, NoneBot `8080+10N`, forward bridge `8769+10N`, and NapCat WebUI `6099+100N`.

Supervisor binds to localhost by default. To reach the console from outside, put it behind a tunnel with its own authentication rather than exposing the port directly.

## Development

```bash
python -m pytest
cd adapters/web/frontend
npm run test:run                                # frontend tests (Vitest)
npm run build                                   # TypeScript checks and production build
```

The decision logic on the QQ side — when to speak, who's allowed what, which files are accepted, which files may never be sent out — deliberately avoids importing NoneBot, so it can be unit-tested without a platform.

## Acknowledgements

- [HCPTangHY/Clonoth](https://github.com/HCPTangHY/Clonoth) — the framework this project is built on
- [qianzhuowo/Clonoth](https://github.com/qianzhuowo/Clonoth) — features from qianzhuowo's fork
- 秋雨·月落 — for the OneBot adapter
- [astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) — parts of the sticker feature draw on this

## License

[GPL-3.0](LICENSE)
