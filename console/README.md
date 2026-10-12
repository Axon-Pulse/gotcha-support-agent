# console

A web page for the Slack support bot (`../slack-bridge/` + `../support-agent-skill/`):

| Tab | What it shows | Reads / writes |
|---|---|---|
| **Conversations** | Every Slack thread: who asked, each question and answer, time and cost, and **what the bot did**: each script, its output, each case it read, each command the guard blocked. | Claude's transcripts + `slack-bridge/state/turns.jsonl` + `threads.json` (read) |
| **Cases** | The KB cases as a form: topics, symptoms, title, intro, sections in any order. On the same screen, the case's **SKILL.md index row** and its **log signatures**. Create, rename, delete. A file whose layout the form can't reproduce exactly opens as markdown. | `support-agent-skill/kb/cases/*.md`, `SKILL.md` §3, `scripts/signatures.txt` |
| **Reference docs** | `system-model.md`, `glossary.md`, `escalation.md` and all of `signatures.txt`, as text with a preview. `SKILL.md` is shown read-only. | `support-agent-skill/kb/`, `scripts/` |
| **Stats** | Questions, cost, time to answer, timeouts, per day; the cases read most. | the same as Conversations |
| **Systems** | The sites the bot can reach, how (Tailscale SSH or plain ssh), and what each ran when last triaged. Click a site for its **sensors**: each platform's nodes with type and address, from the site's own config, and whether each address answered at that triage. The radar's own IP, which isn't in the config, is shown too. **Run triage now** runs the same read-only triage the bot runs for a question. | `list_systems.sh`, `systems.tsv`, saved triages (`slack-bridge/state/triage/`, `/tmp/gotcha-triage/`) |
| **Bot config** | The container, the bot's settings from `slack-bridge/.env` (tokens and keys are named, never shown), its permissions and guard, and who changed what here. | read-only |

The header shows whether `selftest.sh` passes and whether the running bot has the files on
disk. **Deploy** runs `make -C slack-bridge update`.

## How an edit reaches the bot

1. **Save.**
   - The file is written straight to `support-agent-skill/`.
   - `selftest.sh` runs, and its result shows in the header. It checks that every case has
     an index row and every signature has a case.
   - Saving checks first. A signature must be a valid extended regex. It must not match an
     empty line, and must not contain `run_triage.sh`'s heredoc marker. An index cell can't
     hold a bare `|`.
   - If someone else saved the same file since you opened it, your save is refused. Nothing
     is overwritten.
2. **Deploy.**
   - The KB is copied into the bot's image, so nothing reaches Slack until the image is
     rebuilt.
   - The header pill shows "N changes not deployed". It compares `slack-bridge/skill_hash.sh`
     over the files with the label `make` stamped on the running image.
   - Deploy is refused while selftest fails, while a question is being answered, or while
     another deploy is running.
   - A deploy restarts the bot. It takes about a minute, and questions asked during it wait.
3. **Commit.**
   - The console doesn't touch git.
   - The deploy panel lists uncommitted files. Commit them so the next `git pull` doesn't
     lose them.

Every save, rename, delete and deploy is appended to `console/state/audit.jsonl` with the
tailnet login of whoever did it. The Bot config tab shows the latest ones.

## Run it

Install once, as the user that runs the bot (the console needs its `docker` access for
Deploy, and reads the bot's transcripts, which belong to that user):

```sh
make -C console install       # .venv, and console/.env from .env.example
$EDITOR console/.env          # CONSOLE_ALLOWED_USERS=you@company.com,pm@company.com
make -C console dev           # try it: http://127.0.0.1:8766, no login needed
```

### On the tailnet

The console listens on `127.0.0.1:8766` only. `tailscale serve` puts it on the tailnet at
`http://<this host>:8765`. Each request it forwards carries the caller's login in a `Tailscale-User-Login` header,
and the console lets in only the logins in `CONSOLE_ALLOWED_USERS`. A request with no login,
from this machine or from a tagged device, is refused, unless `CONSOLE_ALLOW_LOCAL=1`, which
is for `make dev` only.

```sh
sudo make -C console serve    # tailscale serve --bg --http 8765 http://127.0.0.1:8766
# open http://gotcha-server:8765 (or the full gotcha-server.<tailnet>.ts.net name)
```

Plain HTTP is fine inside the tailnet: Tailscale encrypts the traffic between devices. For an
`https://` address, enable HTTPS certificates for the tailnet (admin console → DNS) and use
`--https 8765` instead. Writes also need an `X-Console: 1` header and a JSON body, so a page on another site
can't post to it.

**What the login check relies on:**
- **Only `tailscale serve` can reach the console.** It binds to loopback, so nothing else on
  the network can.
- **The header can't be forged.** `tailscale serve` sets `Tailscale-User-Login` itself, and
  the check below confirms a client-sent one is replaced.
- **The bot can't call it.** The bot runs on this host's network (`network_mode: host`), so
  it could reach loopback. What stops it is that the bot can't run `curl` or fetch a URL:
  `bash_guard.py` and its settings refuse both.

After `make serve`, check the second point from another tailnet device:

```sh
curl -s -H 'Tailscale-User-Login: someone@else.com' http://gotcha-server:8765/api/auth/status
```

The answer must name *your* login, or say that you aren't allowed. It must not say
`someone@else.com`.

### Sign-in, roles and the bot's secrets

Being on `CONSOLE_ALLOWED_USERS` gets you to the sign-in form. To get past it you enter a
6-digit code from an authenticator app (Google Authenticator, Authy, 1Password, …).

| | viewer | admin (`CONSOLE_ADMINS`) |
|---|:-:|:-:|
| Use the console as before (conversations, cases, triage, deploy) | ✅ | ✅ |
| See which secrets are set, and who changed them when | ✅ | ✅ |
| **Reveal** a secret | | ✅ after a code; one code is good for 30 minutes, each value shows for 30 s |
| **Change or add** a secret | | ✅ with a code on every save |

The secrets are the entries of `slack-bridge/.env` that the Bot tab doesn't show: the Slack
tokens, the Anthropic key, and anything else put there. Saving one rewrites only that line
(the file stays `chmod 600`), then restarts the bot (`make -C slack-bridge restart`, which
recreates the container so it reads `.env` again). If questions are being answered, the
restart waits for them, up to 10 minutes. Every sign-in, reveal, change and refusal goes in
the audit log, never with a value.

**Setting someone up** (on this machine, with them next to you or on a call):

```sh
make -C console totp-enroll EMAIL=name@company.com
```

It prints a QR code to scan, and 8 one-use recovery codes to keep somewhere safe, then asks
for the first code from the app to confirm. Enrolment is only done here, never from the page:
any process on this host can send a `Tailscale-User-Login` header, so enrolling from the page
would let it enrol as someone who hasn't yet.

**Passkeys: no code to type.** Signed in, click **passkeys** in the header, then **Register a
passkey** (it asks for your app code first). The browser keeps it on this computer
(fingerprint, Windows Hello, Touch ID), on your phone (scan the QR code it shows) or on a
security key. After that: **Sign in with passkey** on the sign-in screen, **Use passkey** in the
code dialog when revealing, and an empty code field when saving uses the passkey. The app
code and recovery codes keep working as the fallback; `totp-reset` removes the passkeys too.

Browsers allow passkeys only on `https://` addresses or `localhost`, never on an IP address:
on the tailnet that means `tailscale serve --https` (enable HTTPS certificates for the tailnet
first). A passkey is bound to the host name it was made on (`CONSOLE_RP_ID` to fix one).

**Out of recovery codes:** `make -C console totp-recovery EMAIL=...` gives a fresh set (the
old ones stop working; the app entry stays).

**A lost phone:** sign in with a recovery code, or `make -C console totp-reset EMAIL=...`
(ends their sessions at once) and enrol again. `make -C console totp-list` shows who is set up.

Who is an admin is set in `console/.env` and takes effect when the console restarts. It is
not editable from the page, so a session someone has taken over can't make itself an admin.
Signing in lasts 12 hours (2 idle); restarting the console signs everyone out.

**Secrets tab, second part — system access: how to reach each system.** For every system and component in
`systems_inventory.yaml` (repo root): address, port and SSH user, which everyone signed in can
see, and the SSH password, which only an admin can reveal (same code and 30-minute window as
Secrets) or change (a code on every save). Addresses, ports and users are written to the YAML;
a password goes only to `secrets.local.env`, under the component's `password_env` name (given
one like `GOTCHA5_CAM_SSH_PW` if it had none). The page can't change `password_env`, so it
can't be pointed at another entry of that file, such as the Anthropic key. Both files are
gitignored and rewritten `chmod 600`. Point elsewhere with `CONSOLE_INVENTORY_FILE` and
`CONSOLE_DEVICE_SECRETS_FILE`.

**What this doesn't do:** `slack-bridge/.env`, `secrets.local.env` and `console/state/totp.json` stay plain text on
disk, owner-only. Anyone with a shell as this user can read both.

### Keeping it running

On gotcha-server it runs from `run_forever.sh`, which starts the console and starts it again
5 seconds after it stops. A line in the user's crontab starts it at boot, so it needs no sudo:

```
@reboot    /home/gotcha/gotcha-support-agent/console/run_forever.sh >/dev/null 2>&1
```

- **Log:** `console/state/console.log`.
- **Start it now:** `setsid console/run_forever.sh &`. Only one copy runs at a time, so this is
  harmless if one is already running.
- **Restart it, e.g. after a console code change:** `pkill -f 'uvicorn console.server:app'`.
  The loop brings it back.
- **Stop it for good:** `pkill -f console/run_forever.sh; pkill -f 'uvicorn console.server:app'`.

The console is not in Docker on purpose. Deploy rebuilds the bot's container, and doing that
from inside a container would need the host's Docker socket mounted into it, plus every path
the bot's build uses mounted at the same place.

### Or as a systemd service

If someone with sudo prefers systemd, use
`/etc/systemd/system/gotcha-support-console.service` instead of the crontab line, with
`YOURUSER` the bot host user:

```ini
[Unit]
Description=gotcha support console
After=network-online.target tailscaled.service

[Service]
User=YOURUSER
WorkingDirectory=/home/YOURUSER/gotcha-support-agent
EnvironmentFile=/home/YOURUSER/gotcha-support-agent/console/.env
ExecStart=/home/YOURUSER/gotcha-support-agent/console/.venv/bin/python -m uvicorn console.server:app --host 127.0.0.1 --port 8766
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```sh
sudo systemctl daemon-reload && sudo systemctl enable --now gotcha-support-console
journalctl -u gotcha-support-console -f
```

## Where the conversations come from

- **Claude's transcripts** hold the conversation itself. Each turn's question, the tool calls
  and their output, the answer, and the running cost are in
  `slack-bridge/state/claude/projects/-app-slack-bridge-workspace/<session>.jsonl` (the Docker
  service). A `make dev` run writes them to `~/.claude/projects/<workspace path>/` instead.
- **The bridge's `state/turns.jsonl`** adds what a transcript lacks:
  - who asked in Slack;
  - a turn that hit the time limit (it leaves no answer and no cost);
  - a turn still running, which Deploy waits for.

  A logged turn is matched to its transcript turn by time. Threads from before the log
  existed come from the transcripts alone, without who asked.

Slack users show as IDs (`U0…`): the bot's Slack app has no `users:read` scope.

## Files

| Path | What it is |
|---|---|
| `server.py` | The FastAPI app: routes, the tailnet login check, sign-in, the audit log. |
| `auth.py` | Roles, authenticator codes (TOTP), sessions; `python -m console.auth` enrols people. |
| `secrets_env.py` | The hidden entries of `slack-bridge/.env`: names for all, values for admins. |
| `passkeys.py` | Passkeys (WebAuthn): register, sign in, unlock reveals, confirm saves. |
| `access.py` | Each system's addresses, SSH users and passwords (`systems_inventory.yaml`, `secrets.local.env`). |
| `kb.py` | Cases ↔ parts, SKILL.md index rows, signatures, reference docs, `selftest.sh`. |
| `conversations.py` | Transcripts + turn log → threads, steps and stats. |
| `ops.py` | Bot config (allowlisted `.env` names), the container, Systems, Deploy and restart. |
| `sites.py` | A site's sensors from its newest saved triage; running a triage on demand. |
| `settings.py` | Paths and access, from `CONSOLE_*` variables with this checkout as the default. |
| `index.html` | The whole page: one file, no build step, no external scripts. |
| `tests/` | Every test runs on a copy of the skill. `page_harness.js` runs the page's script under node against answers captured from the real server and drives every tab. |
