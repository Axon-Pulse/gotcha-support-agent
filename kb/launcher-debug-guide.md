# System Launcher Debug Guide

This guide explains how to debug issues with the Gotcha30 System Launcher and its managed nodes.

## Debug Modes

### Verbose Mode

Enable verbose logging to see detailed information:

```bash
./build/bin/system_launcher -c config.yaml -v
```

Verbose mode shows:
- Configuration loading and parsing
- Node template application
- Process spawn details (PID, command, arguments)
- State transitions
- Shutdown sequence progress

### Log Files

Enable log file output for post-mortem analysis:

```bash
./build/bin/system_launcher -c config.yaml --log-dir ./logs
```

Or in configuration:

```yaml
session:
  log_directory: "./logs"
  log_level: "debug"
```

Log files created:
- `launcher.log` - Main launcher log
- Timestamped entries with component prefixes

### Log Levels

| Level | Description |
|-------|-------------|
| debug | Detailed internal state, useful for development |
| info | Normal operational messages |
| warn | Warnings that don't stop operation |
| error | Errors that affect functionality |
| fatal | Errors that stop the launcher |

## Understanding Launcher Output

### Startup Sequence

```
╔══════════════════════════════════════════════════════════╗
║           Gotcha30 System Launcher v1.0.0                ║
╚══════════════════════════════════════════════════════════╝

[2025-01-15 10:30:00.123][INFO ][MAIN        ] Config: /path/to/config.yaml
[2025-01-15 10:30:00.124][INFO ][MAIN        ] Terminal backend: aggregated
[2025-01-15 10:30:00.125][INFO ][CONFIG      ] Loaded config: config.yaml (3 nodes)
=== Gotcha30 System Launcher ===
Session: My Session

[2025-01-15 10:30:00.126][INFO ][LAUNCHER    ] Initialized with 3 node(s)
[2025-01-15 10:30:00.127][INFO ][PROCESS     ] Spawned 3 of 3 configured nodes
[2025-01-15 10:30:00.128][INFO ][LAUNCHER    ] All nodes started. Press Ctrl+C to shutdown.
```

### Process Output

Color-coded output from nodes:

```
[Radar Node    ] Starting radar connection...
[Tracker Node  ] Initializing tracker...
[Health Monitor] System health: OK
```

Each node gets a unique color for easy identification.

### Shutdown Sequence

```
[LAUNCHER]        Shutting down...
[2025-01-15 10:35:00.000][INFO ][SHUTDOWN    ] Initiating shutdown: SIGINT (Ctrl+C)
[2025-01-15 10:35:00.001][INFO ][SHUTDOWN    ] Publishing /shutdown topic
[2025-01-15 10:35:00.002][INFO ][ECAL        ] Published /shutdown topic
[2025-01-15 10:35:00.003][INFO ][SHUTDOWN    ] Waiting for processes to respond...
[2025-01-15 10:35:02.003][INFO ][SHUTDOWN    ] After /shutdown topic wait: 2 process(es) still running
[2025-01-15 10:35:02.004][INFO ][SHUTDOWN    ] Sending SIGTERM to remaining processes
[2025-01-15 10:35:02.005][INFO ][PROCESS     ] Process exited: Radar Node (PID 12345) - Exit code 0
[2025-01-15 10:35:02.006][INFO ][PROCESS     ] Process exited: Tracker Node (PID 12346) - Exit code 0
[2025-01-15 10:35:02.007][INFO ][SHUTDOWN    ] All processes terminated gracefully

=== System Status ===
Nodes: 0 running, 3 stopped

NAME                STATE          PID       EXIT
-------------------------------------------------------
Health Monitor      EXITED_NORMAL  12344     0
Radar Node          EXITED_NORMAL  12345     0
Tracker Node        EXITED_NORMAL  12346     0

=== Launcher Shutdown ===
[2025-01-15 10:35:02.008][INFO ][MAIN        ] Launcher exited with code 0
```

## Process States

| State | Description |
|-------|-------------|
| NOT_STARTED | Process hasn't been spawned |
| STARTING | Process is being spawned |
| RUNNING | Process is running normally |
| TERMINATING | SIGTERM sent, waiting for exit |
| EXITED_NORMAL | Clean exit (code 0 or SIGTERM) |
| EXITED_ERROR | Non-zero exit code |
| CRASHED | Unexpected signal received |
| KILLED | Terminated via SIGKILL |

## Shutdown Phases

| Phase | Description |
|-------|-------------|
| NOT_STARTED | Shutdown hasn't begun |
| PUBLISHING_SHUTDOWN_TOPIC | Publishing eCAL /shutdown |
| WAITING_GRACEFUL | Waiting for graceful exit |
| SENDING_SIGTERM | Sending SIGTERM signals |
| WAITING_SIGTERM | Waiting after SIGTERM |
| SENDING_SIGKILL | Sending SIGKILL signals |
| COMPLETED | Shutdown finished |

## Debugging Specific Issues

### Debug Configuration Loading

```bash
# Check YAML validity
python3 -c "import yaml; yaml.safe_load(open('config.yaml'))"

# Validate against schema (requires jsonschema)
pip install jsonschema pyyaml
python3 -c "
import yaml
import json
from jsonschema import validate

with open('config.yaml') as f:
    config = yaml.safe_load(f)

with open('schemas/gotcha30-config.schema.json') as f:
    schema = json.load(f)

validate(config, schema)
print('Configuration is valid')
"
```

### Debug Process Spawn Issues

Enable debug logging and check for spawn errors:

```bash
./build/bin/system_launcher -c config.yaml -v 2>&1 | grep -E "(spawn|PROCESS|ERROR)"
```

Check if the command exists:
```bash
which ./build/bin/my_node
# or
ls -la ./build/bin/my_node
```

Check library dependencies:
```bash
ldd ./build/bin/my_node
```

### Debug Shutdown Issues

Identify which processes aren't terminating:

```bash
./build/bin/system_launcher -c config.yaml -v 2>&1 | grep -E "(SHUTDOWN|SIGTERM|SIGKILL|still running)"
```

The log will show which processes required SIGKILL:
```
[WARN] Sending SIGKILL to 1 unresponsive process(es): StubbornNode
```

### Debug eCAL Communication

Monitor eCAL topics:
```bash
ecal_mon_cli -m
```

Or use the GUI:
```bash
ecal_mon_gui &
```

Check if /shutdown topic is published:
```bash
ecal_mon_cli -t /shutdown
```

### Debug Terminal Backend Issues

For tmux issues:
```bash
# List sessions
tmux list-sessions

# Attach to session
tmux attach -t gotcha30_session

# Kill orphaned sessions
tmux kill-server
```

For gnome-terminal issues:
```bash
# Check display
echo $DISPLAY

# Test gnome-terminal
gnome-terminal --tab -e "echo test"
```

## Debug Tools

### pstree - Process Tree

View the process hierarchy:
```bash
pstree -p $(pgrep system_launcher)
```

### strace - System Calls

Trace process spawning:
```bash
strace -f -e trace=process ./build/bin/system_launcher -c config.yaml
```

### ltrace - Library Calls

Trace library calls:
```bash
ltrace -f ./build/bin/system_launcher -c config.yaml
```

### gdb - Debugger

Debug the launcher:
```bash
gdb --args ./build/bin/system_launcher -c config.yaml -v

# In gdb:
(gdb) break launcher::ProcessManager::spawnAll
(gdb) run
(gdb) bt    # backtrace when stopped
```

### valgrind - Memory Check

Check for memory issues:
```bash
valgrind --leak-check=full ./build/bin/system_launcher -c config.yaml
```

## Common Debug Patterns

### Why Doesn't My Node Start?

1. Check verbose output for spawn errors
2. Verify executable path and permissions
3. Check working directory
4. Test running the command directly
5. Check for missing arguments

### Why Doesn't My Node Stop?

1. Verify signal handler implementation
2. Check if subscribed to /shutdown topic
3. Increase timeout values
4. Look for blocking operations in cleanup

### Why Is Output Missing?

1. Check that stdout/stderr are line-buffered
2. For Python, use `-u` flag for unbuffered output
3. Verify terminal backend settings
4. Check for output redirection in node

### Why Did The Launcher Crash?

1. Check log files for errors
2. Run with verbose mode
3. Check for signal handling conflicts
4. Verify configuration is valid

## Performance Profiling

### CPU Profiling

```bash
# Using perf
perf record ./build/bin/system_launcher -c config.yaml
perf report

# Using valgrind's callgrind
valgrind --tool=callgrind ./build/bin/system_launcher -c config.yaml
kcachegrind callgrind.out.*
```

### Memory Profiling

```bash
# Using massif
valgrind --tool=massif ./build/bin/system_launcher -c config.yaml
ms_print massif.out.*
```
