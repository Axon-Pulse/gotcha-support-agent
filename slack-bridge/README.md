# slack-bridge

A Slack bot that answers PMs about deployed gotcha systems. It runs Claude Code
headless with the `gotcha-support` skill (`../support-agent-skill/`). **The skill is the
brain; this folder only carries messages in and answers out.**

```
PM: @gotcha-support the map is empty on gotcha 3
      │  Slack Socket Mode (outbound only)
      ▼
bridge.py ─── posts ":mag: Checking…" in the thread straight away
      │
      │  claude -p [--resume <thread's session>]   message on stdin   cwd: workspace/
      │    skill:        workspace/.claude/skills/gotcha-support -> ../support-agent-skill
      │    permissions:  workspace/.claude/settings.json (dontAsk: no approvals, no writes)
      │    every Bash:   bash_guard.py (read-only checks only, fails closed)
      │    reaches site: tailscale ssh <host> — which is why this runs on the tailnet
      ▼
edits "Checking…" into the answer:

  *axon-gotcha-3: the map is showing demo data, not the real sensors*
  *Tell the customer:* The display lost its link to the sensors after this morning's
  update and fell back to sample data. We're rolling the update back now.
  *You do:*
  1. Roll back the image — `make rollback TAG=<previous tag>` then `make up`
  2. …
  *Confirm fixed:* the map is centred on the site again.
  *Confidence:* high · verified on the machine · sensors and network ruled out.
  _41s · 6 steps · $0.31_
```

A reply in the same thread continues the same Claude session, so "and the camera?" gets
an answer built on what was already found. A new top-level message starts fresh.

## Files

| Path | What it is |
|---|---|
| `bridge.py` | The whole bot: admission, "Checking…", one headless Claude turn, the answer. |
| `bash_guard.py` | PreToolUse hook. Allows the skill's scripts and read-only remote commands; denies everything else, including its own crashes. |
| `workspace/.claude/settings.json` | The bot's permission surface: which tools, which paths are off limits, the hook. |
| `workspace/.claude/skills/gotcha-support` | Symlink to the skill, so Claude Code loads it in this workspace. |
| `manifest.yaml` | The Slack app, as a paste-in manifest. |
| `tests/` | The guard's allow/deny table, and the bridge without Slack or Claude. `GOTCHA_LIVE_CLAUDE=1 pytest tests/test_live_permissions.py` also runs the real `claude` against canary files, to check the read allow-list (about ten model calls). |
| `state/threads.json` | Slack thread → Claude session (created at run time, gitignored). |

## Setup

One-time, on one machine. Steps 1 to 3 collect the credentials, 4 installs, 5 checks, 6 runs, 7 makes it a service.

### 0. Who needs access to what

| System | What for | Access needed |
|---|---|---|
| Anthropic Console | Create the API key; billing | Org admin, or a role allowed to create keys |
| Slack | Create the app, install it, invite it to a channel | Any member can create it; a workspace admin often has to approve the install |
| GitHub | The host clones `gotcha-support-agent` and `gotcha30` (private) | Read access with your own key; repo admin only for optional deploy keys |
| The bot host | Install and run the bot | A login; `sudo` only for the systemd service (step 7) |
| Tailscale | Only if SSH to a site asks for a browser | Tailnet admin |
| Site machines | Nothing to install or change | None. The bot uses the logins the host already has (see [Reaching the sites](#reaching-the-sites-without-a-person)) |

The host is an always-on Linux machine that is **on the tailnet** (the skill connects to
sites with `tailscale ssh`, or plain `ssh` over the tailnet name) and can reach **Slack**
and the **Anthropic API** outbound. No inbound port is needed.

### 1. Anthropic API key
1. Sign in at [console.anthropic.com](https://console.anthropic.com) with the company account.
2. **Settings → API Keys → Create Key**, named `gotcha-support-bot`.
3. Copy it: it is shown once. This is `ANTHROPIC_API_KEY` (`sk-ant-…`).
4. Check **Settings → Billing** has credit. Every answer ends with its cost, e.g. `41s · 6 steps · $0.31`.

Alternatively, log in once with `claude` as the user that runs the bot. The API key is
simpler for an unattended service.

### 2. Slack app
1. [api.slack.com/apps](https://api.slack.com/apps) → **Create New App → From a manifest**, pick the workspace, choose YAML, paste all of `manifest.yaml`.
2. **App token:** Basic Information → App-Level Tokens → Generate Token and Scopes, name it `socket`, scope `connections:write`. Copy the `xapp-…` token: `SLACK_APP_TOKEN`.
3. **Bot token:** Install App → Install to Workspace (or request approval). Copy the Bot User OAuth Token, `xoxb-…`: `SLACK_BOT_TOKEN`.
4. **Channel:** create a test channel such as `#gotcha-support-test` and run `/invite @gotcha-support` in it.
5. **Channel ID:** click the channel name → **About**; the ID (`C0…`) is at the bottom: `SLACK_ALLOWED_CHANNELS`.

The manifest gives the bot `app_mentions:read`, `chat:write` and `im:history`, the
`app_mention` and `message.im` events, and Socket Mode (the host connects out to Slack; no
public URL). It can't read other channel messages or post where it wasn't invited. The
tokens, the install and the invite are the manual parts above.

### 3. GitHub access for the host
The host clones this repo and `Axon-Pulse/gotcha30` (the skill reads gotcha30 source at each site's exact version).

```sh
ssh-keygen -t ed25519 -C gotcha-server
cat ~/.ssh/id_ed25519.pub          # GitHub → Settings → SSH and GPG keys → New SSH key
ssh -T git@github.com              # must greet you by name
```

A read-only deploy key per repo is cleaner later (repo admin needed, and an
`~/.ssh/config` alias, since GitHub won't take one deploy key on two repos).

### 4. Install
Run as the user the bot will run as.

```sh
curl -fsSL https://claude.ai/install.sh | bash      # or your usual install
~/.local/bin/claude --version

git clone git@github.com:Axon-Pulse/gotcha-support-agent.git
cd gotcha-support-agent && git switch add-support-agent-skill
python3 -m venv slack-bridge/.venv
slack-bridge/.venv/bin/pip install -r slack-bridge/requirements.txt

support-agent-skill/scripts/code.sh clone           # needs read access to gotcha30
```

"Headless Claude" is the normal `claude` command run with `-p`: one question in, one
answer out. `bridge.py` runs it once per Slack message, inside `slack-bridge/workspace/`,
where the skill and the safety settings live. You never start it yourself.

### 5. Check each piece on its own
So a failure points at one thing. Run these as the user that will run the bot.

1. **Claude works with the key.** Should print `ok` (the first call can take a minute):

       export ANTHROPIC_API_KEY=sk-ant-...
       ~/.local/bin/claude -p "say ok" </dev/null

   A warning that claude.ai connectors are disabled because `ANTHROPIC_API_KEY` takes
   precedence is harmless: the machine is also logged in to claude.ai, and the bot uses the key.
2. **Every site can be reached with nobody at the keyboard.** See
   [Reaching the sites](#reaching-the-sites-without-a-person) below. `support-agent-skill/scripts/list_systems.sh`
   shows which transport each site uses (`via tailscale` or `via ssh`).
3. **The triage works.** It says how it connected, then prints about 200 lines ending in `=== end of triage ===`:

       support-agent-skill/scripts/run_triage.sh axon-gotcha-3

4. **The guard is switched on.** `echo` isn't on the allowlist, so it must be refused:

       cd slack-bridge/workspace
       ~/.local/bin/claude -p "Run exactly this bash command: echo guard-test" \
         --permission-mode dontAsk --setting-sources project --output-format json
       cd ../..

   The result must say the command was **blocked by the read-only guard**. If it prints
   `guard-test`, the hook isn't running. Don't start the bot.

#### Reaching the sites without a person
Interactive use of the skill can stop and ask a human, for example for a Tailscale browser
check or a password. The bot can't, so its access must work unattended.

- **Sites marked `via tailscale`:** `tailscale ssh <site> true` must return at once. If it
  prints a login link, the Tailscale ACL `ssh` rule from the bot host to the gotcha machines
  is `"action": "check"`, which waits for a login that never comes, so every run times out.
  A tailnet admin adds a rule with `"action": "accept"` for the bot host, e.g. tagged
  `tag:support-bot`. Tagging changes who owns the machine on the tailnet, so let the admin decide how.
- **Sites marked `via ssh`** don't run Tailscale SSH (`tailscale ssh` fails there with *Host
  key verification failed*; that is expected). They are reached with plain `ssh` over the
  tailnet name. Set each up once, with that site's login (here `gotcha`):

      ssh gotcha@axon-gotcha-3 true             # accept the fingerprint, type the password if asked
      ssh-copy-id gotcha@axon-gotcha-3          # only if it asked for a password
      ssh -o BatchMode=yes gotcha@axon-gotcha-3 true   # the real test: no prompt, returns at once

  Set `GOTCHA_SSH_USER` in the service environment if the login differs from the bot's local
  user name. It is the host's own setting: a message can't change it, and the guard refuses
  it on a command. *Permission denied* after that means the wrong login name.

**Which account it logs in as.** For internal use by PMs, the bot uses the logins
gotcha-server already has on the sites. It runs only the skill's read-only scripts, against
the gotcha sites only, and the guard is what holds it to that.

**Before customers get any access,** to the bot or to the site machines, switch those
logins to non-sudo, read-only users: no sudo, and docker reads only (a sudoers rule for
`docker ps/inspect/logs`, or a read-only socket proxy). After that the machine itself
refuses a write, even if the guard has a gap.

Check what the bot currently logs in as with `tailscale ssh <site> 'id'`. `root`, or
membership of `sudo` or `docker`, means full control.

### 6. First run, in the foreground
Ctrl-C stops it.

```sh
cp slack-bridge/.env.example slack-bridge/.env && chmod 600 slack-bridge/.env   # once; paste the tokens in
set -a; . slack-bridge/.env; set +a
slack-bridge/.venv/bin/python slack-bridge/bridge.py
```

`SLACK_ALLOWED_CHANNELS` fails closed: unset means nobody is answered. `slack-bridge/.env` is
gitignored and the same file the Docker service reads (step 7).

It logs `bridge up: … mode=strict` and keeps running. In the test channel:
- `@gotcha-support what does end_on_first_complete do?` is answered from the KB and source, without touching a site.
- `@gotcha-support is everything ok on axon-gotcha-3?` runs a real triage: "Checking…" first, then the answer.

### 7. Run as a service, with Docker
It starts at boot, restarts on failure, and reads its credentials from `slack-bridge/.env`.
Stop the foreground bot first: two bots on the same app token split the questions between them.

```sh
cd slack-bridge
make check      # .env filled in, tailscaled up, ~/.ssh and the gotcha30 clone present, uid matches
make build
make run        # refuses while a foreground bridge.py is running
make logs       # expect "bridge up: …"
```

After a code or KB change (or a `git pull`): `make update`. The skill, KB and guard are copied
into the image, so a KB change needs it too. After editing `.env`: `make restart`. Also
`make stop`, `make status`, `make shell`, `make test`, and `make dev` (the foreground run with
`.env`, no Docker). `make` alone lists them.

The image holds `bridge.py`, the skill and Claude Code (pinned by `CLAUDE_VERSION` in the
`Dockerfile`). Everything else is borrowed from the host, so steps 3 and 5 still apply, as the
host user:
- **Tailscale:** the host's `tailscaled` socket and `tailscale` CLI, on the host's network
  (`network_mode: host`), so the tailnet names resolve as they do on the host. No port is opened.
- **SSH:** the host user's `~/.ssh`, read-only. The container user has the same uid (`BOT_UID`,
  default 1000; check with `id`), or ssh refuses the key. Its login name is `gotcha`; set
  `GOTCHA_SSH_USER` if the sites use another.
- **gotcha30 source:** `~/.cache/gotcha-support/gotcha30` (create it with `code.sh clone` first;
  if it's missing, Docker creates an empty root-owned directory there).
- **Site versions:** `~/.local/state/gotcha-support`, shared with runs of the skill on the host.

Claude's sessions and the thread map live in Docker volumes (`claude`, `threads`), so threads
survive a restart or rebuild.

### 7b. Or as a systemd service, without Docker
Reads its credentials from a root-only file. Needs `sudo`. Replace `YOURUSER` with the login
the bot runs as: its `~/.ssh` (keys, pinned host keys) and Claude install live there.

1. **The credentials file:**

       sudo tee /etc/gotcha-support-bot.env >/dev/null <<'EOF'
       SLACK_BOT_TOKEN=xoxb-...
       SLACK_APP_TOKEN=xapp-...
       SLACK_ALLOWED_CHANNELS=C0...
       ANTHROPIC_API_KEY=sk-ant-...
       CLAUDE_BIN=/home/YOURUSER/.local/bin/claude
       # only if the site login differs from YOURUSER:
       # GOTCHA_SSH_USER=gotcha
       EOF
       sudo chmod 600 /etc/gotcha-support-bot.env

   `CLAUDE_BIN` matters: a service doesn't have `~/.local/bin` on its `PATH`, so without it
   the bot can't find `claude`. Add any setting from the table below to this file too.

2. **The unit,** `/etc/systemd/system/gotcha-support-bot.service`:

   ```ini
   [Unit]
   Description=gotcha support Slack bot
   After=network-online.target tailscaled.service

   [Service]
   User=YOURUSER
   WorkingDirectory=/home/YOURUSER/gotcha-support-agent
   EnvironmentFile=/etc/gotcha-support-bot.env
   ExecStart=/home/YOURUSER/gotcha-support-agent/slack-bridge/.venv/bin/python slack-bridge/bridge.py
   Restart=on-failure

   [Install]
   WantedBy=multi-user.target
   ```

3. **Start it and watch the log:**

       sudo systemctl daemon-reload
       sudo systemctl enable --now gotcha-support-bot
       journalctl -u gotcha-support-bot -f

4. **Update later:** `cd ~/gotcha-support-agent && git pull`, then `sudo systemctl restart gotcha-support-bot`.

What is remembered where: the Slack tokens and API key in `/etc/gotcha-support-bot.env`;
the SSH keys and pinned host keys in the bot user's `~/.ssh`; the Tailscale login in
`tailscaled`; the Slack thread to Claude session map in `slack-bridge/state/threads.json`.
A dedicated service user (e.g. `support-bot`) works too, but then do steps 3 and 5 as that user.

### When something fails

| You see | Cause | Fix |
|---|---|---|
| "This bot has no allowlist configured, so it is refusing everyone." | `SLACK_ALLOWED_CHANNELS` is unset | Set it to the channel ID. Unset refuses everyone on purpose. |
| "This channel is not approved for the support bot." | Asked in a channel not on the list | Add its ID, comma-separated |
| No reply at all | Bot not invited, or the process isn't running | `/invite @gotcha-support`; check `docker compose logs` (or `journalctl -u gotcha-support-bot`) |
| Docker: `Permission denied (publickey)` or git "dubious ownership" | Container uid differs from the owner of `~/.ssh` / the clone | Set `BOT_UID`/`BOT_GID` in `.env` to `id -u`/`id -g`, rebuild |
| "No answer within 30s" | Slow site link, or a site waiting for a password or browser approval, or `BRIDGE_TIMEOUT_S` too low | Run check 5.2 for that site by hand; raise `BRIDGE_TIMEOUT_S` |
| `tailscale ssh`: "Host key verification failed" | That site doesn't run Tailscale SSH | Expected. Use plain ssh (check 5.2); the skill does this by itself. |
| Service fails: `claude` not found | `CLAUDE_BIN` missing | Add it to `/etc/gotcha-support-bot.env` |
| Answer says a check was blocked | The guard refused a command the model wrote | Expected. The answer gives the PM that check as a step. |
| The bridge exits at start: "the Bash guard did not block…" or "blocked the skill's own…" | The guard can't run (no `python3`, a syntax error, a moved file), or it blocks everything | Fix the guard. The bridge won't start with Bash unguarded. `python3` must be 3.10 or newer. |

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | — | Required. |
| `SLACK_ALLOWED_CHANNELS` | none | Channels the bot answers in. **Unset refuses everyone.** |
| `SLACK_ALLOWED_USERS` | any | Narrows further; required for DMs. |
| `SLACK_ALLOW_DMS` | `0` | `1` to answer DMs from allowed users. |
| `BRIDGE_EFFORT` | `medium` | Claude Code effort: `low` is faster, `high` is more thorough. |
| `BRIDGE_MODEL` | `claude-sonnet-5-5` | e.g. `claude-opus-5-5`; empty uses Claude Code's own default. |
| `BRIDGE_TIMEOUT_S` | `30` | One answer's wall-clock cap; the ssh sessions die with it, and a turn that hits it posts no answer. The triage's own limit is set 12s below it (`GOTCHA_TRIAGE_TIMEOUT`). |
| `BRIDGE_MAX_CONCURRENT` | `2` | Questions answered at once. |
| `BRIDGE_ALLOWED_HOSTS` | `axon-gotcha-[0-9]+` | Regex (whole name) for the machines the bot may reach. The guard refuses any other host, and `list_systems.sh` lists only these. |
| `CLAUDE_BIN` | `claude` | Path to the `claude` binary. Set it for a service. |
| `GOTCHA_SSH_USER` | the bot's login | Login on the sites, if it differs. Set it here; a message can't. |
| `BRIDGE_ALLOW_PING` | `1` | The triage may ping sensors (two packets per address, on the customer's subnets). `0` switches it off. |

### What it runs on a site

The bot runs the skill's scripts (`run_triage.sh`, `remote_logs.sh`, `list_systems.sh`,
`code.sh`) and nothing it composed itself: no `ssh`, no `tailscale ssh`. `remote_logs.sh <host>
<container> [lines] [--grep REGEX]` is the one way it reads a container's log: a fixed
`docker logs --tail`, at most 500 lines, redacted like the triage, never following. When the
triage doesn't settle a question, the answer names the one next check as a step for the PM, with
its exact command.

There used to be an opt-in mode (`BRIDGE_FOLLOWUPS=1`) that let the bot run its own read-only
commands on a site. It is gone: its list of "read-only" commands let through file writes,
POSTs and secret reads, and the commands were written by the model at run time.

## What the bot can and cannot do

The bot is held read-only by **independent layers**. Any one of them stopping a command
is enough. For internal use, layers 1 and 2 are in place. Layer 3 comes with the switch
to read-only users, before customer access.

1. **Claude Code settings** (`workspace/.claude/settings.json`):
   - permission mode `dontAsk`, so anything not allowed is refused and nobody is asked;
   - no Write, Edit, WebFetch, WebSearch or subagents. (Other skills can still load, because an
     allow rule doesn't limit them; they can't do anything the denied tools would be needed for.)
   - reads are an **allow-list**: the workspace, the skill's real directory and
     `/tmp/gotcha-triage`. Every other path on the host is refused, `.env` files, `~/.netrc`
     and key files included. (A deny-list can't do this: patterns such as `**/.env` only match
     under the workspace, so a `.env` anywhere else stayed readable.) The skill is a symlink and
     a read is judged on the path it resolves to, so the bridge adds the skill's absolute path
     with `--settings` (`bridge.permission_overlay`), plus absolute denies for secret file names
     inside it;
   - the Slack message goes to `claude` on **stdin**, never as an argument. As an argument, a
     message such as `--allowedTools=Bash` would be parsed as a flag.

   The bridge passes `--setting-sources project`, so settings in the bot user's
   `~/.claude` can't widen this.
2. **`bash_guard.py`**, on every Bash call. It allows:
   - the skill's `run_triage.sh`, `list_systems.sh` and `code.sh` (not `clone`; the version,
     file and line range it is given must be plain names and numbers, because `code.sh` puts
     them into a `sed` script and sed's `e` command runs a shell command);
   - `tailscale ping <site>`, `tailscale ip [<site>]`, `tailscale version`.

   Every host named must match `BRIDGE_ALLOWED_HOSTS` (`axon-gotcha-<number>`): not a laptop,
   a server, or any other machine on the tailnet, and never a name that starts with `-`.
   `tailscale status` is refused because it lists the whole tailnet; `list_systems.sh` lists
   only the allowed sites. A script's environment is not the model's to set: only
   `GOTCHA_CONFIG` (a `configs/…` path), `GOTCHA_SSH` and `GOTCHA_TRIAGE_TIMEOUT`, with those
   shapes.

   Denied: `ssh`, `tailscale ssh`, restarts, `make`, `sudo`, `;` `&&` `>` `$(…)`, `--ping` when
   switched off (`BRIDGE_ALLOW_PING=0`), and anything it can't parse.

   **If the guard can't run, the command is blocked, not allowed.** Claude Code runs a command
   when a hook exits with anything but 2, so `settings.json` runs the guard as
   `python3 bash_guard.py || exit 2`, and the bridge checks at start that the hook blocks
   `rm -rf /` and passes the skill's own scripts. It refuses to start otherwise.
3. **The remote account** is read-only by its own permissions, once the logins are
   switched (see [Reaching the sites](#reaching-the-sites-without-a-person)).

The Slack tokens are stripped from the environment Claude runs in.

Every fix goes into the answer as a step **for the PM**. The bot never runs one.

## Compared with the LangGraph bot on `main`

| | This bridge + skill | LangGraph bot (`slack_app.py`, `graph.py`) |
|---|---|---|
| Code to own | ~300-line bridge + ~300-line guard; the skill is markdown + bash | ~8k lines: graph, agents, tools, console, Slack gateway |
| How it diagnoses | Claude follows the skill: one triage bundle, KB cases, gotcha30 source **at the site's exact version** | Fixed agents calling fixed tools; only what's been coded as a tool |
| Adding a check or a case | Edit `SKILL.md`, a case file or `signatures.txt` | New tool module + agent wiring + tests |
| Read-only enforcement | Settings + a command guard + the remote account | Fixed command table in code + inventory-only hosts + no write tools |
| PM answer shape | In the bridge prompt (`SLACK_PROMPT`) | Needs `REPORT_SCHEMA` + Slack formatting changes (not done) |
| Speed | One Claude session; the skill's fast path; effort configurable | ~15 sequential model calls today (routing + agents + synthesis + rewrite) |
| Follow-ups in a thread | Resumes the same session | `conversation.py` routes follow-up vs new run |
| Console, approvals, KB editor | None: the KB is files in git | Web console with approvals, traces, inventory editor |
| Same either way | Slack app, a tailnet host, SSH access to each site, a read-only account on the gotcha machines | ← |

## Status

**Running.** The bridge has been run against a real Slack workspace, with headless Claude
in `workspace/` and the skill's triage against a real site (`axon-gotcha-4`).

**Unit tests:** `python -m pytest slack-bridge/tests -q` covers:
- the guard's allow/deny table and its exit codes, including failing closed;
- the bridge's argv, result parsing, thread store, admission, and the ack-then-edit flow, with fakes for Slack and Claude.

`test_the_triage_payload_only_reads` currently fails: its "no file redirects" check
matches the `<x>` in an echo string in `triage_remote.sh`, not a real write.

**Before customers get access:**
1. **Confirm the guard is wired** on the bot host (setup check 5.4). Running the bot does
   not prove the hook fires.
2. **Switch the site logins to read-only users** (see [Reaching the sites](#reaching-the-sites-without-a-person)).

**Known follow-ups:**
- **Reply shape location:** move it from `SLACK_PROMPT` into `SKILL.md` §8, so the skill answers the same way inside and outside Slack.
- **`signatures.txt` injection:** `run_triage.sh` pastes `signatures.txt` into a heredoc that runs on the site. A line equal to its end marker would run as shell there. Validate the file, or send it base64-encoded.
- **Rate limiting:** there's no per-user limit yet. The LangGraph gateway has one; add it if the channel gets busy.
- **Thread store:** `state/threads.json` grows without bound. Prune it by age.
