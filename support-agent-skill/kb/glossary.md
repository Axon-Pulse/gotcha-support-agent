# Glossary

Written for whoever is on the call. Customers, field engineers and developers use different
words for the same things, and most confused support conversations start here.

## The system

**gotcha** — the whole product: sensors, the processing core, and the C2 web interface.

**system** / **site** — one deployed installation, e.g. `axon-gotcha-3`. One machine, one set
of sensors, one config directory. "Site" and "system" are used interchangeably; the config
layout calls it a system, people on the phone usually say site.

**C2** — command and control; the web interface the operator actually looks at. When a
customer says "the system", they usually mean this, because it is the only part they see.

**the core** — everything behind the C2: the sensor nodes and the processing chain. When an
engineer says "the system", they usually mean this.

## Pieces of software

**node** — one process doing one job (talk to a radar, track targets, drive a camera). A
deployed system runs somewhere between five and twenty of them.

**launcher** / **system_launcher** — the program that reads the config and starts every node.
If the launcher does not start, nothing does.

**eCAL** — the message bus the nodes talk over. Nodes publish to *topics* (`/radar/detections`,
`/Targets`) and subscribe to them. If someone says "it's not on the bus", they mean nothing is
publishing that topic.

**gateway** — the bridge between the core and the web UI. Reads from eCAL, serves the browser
over a websocket on port 8080.

**frontend** — the web page itself, served on port 5173. Separate process from the gateway,
which is why the page can load perfectly while showing no data.

**py_glue** — the compiled Python bindings that let Python code talk to the C++ core and its
message formats. Shipped as a wheel inside the image. A large share of confusing failures are
version mismatches in this one component.

## Sensors

**Magos** — the radar. A site typically has several, each covering a sector. Conventionally on
the `192.168.1.x` subnet.

**ASU** — the acoustic sensor unit; detects drones by sound. Conventionally `192.168.2.x`.

**Meduza** — an electro-optical sensor that detects and reports on its own. Conventionally
`192.168.3.x`. **Not the same thing as the PTZ camera** — this confusion is common and costs
real time, so establish which one the customer means early.

**PTZ camera** — a pan/tilt/zoom camera the system can point at a target, spoken to over
**ONVIF** (the industry standard protocol for IP cameras) and streamed over **RTSP**.

**effector** — the countermeasure hardware. A **net effector** fires a net; a **blinding
effector** uses light.

## Processing

**detection** — one sensor saying "something is there, right now". Cheap, noisy, no memory.

**track** — detections joined over time into one moving thing. Produced by the **tracker**.

**target** — a track that has been fused across sensors and classified. Produced by the
**target node**. This is what the operator sees.

**shadow** — a track kept alive for a drone that has stopped moving and become invisible to
the radar's Doppler. Stationary drones are not gone.

**classification** — deciding whether a track is a drone, a bird, or something else. Uses a
model file that is **supplied at deployment and not in the source repo** — a frequent cause of
"it detects but never identifies".

**defense center** — the site's reference point; everything is positioned relative to it. If
this is wrong, the entire picture is in the wrong place while being internally consistent.

## Deployment

**image** — the Docker image containing all the software. Every machine runs one.

**IMAGE_TAG** — which version a machine is on. `:stable` is a release; `:latest` is the newest
build of main. **The first question for any "it broke after the update" report.**

**harness** — the small set of files a field machine has outside the image: the Makefile, the
compose file, `.env`, and a few scripts. Machines without a git checkout get these from inside
the image itself.

**`.env`** — the machine's own settings. Created once at install and **never updated
afterwards**, which is why old machines can be missing settings that newer ones have.

**mock mode** — the gateway inventing data instead of reading real sensors. Used for
development. When it happens accidentally on a real site, the tell is a map centred on Dallas,
Texas.

## Things that mean "broken" in a specific way

**silent failure** — the system reports itself healthy and does nothing useful. Most of this
knowledge base is about these, because they are the ones that waste days.

**degraded** — a node is running but not doing its whole job. Usually deliberate: losing one
feature is better than taking the system down.

**heartbeat timeout** — a node stopped responding for five seconds and the launcher declared
it dead.
