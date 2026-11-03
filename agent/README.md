# Capture agent

Runs on the computer next to the monitors (Windows, macOS, Linux or a Raspberry Pi). It films the monitors,
saves a still every few seconds, uploads it to the server, and sends a live view while someone watches the dashboard.

## Install

You need Python 3.10 or newer (on Windows, from python.org, with "Add to PATH" ticked). From this `agent` folder:

**Windows (PowerShell)**

```powershell
py -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.yaml config.yaml
notepad config.yaml
```

**macOS / Linux / Raspberry Pi**

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
nano config.yaml
```

In `config.yaml`, set `server_url`, `agent_token` (the server's `AGENT_TOKEN`), a name for this computer (`agent_id`),
and one entry per camera.

## Check everything before a test

In the commands below, Windows uses `.venv\Scripts\python` instead of `.venv/bin/python`.

```bash
.venv/bin/python -m aqagent list-cameras
.venv/bin/python -m aqagent snapshot -c config.yaml
.venv/bin/python -m aqagent check -c config.yaml
```

- `list-cameras` finds which camera number is which webcam.
- `snapshot` saves one picture. Check that every screen is sharp and readable.
- `check` tests the server address, the token, and the clock difference from the server.

Then start capturing:

```bash
.venv/bin/python -m aqagent run -c config.yaml
```

Stop it with Ctrl+C. Stills that haven't uploaded yet stay in `spool/pending` and go up next time.

## Start automatically and survive reboots

- **Windows:** run `powershell -ExecutionPolicy Bypass -File scripts\install-windows-task.ps1`. It registers a task
  that starts the agent at log-on and restarts it if it stops. Also set *Power options → Sleep: Never* and
  pause Windows Update for the test period.
- **Linux / Raspberry Pi:** edit and install `scripts/aqagent.service` (instructions inside). It starts on boot.
- **macOS:** the agent blocks idle sleep while running. To start it at log-in, use a LaunchAgent, or keep a
  terminal open.

The agent writes a log to `spool/agent.log`.

## Tips

- **Several USB webcams on one computer:** keep `fourcc: MJPG` (the default) and plug them into different USB ports.
  Hubs often can't carry two 1080p streams.
- **Network cameras:** any camera with an RTSP stream works: `source: "rtsp://user:pass@192.168.1.50:554/stream1"`.
- **Focus:** `autofocus: false` stops the camera refocusing on its own. If the image is then blurry, set `focus` (try values 0–255).
- **Disk:** sent stills are kept locally for `keep_sent_days` as a second copy (about 2 GB per camera per day).
- **Never change a camera's `id` during a test.** The server uses it to tell cameras apart.
