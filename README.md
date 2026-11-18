# Yune bridge

The bridge is a small program you run on your own computer (macOS, Linux or Windows) so that you can work with
**Claude Code** and **Codex** on that machine from [Yune](https://yune.jp) → *Connections*: start sessions in your
projects, watch the agent's replies and steps, answer its questions, send files, talk to it by voice — from the browser
or your phone.

This repository is the complete source of the bridge that Yune installs and updates. Nothing else runs on your machine.

## How it works

- **Outgoing connection only.** The bridge opens one WebSocket to Yune (`/api/connections/link`) and keeps it alive.
  It does not listen on any public port: its local HTTP server binds to `127.0.0.1` on a random port and requires a
  random per-run token. Yune sends requests through that WebSocket; the bridge answers them.
- **Agents are yours.** The bridge drives the `claude` and `codex` CLIs that are already installed and logged in on
  your machine (Claude Agent SDK, `codex exec --json`). Your subscription, settings and skills are used as is.
- **Pairing.** `python -m claude_bridge.remote pair <site>` shows a one-time code; you enter it in Yune. The machine
  key is stored in `~/.yune-bridge/config.json` (mode 600); Yune keeps only its hash. "Disconnect machine" in Yune
  revokes the key and the bridge stops.

## What is sent to Yune — and what is not

Sent (so that you can see it in Yune):

- the session feed: your messages, the agent's replies, tool calls with their (truncated) input and output — e.g. the
  commands it runs and their results, file paths it edits, the questions it asks;
- files you upload to a session, and files the agent explicitly shows you (`send_file`);
- the list of projects (folder name, path, branch, last commit subject and time, number of changed files);
- machine info (host name, OS), which agents are installed and logged in, their models and subscription limits.

Not sent:

- your source code as a whole — only what appears in the session feed (the agent's tool output, which you see too);
- your API keys, tokens or agent credentials (`~/.claude`, `~/.codex` stay on the machine; login links and codes are
  only relayed to your browser while you log in);
- anything from folders outside the configured project roots, unless an agent in a session reads it.

Each session has an access mode, switchable at any time from the icon under the message field:

- **Full access** (default) — Claude Code in `bypassPermissions`, Codex without a sandbox, exactly as if you ran them
  in a terminal yourself.
- **Ask** — Claude Code asks you in Yune (Allow / Deny) before editing files or running commands; reading and searching
  need no approval. Codex runs in the `workspace-write` sandbox (edits only inside the project, no network), since
  `codex exec` cannot ask per action.
- **Read only** — Claude Code may only read and search; Codex runs in the `read-only` sandbox.

Only connect machines and projects you are comfortable an agent working in.

## Install

macOS / Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/ronskii2/yune-bridge/main/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/ronskii2/yune-bridge/main/install.ps1 | iex
```

The installer downloads the release archive from this repository, **checks its content hash against the build**
before installing (see below), creates a virtualenv in `~/.yune-bridge-app`, pairs the machine and enables autostart
(launchd, `systemd --user`, Task Scheduler). Python 3.11+ is required. Yune's own one-liner
(`curl -fsSL https://yune.jp/connect/install | sh`) runs the same script.

Manual run from a checkout:

```sh
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m claude_bridge.remote pair https://yune.jp
.venv/bin/python -m claude_bridge.remote run
```

A bridge started from a git checkout never updates itself — `git pull` is up to you.

## Versions and verification

A build is the first 12 hex digits of SHA-256 over the bridge files: for every file of the archive layout
(`claude_bridge/<module>.py` from the list `PACKAGE` in `claude_bridge/update.py`, and `requirements.txt`), sorted by
name, the hash is fed `name + "\0" + sha256(content)`. Every release is tagged with its build and carries
`yune-bridge.zip` with exactly these files plus `claude_bridge/build.txt`.

Check a checkout or an installed bridge yourself:

```sh
git checkout <build>
python3 -m claude_bridge.update --build       # prints the build of these files
python3 -m claude_bridge.update --check       # installed bridge: files match claude_bridge/build.txt
```

Yune shows the bridge build of each machine. When Yune has a newer build it asks the bridge to update; the bridge
waits until no session is running, downloads **the release of this repository tagged with that build**, recomputes
the hash of the archive content and refuses the update if it does not match. After the first install or update from
GitHub the bridge pins this source (`~/.yune-bridge/update-source`) and ignores any other source the site might
suggest — so the site cannot ship code that is not published here. (Delete that file to go back to updates from the
site.) A bridge running from a git checkout does not self-update at all.

The installer script itself is served by Yune at `/connect/install`; if you want to trust only this repository, run
it from `raw.githubusercontent.com` as shown above.

## Configuration

`~/.yune-bridge/config.json`:

| key          | meaning                                                                             |
|--------------|-------------------------------------------------------------------------------------|
| `roots`      | folders searched for projects (default `~/code`, `~/projects`, `~/Documents`, `~/dev`, `~`) |
| `folders`    | extra folders shown as projects even without git                                    |
| `keep_awake` | keep the machine awake while a session is running (default `true`)                  |
| `resolve`    | connect to the site at this IP instead of DNS (TLS is still checked by name)        |
| `host_dir`   | optional folder with a local extension module `host.py` (see `claude_bridge/extension.py`) |

Logs: `~/.yune-bridge/bridge.log` (macOS), `journalctl --user -u yune-bridge` (Linux).

## License

MIT — see [LICENSE](LICENSE).
