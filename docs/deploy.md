# Deploying on the Hostinger KVM 2 VPS

The server runs as four Docker containers: **PostgreSQL** (database), **api** (dashboard + upload API),
**worker** (reads numbers, raises alerts) and **Caddy** (HTTPS with a free, auto-renewed certificate).

## 1. Prepare the VPS (once)

1. In Hostinger hPanel, set the VPS operating system to **Ubuntu 24.04 with Docker** (or plain Ubuntu, then
   install Docker with `curl -fsSL https://get.docker.com | sh`).
2. Pick a subdomain, e.g. `aq.yourcompany.com`, and add a DNS **A record** pointing it at the VPS IP address.
3. Open ports **22, 80 and 443** in the VPS firewall (hPanel → VPS → Firewall, and `ufw` if you use it).
   Port 80 is needed for the HTTPS certificate.

## 2. Install

SSH into the server, then:

```bash
git clone https://github.com/theOGdivya/Stream-the-data-.git aq && cd aq/deploy
```

The repository is private, so GitHub will ask for credentials. Use a fine-grained personal access token with read access to this repository, or add a deploy key.

```bash
cp .env.example .env
nano .env
```

Fill in every value in `.env`. For each secret, generate a random string with `openssl rand -hex 24`.
`DASHBOARD_USERS` takes one `name:password` pair per person; the names appear in the audit log.

```bash
docker compose up -d --build
docker compose ps
```

Within about a minute, `https://aq.yourcompany.com` asks for a login. If the certificate doesn't appear, run
`docker compose logs caddy`; usually the DNS record hasn't propagated yet or port 80 is closed.

## 3. Connect a capture computer

Follow [agent/README.md](../agent/README.md). Use `server_url: https://aq.yourcompany.com` and set
`agent_token` to the `AGENT_TOKEN` value from `.env`.

## 4. Backups (do this before the first real test)

```bash
crontab -e
```

Add this line:

```
17 2 * * * /root/aq/deploy/backup.sh >> /var/log/aq-backup.log 2>&1
```

This dumps the database and copies new pictures into `deploy/backups/` every night. **Also copy that folder
off the server** (for example with `rclone` to Google Drive, Backblaze B2 or S3). A backup on the same disk is
lost if the disk is lost.

## 5. Storage planning

KVM 2 has about 100 GB of disk. A 1080p still is about 150–300 KB.

| Setup | Per day | 15-day test |
|---|---|---|
| 1 camera, still every 10 s | ~1.5–2.5 GB | ~25–40 GB |
| 3 cameras, still every 10 s | ~5–7 GB | ~75–110 GB, too much for 100 GB |

To stay within the disk:
- Turn on **Store only the marked monitors** for each camera (Calibrate page) once its monitors are marked. The
  saving depends on how much of the picture is background: in the demo, where monitors fill most of the view, it
  was about 20%.
- Frame several monitors per camera (3–5 on a shelf is fine).
- Use `still_interval_s: 15` or `30` if the monitors only update every 30–60 s anyway.
- Archive finished tests off the server.

The dashboard warns when the disk passes 90%.

## 6. Updating

```bash
cd ~/aq && git pull && cd deploy && docker compose up -d --build
```

Database tables are created automatically, and new optional columns from newer versions are added on start.
There is no full migration tool yet. If a future change alters existing columns, it will come with a migration step.

## Useful commands

```bash
docker compose logs -f api worker
docker compose exec db psql -U aq aq
docker compose restart worker
docker compose down
```

- `logs -f api worker`: follow what the server is doing.
- `exec db psql -U aq aq`: open a database shell.
- `restart worker`: restart the OCR worker.
- `down`: stop everything. Data stays in the Docker volumes.
