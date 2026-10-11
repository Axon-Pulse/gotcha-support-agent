# gotcha-support-agent

The support bot for deployed gotcha systems: PMs ask in Slack, and Claude Code, following the
`gotcha-support` skill, triages the site read-only and answers with what to tell the customer
and what to do.

| Path | What it is |
|---|---|
| [`support-agent-skill/`](support-agent-skill/SKILL.md) | **The brain.** The Claude skill: `SKILL.md`, the knowledge base (`kb/`: cases, system model, glossary, escalation) and the read-only scripts it runs (`scripts/`: triage, logs, source lookup). Also usable on its own from Claude Code. |
| [`slack-bridge/`](slack-bridge/README.md) | **The bot.** Carries Slack messages to headless Claude Code with the skill and posts the answers. Setup, Docker service, permissions and the Bash guard. |
| [`console/`](console/README.md) | **The console.** A web page on the tailnet: conversations with every step the bot took, a KB editor (cases, index rows, log signatures, reference docs), stats, sites, and Deploy. |
| [`legacy/`](legacy/README.md) | The retired LangGraph bot and its console, kept for reference. Nothing above uses it. |

Tests:

```sh
make -C slack-bridge test               # the bridge and the guard
support-agent-skill/scripts/selftest.sh # the skill's scripts and KB consistency
make -C console test                    # the console
```
