# Deploying GuardPoint

Target: a single small Ubuntu 24.04 LTS instance, reached through a
Cloudflare Tunnel at `guardpoint-test.waletech.org`.

## Why this shape

| Decision | Reason |
|---|---|
| **EC2, not Lambda/App Runner/Fargate** | The app persists to a local SQLite file (`guardpoint.db`). Serverless storage is ephemeral, so the verification records and audit trail would vanish on every deploy or scale event. |
| **Cloudflare Tunnel, not an ALB** | No inbound ports, no public IP, no certificates. `cloudflared` dials outbound. |
| **Cloudflare Access** | **The app has no authentication of its own.** Without Access, anyone who learns the hostname can read every client record and the full audit trail, including director names, RC numbers and TINs. |
| **`--workers 1`** | `Store` guards writes with an in-process `threading.Lock`, which does not coordinate across processes. Multiple workers risk `database is locked`. |
| **Bind `127.0.0.1`** | The service must never be reachable except via the tunnel. |
| **Ubuntu 24.04 LTS** | Python 3.12 clears the code's 3.10+ floor (`str \| None` in Pydantic models is resolved at runtime). Every dependency has a prebuilt aarch64 wheel, so nothing compiles on Graviton. |

## Instance sizing

`t4g.small` (2 vCPU, 2 GiB, arm64) is far more than enough — measured
footprint is ~57 MB RAM under load and ~71 MB of disk for the virtualenv.

> **Check your free-tier eligibility before you build.** The AWS free
> tier changed on 15 July 2025:
> * Accounts opened **before** that date keep the 12-month trial
>   (750 hrs/mo of a micro instance).
> * Accounts opened **after** get a **credit-based Free Plan** — $100
>   plus up to $100 more, lasting **6 months or until credits run out**,
>   after which the account **closes** (90-day grace, then resources are
>   erased). Don't put anything durable on it.
>
> The `t4g.small` free trial runs to **31 Dec 2026** and, per AWS's
> documented region list, applies in e.g. `eu-west-1`/`eu-west-2` but
> **not** in `af-south-1` (Cape Town), which is also an opt-in region
> and priced ~14% above average. Confirm in the console: the instance
> type shows a "Free tier eligible" badge when it applies.

## Steps

### 1. Launch the instance

Ubuntu 24.04 LTS (arm64), `t4g.small`, ~20 GB `gp3`. For SSH either
restrict port 22 to your own IP, or use SSM Session Manager and open no
inbound ports at all. You do **not** need to open 80/443 — the tunnel
handles ingress.

### 2. Provision

```bash
sudo apt-get update && sudo apt-get install -y git
sudo git clone https://github.com/adewaleolalekan/GuardPoint.git /tmp/gp
sudo bash /tmp/gp/deploy/provision.sh
```

This creates the `guardpoint` service account, clones to `/opt/guardpoint`,
builds the virtualenv, writes `.env` with mode `600`, installs the systemd
unit, and waits for `/api/health` to answer.

### 3. Add the API key (optional)

Out of the box the app runs against its **built-in simulator** — no
network calls, no credits consumed. To use the live NIPOST gateway:

```bash
sudo -u guardpoint editor /opt/guardpoint/.env   # set POSTCODE_API_KEY=...
sudo systemctl restart guardpoint
```

`nipost_test_...` keys are free and unmetered; `nipost_live_...` keys
consume credits for L2+ lookups. The key stays server-side — the
dashboard never sees it.

### 4. Publish through the tunnel

```bash
# install cloudflared
curl -fsSL https://pkg.cloudflare.com/cloudflare-main.gpg \
  | sudo tee /usr/share/keyrings/cloudflare-main.gpg >/dev/null
echo "deb [signed-by=/usr/share/keyrings/cloudflare-main.gpg] \
https://pkg.cloudflare.com/cloudflared any main" \
  | sudo tee /etc/apt/sources.list.d/cloudflared.list
sudo apt-get update && sudo apt-get install -y cloudflared

cloudflared tunnel login
cloudflared tunnel create guardpoint

sudo mkdir -p /etc/cloudflared
sudo cp /opt/guardpoint/deploy/cloudflared.yml /etc/cloudflared/config.yml
# replace <TUNNEL-ID> with the id printed by `tunnel create`
sudo sed -i 's/<TUNNEL-ID>/YOUR-ID/' /etc/cloudflared/config.yml
sudo cp ~/.cloudflared/YOUR-ID.json /etc/cloudflared/

cloudflared tunnel route dns guardpoint guardpoint-test.waletech.org
sudo cloudflared service install
```

### 5. Gate it with Cloudflare Access — do not skip

Zero Trust dashboard → **Access → Applications → Add an application →
Self-hosted** → hostname `guardpoint-test.waletech.org` → add a policy
allowing only your email (or your Google/GitHub SSO group).

Until this exists, the dashboard, the API, `/docs` and the audit trail are
all world-readable.

### 6. Verify

```bash
curl -s http://127.0.0.1:8000/api/health     # on the box
curl -s https://guardpoint-test.waletech.org/api/health   # externally
```

## Operating notes

```bash
journalctl -u guardpoint -f          # application logs
systemctl status guardpoint          # service state
sudo -u guardpoint /opt/guardpoint/.venv/bin/python -m pytest /opt/guardpoint/tests -q
```

**Updating:** `sudo bash /opt/guardpoint/deploy/provision.sh` — it pulls
`origin/main` and restarts. `.env` is preserved.

**Backups:** `guardpoint.db` is the whole state. With WAL enabled there are
also `-wal` and `-shm` sidecar files; copy all three together, or use
`sqlite3 guardpoint.db ".backup /path/to/backup.db"`.

## Not covered here

* **No Nigerian region.** Nearest AWS region is Cape Town (~5,000 km) and
  OCI's is Johannesburg. Client records contain director names and TINs —
  personal data under the NDPA. Fine for synthetic test data; for real
  records, document the transfer basis first.
* **Single instance, no HA.** Appropriate for a test, not for production.
* **The app has no user accounts.** Cloudflare Access is the only
  authentication. If the app ever stores real client data, add auth in the
  application as well.
