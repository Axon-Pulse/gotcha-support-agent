"""Where the console finds things, and who may use it. One object, read at call time, so a
test can point it at a temporary copy of the skill and the bridge with monkeypatch."""
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _csv(v: str) -> set[str]:
    return {x.strip().lower() for x in os.environ.get(v, "").split(",") if x.strip()}


def claude_project_slug(path: Path) -> str:
    """Claude Code keeps a project's transcripts under ~/.claude/projects/<slug>, where the
    slug is the project's absolute path with every non-alphanumeric turned into `-`."""
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


class Settings:
    def __init__(self):
        env = os.environ.get
        self.skill_dir = Path(env("CONSOLE_SKILL_DIR") or ROOT / "support-agent-skill")
        self.bridge_dir = Path(env("CONSOLE_BRIDGE_DIR") or ROOT / "slack-bridge")
        self.state_dir = Path(env("CONSOLE_STATE_DIR") or ROOT / "console" / "state")
        # Transcripts: the Docker service's (bind-mounted at slack-bridge/state/claude, where
        # the workspace is /app/slack-bridge/workspace), then a `make dev` run's on this host.
        dirs = env("CONSOLE_TRANSCRIPT_DIRS")
        self.transcript_dirs = ([Path(d) for d in dirs.split(":") if d] if dirs else [
            self.bridge_dir / "state" / "claude" / "projects" / "-app-slack-bridge-workspace",
            Path.home() / ".claude" / "projects" / claude_project_slug(self.bridge_dir / "workspace"),
        ])
        self.systems_tsv = Path(env("CONSOLE_SYSTEMS_TSV")
                                or Path.home() / ".local" / "state" / "gotcha-support" / "systems.tsv")
        self.container = env("CONSOLE_CONTAINER") or "gotcha-support-bot"
        self.deploy_cmd = ["make", "-C", str(self.bridge_dir), "update"]
        # Who may use it. Tailnet users by login (the Tailscale-User-Login header that
        # `tailscale serve` sets); a request without one is local, and refused unless asked for.
        self.allowed_users = _csv("CONSOLE_ALLOWED_USERS")
        self.allow_local = env("CONSOLE_ALLOW_LOCAL") == "1"

    @property
    def cases_dir(self) -> Path:
        return self.skill_dir / "kb" / "cases"

    @property
    def turns_file(self) -> Path:
        return self.bridge_dir / "state" / "turns.jsonl"

    @property
    def threads_file(self) -> Path:
        return self.bridge_dir / "state" / "threads.json"


S = Settings()
