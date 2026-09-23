---
topics:
- camera
symptoms:
- there's no video
- the camera moves but we see no picture
- the video window is black
- the stream never loads
---

# No video in the UI, though the camera is otherwise working

Everything about the camera works except the picture. PTZ commands move it, its status shows
healthy, and the video panel stays black or never finishes loading. Because the camera
demonstrably responds, this gets reported as a UI problem.

## Evidence

- `ffmpeg not found; video pipeline disabled`
- `ffmpeg not found; camera ... video bridge disabled`
- `Video pipeline disabled: no RTSP URL (camera offline?)`
- `stream_unavailable`

## Root cause

```
camera --RTSP--> optic_ptz node --eCAL--> gateway media bridge --fMP4--> browser
                      ^                          ^
                  ffmpeg #1                  ffmpeg #2
```

**Stage 1 — the node side.** `python/nodes/optic_ptz_node/video_pipeline.py:60`:

> `ffmpeg not found; video pipeline disabled`

The node pulls RTSP from the camera and encodes for eCAL. Without ffmpeg it publishes nothing,
and every downstream consumer sees an absence rather than an error.

**Stage 2 — the gateway side.** `GUItcha30/gateway/src/adapters/media_bridge.py:133`:

> `ffmpeg not found; camera <id> video bridge disabled`

The gateway remuxes the eCAL video stream into fragmented MP4 for the browser. Without ffmpeg
the websocket at `/api/camera/{id}/stream` sends `{"type": "stream_unavailable"}` and closes —
which the UI renders as a black panel.

Both locate the binary with `shutil.which("ffmpeg")`, so both fail the same way if it is not
on `PATH`. ffmpeg *is* installed in the runtime image specifically for these two consumers, so
on a deployed machine its absence means something is wrong with the image rather than the
site.

There is a third, different case with a similar symptom:

> `Video pipeline disabled: no RTSP URL (camera offline?)`

ffmpeg is present, but the node never got a stream URL from the camera — that is
`camera-offline-onvif`, not this.

## Checks (read-only)

Video passes through **two** separate ffmpeg stages, and they fail with nearly identical
messages from completely different places. Getting the right one first saves the call:

```
make logs SERVICE=gotcha30 2>&1 | grep -i ffmpeg   # stage 1: camera -> eCAL
make logs SERVICE=gateway  2>&1 | grep -i ffmpeg   # stage 2: eCAL -> browser
```

## Fix

Confirm whether ffmpeg is actually present in the container that is complaining:

```
make shell SERVICE=gotcha30
which ffmpeg && ffmpeg -version | head -1
exit
```

and separately:

```
make shell SERVICE=gateway
which ffmpeg && ffmpeg -version | head -1
exit
```

- **Missing in either** — the image is wrong. Do not install it by hand in a running
  container; it will not survive a restart and it makes the machine's state undiagnosable.
  Move the machine to a good image: `make pull && make up`, or `make rollback TAG=<good tag>`
  followed by `make up`.
- **Present in both** — ffmpeg is not your problem. The pipeline is failing for another reason;
  read the lines around the failure for `Video pipeline error:` or
  `Camera <id> remux reader error:`, which carry the real exception.

## Verify

```
make logs SERVICE=gotcha30 2>&1 | grep -i "ffmpeg\|video pipeline" | tail
make logs SERVICE=gateway  2>&1 | grep -i "ffmpeg\|remux"          | tail
```

Both clean, and the video panel in the UI shows live picture.

## If that didn't work

- If the panel shows a picture that is frozen rather than absent, the stream is arriving and
  stalling — look for `remux reader error` on the gateway side.
- If several UI panels are empty and not just video, this may be a wheel-version problem
  instead: `old-wheel-partial-degradation`.
- The Meduza optic and the PTZ camera are different devices with different pipelines. Confirm
  which one the customer means — `--print-config` lists what the site actually has.
