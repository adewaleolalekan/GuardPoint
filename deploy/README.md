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

# The tunnel UUID is the name of the JSON cloudflared just wrote.
TUNNEL_ID=$(basename ~/.cloudflared/*.json .json)

sudo mkdir -p /etc/cloudflared
sudo cp /opt/guardpoint/deploy/cloudflared.yml /etc/cloudflared/config.yml
sudo cp ~/.cloudflared/"${TUNNEL_ID}".json /etc/cloudflared/
sudo sed -i "s|<TUNNEL-ID>|${TUNNEL_ID}|g" /etc/cloudflared/config.yml
sudo chmod 600 /etc/cloudflared/*.json

# Sanity check: the second line must name a file that exists.
grep -E '^(tunnel|credentials-file):' /etc/cloudflared/config.yml
sudo ls -l /etc/cloudflared/

# Test in the foreground BEFORE installing the service. If the config is
# wrong, cloudflared exits here with a real message instead of an opaque
# systemd exit code. Expect "Registered tunnel connection". Ctrl-C to stop.
sudo cloudflared --config /etc/cloudflared/config.yml tunnel run

sudo cloudflared service install
cloudflared tunnel route dns guardpoint guardpoint-test.waletech.org
```

### Troubleshooting

**`cloudflared.service` fails to start with exit code 1.** Almost always a
bad `credentials-file` path in `/etc/cloudflared/config.yml` — typically the
literal `<TUNNEL-ID>` placeholder was never substituted. Check with the
`grep` above, fix it, then `sudo systemctl restart cloudflared`.

**`cp: cannot stat '.../YOUR-ID.json'`.** You are meant to substitute the
*real* UUID, not the literal text `YOUR-ID`. Use `TUNNEL_ID=$(basename
~/.cloudflared/*.json .json)` as above.

**502 from Cloudflare.** The tunnel is up but the app is not. Check
`curl -s http://127.0.0.1:8000/api/health` and `journalctl -u guardpoint`.

### Rotating tunnel credentials

There is **no in-place rotation** for a locally-managed tunnel. Cloudflare's
docs state the tunnel credential (`.json`) "does not expire", and
`cloudflared` reports at creation time: *"To revoke these credentials,
delete the tunnel."* Deleting and recreating the tunnel is the only way.

The leaked value matters only for that one tunnel — the account `cert.pem`
is a separate, account-wide credential and is unaffected, so no
`cloudflared tunnel login` is required.

```bash
sudo systemctl stop cloudflared 2>/dev/null
sudo cloudflared service uninstall 2>/dev/null

# Deleting the tunnel is what revokes the old secret.
cloudflared tunnel delete -f guardpoint

rm -f ~/.cloudflared/OLD-TUNNEL-ID.json
sudo rm -f /etc/cloudflared/OLD-TUNNEL-ID.json

cloudflared tunnel create guardpoint
TUNNEL_ID=$(basename ~/.cloudflared/*.json .json)

sudo cp ~/.cloudflared/"${TUNNEL_ID}".json /etc/cloudflared/
sudo cp /opt/guardpoint/deploy/cloudflared.yml /etc/cloudflared/config.yml
sudo sed -i "s|<TUNNEL-ID>|${TUNNEL_ID}|g" /etc/cloudflared/config.yml
sudo chmod 600 /etc/cloudflared/*.json

# Foreground test first.
sudo cloudflared --config /etc/cloudflared/config.yml tunnel run

cloudflared tunnel route dns guardpoint guardpoint-test.waletech.org
sudo cloudflared service install
```

The old CNAME record still points at the deleted tunnel ID, so
`tunnel route dns` may fail with "record already exists". Delete that CNAME
in the Cloudflare dashboard and re-run it.

Do the rotation **before** configuring Cloudflare Access: Access
applications bind to the hostname, not the tunnel, so a policy would
survive — but it is cleaner to set it up once, afterwards.

### 5. Gate it with Cloudflare Access — do not skip

Zero Trust dashboard → **Access → Applications → Add an application →
Self-hosted** → hostname `guardpoint-test.waletech.org` → add a policy.

**Leave the Path field blank** (or `/*`). If it is set to a sub-path, only
that path is gated and the dashboard root stays public.

The policy needs **two** rows. Omitting the second is the most common
mistake — the login page appears and claims an email was sent, but nothing
arrives, because the application has no permitted login method:

| Action | Rule type | Selector | Value |
| --- | --- | --- | --- |
| Allow | Include | Emails | `you@waletech.org` |
| Allow | Require | Login Method | `One-time PIN` |

**Policy logic — this bites people:**

* `Include` values are **OR**ed. Several emails in one Include rule is fine.
* `Require` values are **AND**ed. Two different emails in a Require rule can
  never both match, so *nobody* gets in.

So put your address under **Include**, never under **Require**.

Avoid these — Cloudflare flags both as "anyone will be able to access your
application":

* `Include → Everyone`
* `Include → Emails ending in` with a broad or empty domain

**Verify from outside the network:**

```bash
curl -sI https://guardpoint-test.waletech.org | head -5
```

* `302` to `https://<team>.cloudflareaccess.com/cdn/cgi/access/login/...`
  means Access is active. Good.
* `200` means Access is **not** applying — the hostname or path does not
  match the application. Fix that before anything else.

**Diagnosing:** Zero Trust → **Logs → Access** shows every request with its
policy decision and the reason. Use it instead of guessing at the UI.

Note that the login page always claims the PIN email was sent, even when no
policy matched — Cloudflare does this to prevent account enumeration. If no
email arrives, the policy is the problem, not email delivery.

**Other known causes:** Firefox Private Window blocks the `CF_Authorization`
cookie on XHR, which breaks the dashboard's `fetch()` calls; two Access
applications on the same hostname conflict; and an existing session cookie
may mean you are never re-prompted (test in a fresh private window).

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
