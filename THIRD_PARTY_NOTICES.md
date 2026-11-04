# Third-party notices

AQ Test Platform is © 2026 EMERGESPHERE Technologies Pvt. Ltd. (see [LICENSE](LICENSE)).
It uses the open-source components below, which stay under their own licences.

## Included in this repository

| Component | Version | Licence | Where |
|---|---|---|---|
| uPlot (charts) | 1.6.31 | MIT | `server/aqserver/static/vendor/`; licence text in `uplot-LICENSE.txt` |

## Installed when the software is set up (not included in this repository)

Python packages, from `server/requirements.txt` and `agent/requirements.txt`:

| Package | Licence |
|---|---|
| FastAPI | MIT |
| Uvicorn | BSD-3-Clause |
| SQLAlchemy | MIT |
| psycopg | LGPL-3.0 (used unmodified, as a separately installed library) |
| python-multipart | Apache-2.0 |
| OpenCV (opencv-python-headless) | Apache-2.0 |
| NumPy | BSD-3-Clause (and compatible licences for bundled parts) |
| pytesseract | Apache-2.0 |
| Pillow | MIT-CMU |
| python-docx | MIT |
| Matplotlib | Matplotlib licence (PSF-based) |
| Requests | Apache-2.0 |
| PyYAML | MIT |
| pygrabber (Windows only) | see the project's page on PyPI |

In the Docker setup (`server/Dockerfile`, `deploy/docker-compose.yml`): Tesseract OCR
(Apache-2.0), DejaVu fonts (free licence), the Python base image, PostgreSQL (PostgreSQL
Licence) and Caddy (Apache-2.0).

Licences above are as declared by each project; check a project's own licence text
before redistributing it.
