"""A site's live state and connection tests, for the Systems tab. Read-only, and no secrets.

- ping(host): tailscale ping from this server: the round trip, and whether the link is direct
  or relayed (DERP, slower).
- check(host): ping, then on the site its gateway's own health summary (total, healthy,
  degraded, critical and offline nodes, release, uptime: the status-bar pill of the GUI) and
  the nodes that have an open health event. Kept for a minute, so a page full of blocks
  doesn't reconnect to every site on each reload; a second request waits for the first.
- speed(host, "link"): throughput between this server and the site, each way, over the tailnet.
- speed(host, "internet"): the site's own internet, measured on the site (Cloudflare's speed
  test): download, upload and the connect time. Time-boxed, so it uses at most ~70 MB.

Each connects as a triage does (the skill's lib_ssh.sh picks tailscale ssh or plain ssh) and
runs one fixed script there; nothing a user types reaches the site.
"""
import json
import re
import shlex
import subprocess
import threading
import time
from contextlib import contextmanager

from console.settings import S

CHECK_FRESH_S = 60
SPEED_S = 5                      # each direction of the link test sends for this long
LINK_LIMIT_S = 2 * SPEED_S + 40  # the whole link test, connecting included
_checks: dict[str, tuple[float, dict]] = {}
_check_locks: dict[str, threading.Lock] = {}
_speed_busy: set[str] = set()
_lock = threading.Lock()

# Run on the site with its own python3 (stdlib only). The gateway (127.0.0.1:8080) sends its
# summary with every update once a client subscribes; the smallest subscription asks for it.
HEALTH_PY = r'''
import base64, json, os, socket, struct, urllib.request
s = socket.create_connection(("127.0.0.1", 8080), timeout=8)
s.sendall(("GET /ws HTTP/1.1\r\nHost: 127.0.0.1:8080\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
           "Sec-WebSocket-Key: " + base64.b64encode(os.urandom(16)).decode()
           + "\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
f = s.makefile("rb")
if b" 101 " not in f.readline():
    raise SystemExit("the gateway refused the connection")
while f.readline() not in (b"\r\n", b""):
    pass

def send(data):   # one masked text frame, as a client must send it
    mask, n = os.urandom(4), len(data)
    head = bytes([0x81, 0x80 | n]) if n < 126 else bytes([0x81, 0xFE]) + struct.pack(">H", n)
    s.sendall(head + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

def frame():
    head = f.read(2)
    if len(head) < 2:
        raise SystemExit("the gateway closed the connection")
    n = head[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", f.read(2))[0]
    elif n == 127:
        n = struct.unpack(">Q", f.read(8))[0]
    return head[0] & 0x0F, head[0] & 0x80, f.read(n)

def message():   # the next data message, its fragments joined; pings and pongs skipped
    kind, data = None, b""
    while True:
        op, fin, payload = frame()
        if op == 8:
            raise SystemExit("the gateway closed the connection")
        if op < 8:
            kind, data = kind or op, data + payload
            if fin:
                return kind, data

def find(o):   # wherever the update keeps it
    if isinstance(o, dict):
        if isinstance(o.get("system_health"), dict):
            return o["system_health"]
        return next((r for r in map(find, o.values()) if r), None)

send(json.dumps({"type": "subscribe_viewport", "bbox4326": [0, 0, 0.001, 0.001],
                 "max_rate_hz": 1, "channels": []}).encode())
for _ in range(10):
    kind, data = message()
    if kind == 1 and (summary := find(json.loads(data))):
        break
else:
    raise SystemExit("the gateway sent no health summary")
url = "http://127.0.0.1:8080/api/v1/events?kinds=health&states=open&limit=100"
try:
    events = json.load(urllib.request.urlopen(url, timeout=5)).get("events", [])
except Exception:   # an older gateway has no events: the summary still stands
    events = []
print(json.dumps({"summary": summary, "problems": [
    {"node": e.get("node_name"), "status": e.get("worst_status")} for e in events]}))
'''

# The site's internet, on the site: time-boxed curl runs against Cloudflare's speed test.
INTERNET_SH = r'''
curl -s -o /dev/null --max-time 8 -w '%{size_download} %{time_total} %{time_starttransfer} %{time_connect}\n' \
  'https://speed.cloudflare.com/__down?bytes=50000000'
head -c 20000000 /dev/zero | curl -s -o /dev/null --max-time 8 -w '%{size_upload} %{time_total} %{time_pretransfer}\n' \
  --data-binary @- 'https://speed.cloudflare.com/__up'
true   # a run cut at its time limit still measured what it moved
'''

# The link, from the site's side: send zeros for SPEED_S seconds; or say it is ready, then
# count what arrives, timed from the first chunk.
SEND_PY = f'''
import sys, time
end, chunk = time.time() + {SPEED_S}, b"\\0" * 65536
while time.time() < end:
    sys.stdout.buffer.write(chunk)
'''
COUNT_PY = '''
import sys, time
print("ready", flush=True)
sys.stdin.buffer.read(65536)
t0, n = time.time(), 0
while b := sys.stdin.buffer.read(65536):
    n += len(b)
print(n, time.time() - t0)
'''


class SiteError(RuntimeError):
    """A check that couldn't be made. The message is safe to show."""


class SiteBusy(SiteError):
    """A speed test of the same site is running."""


def _ssh(host: str, ssh_user: str | None, remote: str) -> list[str]:
    """The command that runs `remote` on the site, over the transport a triage would use."""
    return ["bash", "-c", '. "$1"; pick_transport "$2" "$3"; exec "${SSH[@]}" "$4"', "_",
            str(S.skill_dir / "scripts" / "lib_ssh.sh"), host,
            f"{ssh_user}@{host}" if ssh_user else host, remote]


def _last_line(raw: bytes, fallback: str) -> str:
    lines = raw.decode(errors="replace").strip().splitlines()
    return (lines[-1] if lines else fallback)[:300]


def _run(argv: list[str], timeout: float, data: bytes | None = None) -> str:
    try:
        r = subprocess.run(argv, capture_output=True, timeout=timeout,
                           **({"input": data} if data is not None else {"stdin": subprocess.DEVNULL}))
    except subprocess.TimeoutExpired:
        raise SiteError(f"no answer within {timeout:.0f}s") from None
    if r.returncode:
        raise SiteError(_last_line(r.stderr or r.stdout, f"exit {r.returncode}"))
    return r.stdout.decode(errors="replace")


def ping(host: str) -> dict:
    """{ms, via} from one tailscale ping: via is "direct", or the relay it went through."""
    try:   # exits 1 after a relayed pong ("direct connection not established"): read the output
        r = subprocess.run(["tailscale", "ping", "-c", "1", "--timeout", "5s", host],
                           capture_output=True, text=True, timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        return {"ok": False, "error": "no answer"}
    out = r.stdout + r.stderr
    m = re.search(r"via (\S+) in ([\d.]+)ms", out)
    if not m:
        return {"ok": False, "error": out.strip()[:200] or "no answer"}
    path = m.group(1)
    relay = re.fullmatch(r"DERP\((\w+)\)", path)
    via = (f"relayed ({relay.group(1)})" if relay
           else "direct" if re.fullmatch(r"[\d.]+:\d+|\[[\da-fA-F:]+\]:\d+", path) else f"via {path}")
    return {"ok": True, "ms": float(m.group(2)), "via": via}


def check(host: str, ssh_user: str | None, fresh: bool = False) -> dict:
    """ping, and the gateway's health on the site (or why it couldn't be read). A request
    while one is under way waits for it and takes its answer, even when asked `fresh`."""
    asked = time.time()
    with _lock:
        host_lock = _check_locks.setdefault(host, threading.Lock())
    with host_lock:
        hit = _checks.get(host)
        if hit and (hit[0] >= asked or not fresh and asked - hit[0] < CHECK_FRESH_S):
            return hit[1]
        out = {"host": host, "at": time.time(), "ping": ping(host)}
        if out["ping"]["ok"]:
            try:
                out.update(json.loads(_run(_ssh(host, ssh_user, "python3 -"), 40, HEALTH_PY.encode())))
            except (SiteError, ValueError) as e:
                out["error"] = f"the site's health couldn't be read: {e}"
        _checks[host] = (time.time(), out)
    return out


def file_on_site(host: str, ssh_user: str | None, path: str) -> bool:
    """Whether `path` (absolute, or ~/…) is a file on the site's machine."""
    where = '"$HOME"/' + shlex.quote(path[2:]) if path.startswith("~/") else shlex.quote(path)
    return _run(_ssh(host, ssh_user, f"test -f {where} && echo yes || echo no"), 30).strip() == "yes"


@contextmanager
def _one_speed_test(host: str):
    """One speed test of a site at a time: two would halve each other's numbers."""
    with _lock:
        if host in _speed_busy:
            raise SiteBusy(f"a speed test of {host} is already running")
        _speed_busy.add(host)
    try:
        yield
    finally:
        with _lock:
            _speed_busy.discard(host)


def _mbps(nbytes: float, seconds: float) -> float | None:
    return round(nbytes * 8 / seconds / 1e6, 1) if seconds > 0.05 and nbytes > 0 else None


def speed(host: str, ssh_user: str | None, kind: str) -> dict:
    """Mbit/s each way, for the "link" (server <-> site) or the site's "internet"."""
    with _one_speed_test(host):
        if kind == "internet":
            return _internet(host, ssh_user)
        return _link(host, ssh_user)


def _internet(host: str, ssh_user: str | None) -> dict:
    nums = _run(_ssh(host, ssh_user, INTERNET_SH), 40).split()
    if len(nums) != 7:
        raise SiteError("the speed test on the site gave no result")
    down, total, start, connect, up, up_total, pre = map(float, nums)
    if not down and not up:
        raise SiteError("the site couldn't reach the speed test (no internet, or speed.cloudflare.com blocked)")
    return {"kind": "internet", "download_mbps": _mbps(down, total - start),
            "upload_mbps": _mbps(up, up_total - pre), "connect_ms": round(connect * 1000)}


def _link(host: str, ssh_user: str | None) -> dict:
    deadline = time.time() + LINK_LIMIT_S
    out = {"kind": "link", "ping": ping(host)}
    # Site -> server: time what arrives after the first chunk (the connection is set up by then).
    with _piped(_ssh(host, ssh_user, "python3 -c " + shlex.quote(SEND_PY)), deadline) as p:
        first, n = p.stdout.read(65536), 0
        t0 = time.time()
        while chunk := p.stdout.read(65536):
            n += len(chunk)
        if not first:
            raise SiteError(_last_line(p.stderr.read(), "no data came back from the site"))
        out["download_mbps"] = _mbps(n, time.time() - t0)
    # Server -> site: once the site says it is ready, send for SPEED_S seconds; it does the timing.
    with _piped(_ssh(host, ssh_user, "python3 -c " + shlex.quote(COUNT_PY)), deadline,
                stdin=subprocess.PIPE) as p:
        counted = []
        if p.stdout.readline().strip() == b"ready":
            end, chunk = time.time() + SPEED_S, b"\0" * 65536
            try:
                while time.time() < end:
                    p.stdin.write(chunk)
                p.stdin.close()
                counted = p.stdout.read().split()
            except BrokenPipeError:
                pass
        if len(counted) != 2:
            raise SiteError(_last_line(p.stderr.read(), "the site didn't take the upload"))
        out["upload_mbps"] = _mbps(float(counted[0]), float(counted[1]))
    return out


@contextmanager
def _piped(argv: list[str], deadline: float, **kw):
    """A connection streaming through pipes, killed if it is still going at the deadline."""
    kw.setdefault("stdin", subprocess.DEVNULL)   # never the console's own
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw) as p:
        timer = threading.Timer(max(0.0, deadline - time.time()), p.kill)
        timer.start()
        try:
            yield p
            p.wait(max(0.1, deadline - time.time()))
        except subprocess.TimeoutExpired:
            pass
        finally:
            timer.cancel()
            if p.poll() is None:   # else leaving the with would wait for it without end
                p.kill()
