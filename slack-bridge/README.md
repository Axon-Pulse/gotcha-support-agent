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
      │  claude -p "<message>" [--resume <thread's session>]      cwd: workspace/
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
| `tests/` | The guard's allow/deny table, and the bridge without Slack or Claude. |
| `state/threads.json` | Slack thread → Claude session (created at run time, gitignored). |

## Setup

Everything below is one-time, on one machine.

### 1. The host
An always-on Linux machine that is:
- **on the tailnet** and can reach every gotcha site, because the skill connects with `tailscale ssh`;
- able to reach **Slack** and the **Anthropic API** outbound.

No inbound port is needed.

```sh
# Claude Code (the bot runs it headless)
curl -fsSL https://claude.ai/install.sh | bash      # or your usual install
claude --version

git clone git@github.com:Axon-Pulse/gotcha-support-agent.git
cd gotcha-support-agent && git switch add-support-agent-skill
python3 -m venv slack-bridge/.venv
slack-bridge/.venv/bin/pip install -r slack-bridge/requirements.txt

# The skill reads gotcha30 source at each site's exact version
support-agent-skill/scripts/code.sh clone           # needs read access to gotcha30
```

**Claude credentials.** Set `ANTHROPIC_API_KEY` in the bot's environment. That is
simplest for an unattended service. Alternatively, log in once with `claude` as the user
that runs the bot.

### 2. Reaching the sites without a person
Interactive use of the skill can stop and ask a human, for example for a Tailscale browser
check or a password. The bot can't, so its access must work unattended.

Test it first, as the user that will run the bot:

    tailscale ssh axon-gotcha-3 true

It must return at once. If it asks you to approve in a browser, the Tailscale ACL `ssh`
rule from the bot host to the gotcha machines is `"action": "check"`. A tailnet admin
changes it to `"accept"` for the bot host, e.g. tagged `tag:support-bot`. A `check` rule
waits for a login that will never come, and every run times out.

**Plain SSH**, for machines without Tailscale SSH: install the bot's key for the login it
uses, and pin the host keys (`ssh <host> true` once, as the bot user). Set
`GOTCHA_SSH_USER` if the login name differs from the default.

**Which account it logs in as.** For internal use by PMs, the bot uses the logins
gotcha-server already has on the sites. With [strict mode](#strict-mode) on, it runs only
the skill's read-only scripts, and the guard is what holds it to that.

**Before customers get any access,** to the bot or to the site machines, switch those
logins to non-sudo, read-only users: no sudo, and docker reads only (a sudoers rule for
`docker ps/inspect/logs`, or a read-only socket proxy). After that the machine itself
refuses a write, even if the guard has a gap.

Check what the bot currently logs in as with `tailscale ssh <site> 'id'`. `root`, or
membership of `sudo` or `docker`, means full control.

### 3. The Slack app
Create it from `manifest.yaml`. Its header lists the three manual steps: the app token,
installing it, and inviting it to the channel.

### 4. Run it

```sh
export SLACK_BOT_TOKEN=xoxb-...  SLACK_APP_TOKEN=xapp-...
export SLACK_ALLOWED_CHANNELS=C0123456789      # fail-closed: unset means nobody
export ANTHROPIC_API_KEY=sk-ant-...
slack-bridge/.venv/bin/python slack-bridge/bridge.py
```

As a service, `/etc/systemd/system/gotcha-support-bot.service`:

```ini
[Unit]
Description=gotcha support Slack bot
After=network-online.target tailscaled.service

[Service]
User=support-bot
WorkingDirectory=/opt/gotcha-support-agent
EnvironmentFile=/etc/gotcha-support-bot.env
ExecStart=/opt/gotcha-support-agent/slack-bridge/.venv/bin/python slack-bridge/bridge.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

### Settings

| Variable | Default | Meaning |
|---|---|---|
| `SLACK_BOT_TOKEN`, `SLACK_APP_TOKEN` | — | Required. |
| `SLACK_ALLOWED_CHANNELS` | none | Channels the bot answers in. **Unset refuses everyone.** |
| `SLACK_ALLOWED_USERS` | any | Narrows further; required for DMs. |
| `SLACK_ALLOW_DMS` | `0` | `1` to answer DMs from allowed users. |
| `BRIDGE_EFFORT` | `medium` | Claude Code effort: `low` is faster, `high` is more thorough. |
| `BRIDGE_MODEL` | Claude Code default | e.g. `claude-opus-5-5`. |
| `BRIDGE_TIMEOUT_S` | `30` | One answer's wall-clock cap; the ssh sessions die with it, and a turn that hits it posts no answer. The triage's own limit is set 12s below it (`GOTCHA_TRIAGE_TIMEOUT`). |
| `BRIDGE_MAX_CONCURRENT` | `2` | Questions answered at once. |
| `BRIDGE_FOLLOWUPS` | `0` (strict) | `1` also lets the bot run its own read-only follow-up commands on a site. See [Strict mode](#strict-mode). |
| `BRIDGE_ALLOW_PING` | `1` | The triage may ping sensors (two packets per address, on the customer's subnets). `0` switches it off. |

### Strict mode

**On by default.** The bot runs the skill's scripts (`run_triage.sh`, `list_systems.sh`,
`code.sh`) and nothing it composed itself. When the triage doesn't settle a question, the
answer names the one next check as a step for the PM, with its exact command.

With `BRIDGE_FOLLOWUPS=1`, the bot may also run `tailscale ssh <site> '<command>'`
follow-ups of its own, as the skill describes in SKILL.md §5. The guard checks each one
against the read-only list below. That gets more questions settled in one go, but the
commands are written by the model at run time rather than reviewed in advance. Keep it
off until the bot logs in with read-only users.

## What the bot can and cannot do

The bot is held read-only by **independent layers**. Any one of them stopping a command
is enough. For internal use, layers 1 and 2 are in place. Layer 3 comes with the switch
to read-only users, before customer access.

1. **Claude Code settings** (`workspace/.claude/settings.json`):
   - permission mode `dontAsk`, so anything not allowed is refused and nobody is asked;
   - no Write, Edit, WebFetch, WebSearch or subagents;
   - reading `~/.ssh`, `~/.claude`, `.env` files and key files is denied.

   The bridge passes `--setting-sources project`, so settings in the bot user's
   `~/.claude` can't widen this.
2. **`bash_guard.py`**, on every Bash call. It allows:
   - the skill's `run_triage.sh`, `list_systems.sh` and `code.sh` (not `clone`);
   - `tailscale status|ping`;
   - only with `BRIDGE_FOLLOWUPS=1`: `tailscale ssh`/`ssh` to a host with **one**
     read-only command, parsed and checked. That means docker `ps`/`inspect`/`logs`,
     `grep`, `ls`, `ss`, `ip route get`, local `curl` GETs, `--print-config`, and similar.

   Denied: restarts, `make`, `sudo` (except the triage's `sudo -n docker`), `;` `&&` `>`
   `$(…)`, printing `.env` or site configs (camera passwords), tunnels, `--ping` when switched off
   (`BRIDGE_ALLOW_PING=0`), and anything it can't parse. A crash in the guard is a denial, because
   Claude Code runs the command when a hook fails any other way.
3. **The remote account** is read-only by its own permissions, once the logins are
   switched (setup step 2).

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

## Status of this sketch

**Tested:** `tests/` (`python -m pytest slack-bridge/tests -q`) covers:
- the guard's allow/deny table and its exit codes, including failing closed;
- the bridge's argv, result parsing, thread store, admission, and the ack-then-edit flow, with fakes for Slack and Claude.

**Not yet exercised end to end:** a real Slack workspace, a real headless Claude run in
`workspace/`, and a real site.

Before relying on it:
1. **Confirm the guard is wired**, on the bot host. The test command is harmless on
   purpose:

       cd slack-bridge/workspace
       claude -p "Run exactly this bash command: echo guard-test" \
         --permission-mode dontAsk --setting-sources project --output-format json

   The result must say the command was blocked by the read-only guard. If it prints
   `guard-test` instead, the hook is not running. Do not start the bot.
2. **Run it against one bench system** from a test channel.

**Known follow-ups:**
- **Reply shape location:** move it from `SLACK_PROMPT` into `SKILL.md` §8, so the skill answers the same way inside and outside Slack.
- **`signatures.txt` injection:** `run_triage.sh` pastes `signatures.txt` into a heredoc that runs on the site. A line equal to its end marker would run as shell there. Validate the file, or send it base64-encoded.
- **Rate limiting:** there's no per-user limit yet. The LangGraph gateway has one; add it if the channel gets busy.
- **Thread store:** `state/threads.json` grows without bound. Prune it by age.
