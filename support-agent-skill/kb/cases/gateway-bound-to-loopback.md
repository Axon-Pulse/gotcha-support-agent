---
topics:
- deploy
- network
symptoms:
- the map is empty from my laptop
- connection refused on port 8080
- it works when I'm on the machine but not remotely
- the page loads but no data appears
- we can see the UI but it never connects
---

# The UI works on the machine itself but not from anywhere else

The frontend loads fine — they get the page, the layout, the map tiles. But nothing ever
appears on it, and the browser console shows the websocket failing. If they open it on the
gotcha machine itself (or over a remote desktop to it) everything works perfectly, which
makes it look like a network or firewall problem on their side.

It is not. The gateway is listening on loopback only.

## Evidence

- `curl: (7) Failed to connect to ... port 8080: Connection refused`
- `WebSocket connection to 'ws://.../ws' failed`

## Root cause

`GUItcha30/config/config.yaml` ships with:

```yaml
gateway:
  host: "127.0.0.1"
  port: 8080
```

That value is read at `GUItcha30/gateway/src/main.py` and applied over the `0.0.0.0` default.
Because every compose service runs with `network_mode: host`, there is no Docker port
publishing to paper over it — the process binds to loopback on the host and that is exactly
what you get. Nothing logs a warning, because from the gateway's point of view it started
perfectly.

The frontend keeps working because it is a separate process on 5173 that binds normally,
which is precisely why this presents as "the UI is broken" rather than "the gateway is
unreachable".

## Checks (read-only)

From any machine that is *not* the gotcha box:

```
curl -sS -m 5 http://<machine>:5173/ -o /dev/null -w 'frontend %{http_code}\n'
curl -sS -m 5 http://<machine>:8080/health -o /dev/null -w 'gateway  %{http_code}\n'
```

**Frontend answers, gateway refuses** is the signature. If both refuse, this is not your
problem — it is networking. If both answer, the gateway is reachable and you are looking at
something else (start with `ui-shows-tracks-over-dallas`).

## Fix

There is an environment override — `GATEWAY_HOST`, honoured at
`GUItcha30/gateway/src/main.py:270` — but note it is **not** in `.env.example`, so nobody
discovers it by reading the config. Add it:

```
cd <the gotcha directory>
echo 'GATEWAY_HOST=0.0.0.0' >> .env
make restart SERVICE=gateway
```

If you would rather fix it at the source, edit `gateway-config/config.yaml` on the machine
and change `gateway.host` to `0.0.0.0`, then `make restart SERVICE=gateway`. Both work; the
`.env` route is easier to explain over the phone and easier to undo.

> Before telling a customer to bind `0.0.0.0`, be sure that is what they want. On a site
> where the gotcha machine sits on a network they do not fully control, loopback-only may
> have been a deliberate decision by whoever deployed it.

## Verify

```
curl -sS -m 5 http://<machine>:8080/health
```

Expect HTTP 200 and a JSON body containing `"status": "healthy"`. A 503 means the gateway is
now reachable but unhealthy — that is a *different* problem and you have made progress; go to
`ui-shows-tracks-over-dallas`.

Then reload the UI from the remote machine and confirm entities appear.

## If that didn't work

- The gateway may be reachable but rejecting the websocket specifically — that is the
  `allowed_websocket_hosts` case, and it closes the connection with code 1008 rather than
  refusing to connect. Check the gateway log for `WebSocket connection rejected from`.
- Confirm you actually restarted the right thing: `make restart SERVICE=gateway` only
  restarts the gateway container. `make status` should show it recently started.
- If `.env` edits appear to have no effect at all, the machine may predate that variable
  existing — see `env-missing-vars-after-upgrade`.
