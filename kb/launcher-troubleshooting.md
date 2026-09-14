# System Launcher Troubleshooting Guide

This guide covers common issues and their solutions when using the Gotcha30 System Launcher.

## Startup Issues

### Configuration File Not Found

**Symptom:**
```
[FATAL] Config file not found: /path/to/config.yaml
```

**Solutions:**
1. Verify the path is correct and the file exists
2. Use an absolute path or path relative to the current directory
3. Check file permissions

### Invalid YAML Syntax

**Symptom:**
```
[FATAL] YAML parsing error: yaml-cpp: error at line 12
```

**Solutions:**
1. Validate YAML syntax using an online validator or IDE
2. Check for incorrect indentation (use spaces, not tabs)
3. Ensure strings with special characters are quoted

### Missing Required Fields

**Symptom:**
```
[FATAL] Node missing required field 'cmd'
```

**Solutions:**
1. Each node must have both `name` and `cmd` fields
2. Check that the template (if used) provides a `cmd`
3. Verify the config structure matches the schema

### Unknown Template Reference

**Symptom:**
```
[FATAL] Node 'MyNode' references unknown template: bad_template
```

**Solutions:**
1. Check spelling of template name
2. Ensure the template is defined in `node_templates` section
3. If using inheritance, verify parent config defines the template

## Process Issues

### Node Won't Start

**Symptom:**
```
[ERROR] Failed to spawn process: MyNode
```

**Common Causes:**

1. **Executable not found:**
   ```bash
   # Verify the executable exists
   ls -la ./build/bin/my_node

   # Check if it's executable
   chmod +x ./build/bin/my_node
   ```

2. **Wrong working directory:**
   ```yaml
   # Specify the correct working directory
   nodes:
     - name: MyNode
       cmd: "./build/bin/my_node"
       cwd: "/home/user/project"
   ```

3. **Missing dependencies:**
   ```bash
   # Check library dependencies
   ldd ./build/bin/my_node
   ```

4. **Build not complete:**
   ```bash
   # Rebuild the project
   cd build && make -j$(nproc)
   ```

### Node Crashes Immediately

**Symptom:**
```
[WARN] Process exited: MyNode (PID 12345) - Exit code 1
```

**Solutions:**
1. Run the node directly to see error messages:
   ```bash
   ./build/bin/my_node --help
   ```

2. Check for missing arguments:
   ```yaml
   # Ensure all required arguments are provided
   nodes:
     - name: MyNode
       cmd: "./build/bin/my_node"
       args: ["--required-arg", "value"]
   ```

3. Check for configuration errors in the node itself

### Node Ignores Shutdown

**Symptom:**
```
[WARN] Sending SIGKILL to unresponsive process(es): MyNode
```

**Solutions:**

The node doesn't handle SIGTERM properly. Implement signal handling:

```cpp
#include <csignal>
#include <atomic>

std::atomic<bool> shutdown_requested{false};

void signalHandler(int signal) {
    shutdown_requested.store(true);
}

int main() {
    signal(SIGTERM, signalHandler);
    signal(SIGINT, signalHandler);

    while (!shutdown_requested.load()) {
        // Main loop
    }

    // Cleanup
    return 0;
}
```

For eCAL nodes, subscribe to the `/shutdown` topic:
```cpp
void onShutdownCallback(const GLUE::lifecycle::Shutdown& msg) {
    if (msg.node_name() == "all" || msg.node_name() == node_name_) {
        requestShutdown();
    }
}
```

## Terminal Backend Issues

### tmux Not Available

**Symptom:**
```
[FATAL] tmux backend requested but tmux is not installed
```

**Solution:**
```bash
# Install tmux
sudo apt install tmux
```

### gnome-terminal No Display

**Symptom:**
```
[FATAL] gnome-terminal backend requested but no display is available
```

**Solutions:**
1. Run from a graphical session (X11 or Wayland)
2. Use the `aggregated` or `tmux` backend instead
3. Set the DISPLAY environment variable for remote X:
   ```bash
   export DISPLAY=:0
   ```

### tmux Session Already Exists

**Symptom:**
```
[WARN] tmux session already exists, killing it
```

**Solution:**
This is normal behavior. The launcher cleans up old sessions automatically. To manually clean:
```bash
tmux kill-session -t gotcha30_session
```

## Performance Issues

### High CPU Usage

**Symptom:**
Launcher uses excessive CPU during idle periods.

**Solutions:**
1. Check nodes for busy loops
2. Increase monitor polling interval (future feature)
3. Use `htop` or `top` to identify the offending process

### Output Flooding

**Symptom:**
Terminal is overwhelmed with output.

**Solutions:**
1. Reduce node verbosity
2. Use log files:
   ```yaml
   session:
     log_directory: "./logs"
   ```
3. Use tmux backend to separate outputs

## eCAL Issues

### /shutdown Not Received

**Symptom:**
Nodes don't respond to `/shutdown` topic.

**Solutions:**
1. Verify eCAL is running:
   ```bash
   ecal_mon_gui
   ```

2. Check topic subscription in nodes

3. Increase shutdown wait time:
   ```yaml
   session:
     ecal_shutdown_wait_ms: 5000
   ```

4. Disable if not using eCAL:
   ```bash
   ./build/bin/system_launcher --no-ecal-shutdown
   ```

### eCAL Initialization Failed

**Symptom:**
```
[WARN] Failed to initialize eCAL: ...
```

**Solutions:**
1. Check eCAL installation
2. Verify eCAL configuration in `/etc/ecal/`
3. Continue without eCAL shutdown support (warning only)

## Configuration Inheritance Issues

### Circular Inheritance Detected

**Symptom:**
```
[FATAL] Circular config inheritance detected: a.yaml -> b.yaml -> a.yaml
```

**Solution:**
Review and fix the inheritance chain. Avoid circular references in `extends`.

### Parent Config Not Found

**Symptom:**
```
[FATAL] Config file not found: parent.yaml
```

**Solutions:**
1. Use relative paths from the child config's directory
2. Or use absolute paths in `extends`

## Debugging Tips

### Enable Verbose Logging

```bash
./build/bin/system_launcher -c config.yaml -v
```

This shows:
- Configuration loading details
- Process spawn/exit events
- Shutdown sequence steps
- State transitions

### Check Process Status

During runtime, the launcher shows status on shutdown:

```
=== System Status ===
Nodes: 0 running, 3 stopped

NAME                STATE          PID       EXIT
-------------------------------------------------------
Radar Node          EXITED_NORMAL  12345     0
Tracker Node        KILLED         12346     -
Health Monitor      EXITED_ERROR   12347     1
```

### Test Configuration

Create a simple test config to verify the launcher:

```yaml
version: "1.0"
session:
  title: "Test"
  end_on_first_complete: true
nodes:
  - name: Test
    cmd: "echo"
    args: ["Hello!"]
```

```bash
./build/bin/system_launcher -c test.yaml -v
```

### Run Individual Nodes

If a node isn't starting, try running it directly:

```bash
# Get the command from the config
./build/bin/elm2135Node --ip 192.168.1.100

# Check exit codes
echo $?
```
