<div align="center">

# Clonoth

**An AI bot built to live in a group chat.**

[![License](https://img.shields.io/badge/License-GPL--3.0-1f6feb?style=flat-square)](LICENSE)
[![Python](https://img.shields.io/badge/Python-3.11%2B-1f6feb?style=flat-square)](#quick-start)
[![OneBot](https://img.shields.io/badge/OneBot-11-1f6feb?style=flat-square)](#features)
[![Tests](https://img.shields.io/badge/tests-145%20py%20%2B%2055%20ts-1f6feb?style=flat-square)](#development)

<a href="README.md"><img src="https://img.shields.io/badge/%E7%AE%80%E4%BD%93%E4%B8%AD%E6%96%87-57606a?style=for-the-badge" alt="简体中文"></a>
<a href="README.en.md"><img src="https://img.shields.io/badge/English-1f6feb?style=for-the-badge" alt="English"></a>

</div>

---

It reads the images and files people drop in chat, expands forwarded message trees in full, collects its own sticker library, writes its own tools, schedules its own cron jobs, and remembers who it talked to about what.

Anyone in the group can ask it to search, draw, or work something out — whatever comes up in conversation. Anything that touches the server, like running a command or editing a file, is admins only.

You mostly won't touch YAML — there's a web console, and the sixty-odd QQ settings apply within two seconds without a restart.

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

The **settings view** has twenty-odd pages in four groups: QQ (login and multi-account, group allowlist, when to speak, permissions, persona, memory, stickers, connection health), models (which provider, fallback chains, which channel handles vision, per-provider parameters), and engine (runtime diagnostics, approvals, nodes, tool authorization, drawing, skills, MCP, scheduled jobs).

### Multiple accounts

Up to nine QQ accounts on one machine. Two clicks in the console adds one, and it's ready in a minute or two. Sessions, memory, credentials, persona, and allowlists are per-account; only the sticker library is shared. Deleting archives the workspace by renaming rather than removing it.

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
| `adapters/onebot/` | Everything on the QQ side |
| `adapters/web/` | Console frontend |
| `providers/` | Model backends: OpenAI / Responses / Anthropic / Gemini / DeepSeek |
| `toolbox/` `tools/` | Tool runtime and external tool scripts |
| `stickers/` | Sticker library |
| `config/` | Node definitions and config templates |
| `data/` | Runtime data, never committed |
| `deploy/` | systemd units and deploy scripts |

Adding a model backend means adding one `providers/*.py`; it gets discovered automatically. Adding a tool means adding one `tools/*.py` that declares which permission gate it has to clear.

## Permissions and security

**Everyone uses it normally; nobody but an admin reaches the server.** Web search, image generation, math, vision, stickers — anything that belongs in a conversation, any member of the group can ask for, same as an admin. What's walled off is the server side: reading files, writing files, running commands, and restarting are refused outright for non-admins — not routed to an approval for someone to rubber-stamp, just not available. Images and files they send travel through the attachment path and never touch a tool.

**Admin operations that matter pop a card.** When something needs clearance, the bot DMs an approval card; quote it and reply "approve." If the admin list is empty, every approval is auto-denied rather than quietly allowed.

**The defaults are wide open. Tighten them before production.** The bundled `config/nodes/qq.orchestrator.yaml` grants `execute_command`, `write_file`, `manage_secret`, `create_or_update_tool`, and more — that's so it works out of the box, not a recommendation. Two places to tighten:

- **`tool_access` in the node YAML** — which tools this node can even see. Give it an allowlist, or set `none` to shut it all off.
- **`data/policy.yaml`** — which paths and which commands need approval, and which are refused. Four operations (read, write, execute, restart), each with a default verdict and its own exceptions.

**The server layer is the real backstop.** In production, systemd runs everything as a dedicated user with the whole filesystem read-only except this instance's workspace — the code directory isn't writable, so it can't modify its own source. That's the kernel enforcing it, not a string blocklist, which makes it the layer that actually holds. Think of the config above as "don't let it make mistakes," and this layer as "it couldn't even if it tried."

A few other things that are on by default: secrets stay out of the repo (`.env`, all of `data/`, and `config/qq.yaml` are gitignored — only templates ship); the model only ever sees aliases like `GroupA` and `UserB`, with the real numbers in a local lookup table; logs are redacted before they leave; and `.env`, config files, and private keys can't be sent out over QQ, not even by an admin.

## Quick start

You'll need Python 3.11+, Node.js 18+, and an OneBot 11 implementation — NapCat is the one to use.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp config.example.yaml data/config.yaml     # which model provider
cp policy.example.yaml data/policy.yaml     # permission policy
cp .env.example .env                        # secrets
cp config/qq.example.yaml config/qq.yaml    # QQ settings

cd adapters/web/frontend && npm ci && npm run build && cd -

python main.py     # supervisor + engine
python bot.py      # QQ adapter, in a second terminal
```

On first start, supervisor logs a console URL with a token in it — open that and configure the rest from there.

**Two fields decide whether it talks at all**: `channels.allowed_groups` in `config/qq.yaml` (the group allowlist — empty means it joins no conversation) and `permissions.admin_users` (the admin list — empty means nobody can manage it). Both default to empty on purpose.

## Configuration

| File | Covers | Applies |
|---|---|---|
| `data/config.yaml` | Which provider, fallback chains, which channel handles vision and compaction | Immediately |
| `data/policy.yaml` | Permission policy | Immediately |
| `config/qq.yaml` | The sixty-odd QQ switches | Within two seconds |
| `config/runtime.yaml` | Engine tuning, compaction thresholds, memory budget, cleanup | Immediately |
| `config/nodes/*.yaml` | Node definitions | Immediately |
| `.env` | Ports, secrets, workspace location | **Needs a restart** |

Breaking the QQ config won't take the bot down — on a parse failure it keeps running the last good copy and logs the error. To see what's actually in effect inside the process, check `GET /qq/state`.

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
| 1 | **Providers** | Model base_url, API key, model name | It won't talk |
| 2 | **Account** | Scan the QR code to log the bot's QQ account in | No connection to QQ |
| 3 | **Permissions** | Your own QQ number, as an admin | Every approval is auto-denied, admin commands stop working |
| 4 | **Channels** | The groups it's allowed to sit in | It won't speak in any group |

All of it applies immediately, no restart. Then go @ it in a group.

None of this is asked on the command line because the console validates it, shows you the values actually in effect, and lets you fix a mistake — whereas an API key typed into a shell also lands in your history.

### After it's up

```bash
systemctl status kazebot kazebot-qq     # both should be running
journalctl -u kazebot -f                # live logs
docker logs --tail 50 napcat            # the QQ side
```

If @-ing it in a group does nothing, the group almost certainly isn't in the allowlist — add it on the console's Channels page, live within two seconds.

To update, run `deploy/deploy.sh`: it pulls, rebuilds the frontend if the lockfile changed, restarts, and **watches for 90 seconds to confirm the engine actually came up**. Don't skip that check — with supervisor alive and the engine dead, systemd still reports running and nothing looks wrong from the outside.

To add more accounts (up to nine):

```bash
sudo /opt/kazebot/deploy/install_provision.sh <first account's QQ number>
```

After that the console's Account page grows a "multi-instance" section; adding one is two clicks.

### What you end up with

```
/opt/kazebot/                 code, and also the first account's workspace
/opt/kazebot-data/
  ├ stickers/                 sticker library shared across accounts
  └ <uin>/                    one workspace per additional account
```

Production runs on systemd; Docker only hosts NapCat. Ports `8765` (console), `8080` (NoneBot), `8769` (forward bridge), and `6099` (NapCat) all listen on localhost only — **your firewall only needs to allow 22**.

Troubleshooting, unattended installs, and the manual steps if you'd rather not use the script are all in [deploy/INSTALL.md](deploy/INSTALL.md).

For multiple accounts, ports derive from the instance index: supervisor `8765+10N`, and so on.

Supervisor binds to localhost by default. To reach the console from outside, put it behind a tunnel with its own authentication rather than exposing the port directly.

## Development

```bash
pytest                                          # 145 test files
cd adapters/web/frontend && npm run test:run    # 55 frontend tests
```

The decision logic on the QQ side — when to speak, who's allowed what, which files are accepted, which files may never be sent out — deliberately avoids importing NoneBot, so it can be unit-tested without a platform.

## Acknowledgements

- [HCPTangHY/Clonoth](https://github.com/HCPTangHY/Clonoth) — the framework this project is built on
- [qianzhuowo/Clonoth](https://github.com/qianzhuowo/Clonoth) — features from qianzhuowo's fork
- 秋雨·月落 — for the OneBot adapter
- [astrbot_plugin_smart_imagechat_hub](https://github.com/QingchenWait/astrbot_plugin_smart_imagechat_hub) — parts of the sticker feature draw on this

## License

[GPL-3.0](LICENSE)
