# Stream the data: air-quality monitor side-by-side test platform

Most air-quality monitors only show their readings on a screen and can't export data. This platform
**films their screens with ordinary webcams, stores every picture on your own server, and reads the
numbers off the pictures** (OCR) so they can be compared, minute by minute, with your own Wi-Fi monitors
and with reference data.

You can check in at any time on the **Live** page: a live view of every camera, the numbers currently
on each monitor, alerts, and charts.

```
 TEST BENCH (laptop / desktop / Raspberry Pi)            YOUR SERVER (Hostinger KVM 2, Docker)
 ┌─────────────────────────────────────────┐           ┌──────────────────────────────────────────────┐
 │ monitors ──► webcam(s) ──► capture agent │──HTTPS──►│ Caddy (HTTPS) ─► API ─► stored pictures       │
 │                 • a still every 10 s     │           │                   │   (SHA-256 checked)       │
 │                 • saved locally first,   │           │                   ▼                           │
 │                   uploaded when online   │           │                PostgreSQL ◄── worker: reads    │
 │                 • live view on demand    │           │                   ▲          numbers + alerts  │
 └─────────────────────────────────────────┘           │                   │                           │
 your own monitors ─► your cloud ─► CSV upload / push ─►│ reference data ───┘                           │
                                                        │ Dashboard: Live · Calibrate · Review · Data   │
                                                        └──────────────────────────────────────────────┘
```

## What it does

| | |
|---|---|
| **Capture** | The agent keeps each camera open, saves a still every *N* seconds (aligned to the clock so all cameras match), and uploads it. If the internet drops, stills queue on disk and upload later; nothing is lost. Focus/exposure can be locked; sleep is blocked. |
| **Live view** | Only while someone is watching a camera on the dashboard, the agent also streams a smaller picture about twice a second, so upload stays small the rest of the time. |
| **Evidence** | Every stored picture is checked against the SHA-256 the agent computed when it took it. Exports include each picture's hash, and the pictures manifest lists them all. |
| **Reading numbers** | You mark each monitor's screen once (click 4 corners), then draw a box around each number. A purpose-built seven-segment reader handles LCD digits; Tesseract handles normal fonts on colour screens. |
| **Quality control** | Unclear readings, values outside a plausible range, and low-confidence reads go to a **review queue**. A person confirms, corrects, or rejects each one, and every decision is logged with their name. Re-reading past pictures never overwrites human decisions. |
| **Comparison** | Import your own monitors' cloud data or a reference instrument (CSV upload or JSON push). It's drawn on the same charts as dashed lines. |
| **Alerts** | Camera offline, image dark or frozen, stills not arriving, upload backlog, computer clock off by more than 5 s, disk low. Shown on the dashboard and optionally sent to Slack/Discord/Telegram. |
| **Exports** | Per test run or any time range: readings CSV (machine value, final value, status, reviewer, picture hash), pictures manifest, and reference data. |

## Repository layout

| Path | What |
|---|---|
| `agent/` | Capture agent (Python + OpenCV) for the computer next to the monitors. See [agent/README.md](agent/README.md). |
| `server/` | API, OCR worker and dashboard (FastAPI, SQLAlchemy, OpenCV). Dashboard pages are plain HTML/JS in `server/aqserver/static/`. |
| `deploy/` | Docker Compose stack for the VPS: PostgreSQL, API, worker, Caddy (automatic HTTPS), plus a backup script. |
| `docs/deploy.md` | Step-by-step server setup on Hostinger. |
| `docs/test-protocol.md` | How to set up and run a side-by-side test so the data holds up in a white paper. |

## Try it locally in 5 minutes (no webcam needed)

The agent has a built-in **synthetic camera** that films two fake monitors whose numbers change.

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
```

Start the server and the worker (two terminals, both in `server/`):

```bash
export DATA_DIR=./data AGENT_TOKEN=local-test-token-123 DASHBOARD_USERS=admin:admin
.venv/bin/uvicorn aqserver.main:create_app --factory --port 8000
```

```bash
export DATA_DIR=./data AGENT_TOKEN=local-test-token-123 DASHBOARD_USERS=admin:admin
.venv/bin/python -m aqserver.worker
```

Start the agent (third terminal):

```bash
cd agent
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Edit `config.yaml` so that `server_url: http://127.0.0.1:8000`, `agent_token: local-test-token-123` and the camera has `source: synthetic`. Then:

```bash
.venv/bin/python -m aqagent run -c config.yaml
```

Open <http://127.0.0.1:8000> (login `admin` / `admin`). Go to **Calibrate**, add each fake monitor by clicking its
screen corners, draw boxes around the numbers, and press **Test reading**. Readings then appear on the **Live** page.
(The Tesseract reader also needs `tesseract-ocr` installed; the seven-segment reader does not.)

## Tests

```bash
cd server && .venv/bin/python -m pytest      # API, OCR (750+ rendered LCD cases: 3 font widths, slant, ghost segments), review, exports, alerts
cd agent && ../server/.venv/bin/python -m pytest   # spool, config, and a real upload + live-stream round trip
```

Set `TEST_DATABASE_URL=postgresql+psycopg://user:pw@host:5432/db` to run the server tests against PostgreSQL.

## Status and next steps

Working now: capture, store-and-forward upload, live view, calibration UI, OCR, review queue with audit log,
reference import, alerts, exports, Docker deployment.

Not built yet:
- An analysis notebook that turns the exports into white-paper tables and figures (R², slope/intercept, RMSE, bias, completeness).
- Support for monitors that cycle between screens (PM2.5, then CO₂, ...).
- A learned digit reader trained on your corrected readings, for unusual fonts.
- Pulling your own monitors' data straight from your cloud's API (for now: CSV upload or JSON push).
