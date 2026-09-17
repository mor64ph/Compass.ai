# Deploying Compass

> ### Which guide do you want?
>
> **No credit card?** Oracle's Always Free tier still requires one for identity
> verification, and so do Fly, Railway and Google Cloud. Use
> **[deploy/huggingface/README-DEPLOY.md](../deploy/huggingface/README-DEPLOY.md)**
> instead — Hugging Face Spaces needs no card and gives 2 vCPU / 16 GB, which is
> the only card-free tier with enough memory to run Compass without dropping the
> embedding model. The trade is an ephemeral disk, so data resets on restart;
> `COMPASS_DEMO_MODE` exists to make that a feature rather than a fault.
>
> **This guide** is for a host you control — an Oracle Always Free instance, or
> any Linux box with Docker. It is the better option when you have one: 24 GB of
> RAM, a persistent volume, and your own domain.

Target: an **Oracle Cloud Always Free** ARM instance — 4 cores, 24 GB RAM,
200 GB disk, free indefinitely rather than for a trial period. Compass needs
about 1 GB of that, so there is room to spare.

Nothing here is Oracle-specific except §1. The Docker setup runs on any Linux
host with Docker installed.

> **Why not Render, Railway or Fly?** Checked in September 2026: Fly's free tier
> is gone (a 2-hour trial for new accounts), and Render, Railway and Koyeb all
> cap free compute around 512 MB with no free persistent disk. Compass needs
> ~700 MB resident for the embedding model and a real disk for SQLite, so all
> three would mean either paying or dropping semantic scoring — which weakens the
> quality gate's templated-letter detection, the feature the whole tool is built
> around. Those are the trade-offs, if you would rather make them differently.

**Time:** about 45 minutes, most of it waiting for Oracle and for the image build.

---

## 1. An Oracle Always Free instance

1. Sign up at [cloud.oracle.com](https://cloud.oracle.com). Pick a **home region
   near you** — it cannot be changed later. Mumbai or Hyderabad for India.
   A card is required for identity verification; Always Free resources do not
   charge it. Keep the account on Always Free if you want that guaranteed.
2. **Compute → Instances → Create instance**
   - Image: **Ubuntu 24.04**
   - Shape: **Change shape → Ampere → VM.Standard.A1.Flex**, then
     **4 OCPUs / 24 GB** — the whole free allowance in one instance
   - Add your SSH public key (generate with `ssh-keygen -t ed25519` if needed)
   - Boot volume: 100–200 GB
3. Note the **public IP**.

> **"Out of host capacity"** is the normal first experience with A1.Flex — the
> free ARM shapes are heavily contested. Try a different availability domain,
> try again later, or ask for 2 OCPUs / 12 GB, which is still plenty. Retrying on
> a loop for a few hours is the usual answer.

### 1.1 Open the firewall — both of them

This is where most Oracle deployments stall. There are **two** independent
firewalls and traffic needs to pass both.

**a. The VCN security list** (Oracle's side)
Networking → Virtual Cloud Networks → your VCN → Subnets → your subnet →
Security Lists → default → **Add Ingress Rules**:

| Source CIDR | Protocol | Destination port |
|---|---|---|
| `0.0.0.0/0` | TCP | 80 |
| `0.0.0.0/0` | TCP | 443 |

**b. iptables on the host** (Ubuntu's side — Oracle's images ship with
everything but SSH blocked):

```bash
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 80 -j ACCEPT
sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport 443 -j ACCEPT
sudo netfilter-persistent save
```

Miss either one and Caddy's certificate request times out with no useful error.

---

## 2. A hostname

TLS needs a name, not an IP. Free option — [DuckDNS](https://www.duckdns.org):
sign in, pick a subdomain, point it at your public IP. You get
`yourname.duckdns.org`, which Let's Encrypt will happily issue for.

If you own a domain, an `A` record to the public IP is better.

**Confirm it resolves before continuing.** An ACME challenge against a wrong
record burns one of five attempts per hostname per hour:

```bash
dig +short yourname.duckdns.org    # must print your instance's public IP
```

---

## 3. Docker

```bash
ssh ubuntu@<your-public-ip>

sudo apt-get update && sudo apt-get upgrade -y
sudo install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg \
  | sudo gpg --dearmor -o /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo $VERSION_CODENAME) stable" \
  | sudo tee /etc/apt/sources.list.d/docker.list > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin

sudo usermod -aG docker $USER && newgrp docker
docker run --rm hello-world
```

---

## 4. Compass

```bash
sudo mkdir -p /opt/compass && sudo chown $USER:$USER /opt/compass
git clone https://github.com/mor64ph/ai-jobsearch-engine.git /opt/compass
cd /opt/compass

cp deploy/env.deploy.example .env.deploy
chmod 600 .env.deploy
```

Edit `.env.deploy` and set, at minimum:

```bash
COMPASS_DOMAIN=yourname.duckdns.org
ACME_EMAIL=you@example.com
COMPASS_SECRET_KEY=<paste the output of the command below>
GEMINI_API_KEY=<your key>
```

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(48))"
```

**The secret key is not optional.** Compass refuses to start without a real one
when bound to anything but loopback, because the placeholder in
`app/config.py` is published in a public repository — leaving it in place would
let anyone who reads the repo forge a session cookie for your admin account.
`app/security.py::assert_deployable` enforces that, so a mistake here is a clear
startup failure rather than a silent hole.

---

## 5. Build and start

```bash
docker compose up -d --build
```

The first build takes **10–20 minutes** on 4 ARM cores. Most of it is compiling
wheels that have no prebuilt arm64 version, plus baking the embedding model into
the image so the first scoring request is instant and the container needs no
outbound network.

```bash
docker compose logs -f app     # wait for "Compass startup complete"
docker compose ps              # both services should read healthy
```

Then open `https://yourname.duckdns.org`. Caddy will have obtained a certificate
on the first request; the very first load can take a few seconds while it does.

`/setup` creates the owner account. **Do that immediately** — until you do, the
page is open to whoever reaches it first. It closes permanently afterwards, and
everyone else joins by invite from Settings.

---

## 6. Backups

`compass.db` is a single file holding a career history. Losing it loses
everything.

```bash
./deploy/backup.sh
(crontab -l 2>/dev/null; echo "0 3 * * * cd /opt/compass && ./deploy/backup.sh >> /var/log/compass-backup.log 2>&1") | crontab -
```

It uses SQLite's `.backup` rather than `cp`. With WAL enabled — which it is — a
plain copy taken mid-write is missing whatever is still in the `-wal` file, and
you discover that at restore time. The script also reopens each backup and checks
the expected tables are readable, because a backup nobody has restored is a
hypothesis rather than a backup.

**Get them off the box.** An Always Free instance is not a durable store:

```bash
rsync -avz ubuntu@<ip>:/opt/compass/backups/ ./compass-backups/
```

### Restoring

```bash
docker compose stop app
gzip -dc backups/compass-20260917T030000Z.db.gz > /tmp/restore.db
docker compose cp /tmp/restore.db app:/data/compass.db
docker compose start app
```

---

## 7. Updating

```bash
cd /opt/compass
./deploy/backup.sh          # first, always
git pull
docker compose up -d --build
```

The database is on a named volume, so a rebuild does not touch it.

> **No migrations yet.** Compass has no Alembic, so a schema change means the new
> code meets an old database. Adding a table is safe — `create_all` handles it,
> which is how discovery's tables arrived. Changing or removing a *column* is
> not: back up, and check the diff for model changes before pulling.

---

## 8. Post-deploy checklist

```
[ ] https:// loads with a valid certificate (no browser warning)
[ ] http:// redirects to https://
[ ] Owner account created, so /setup is closed
[ ] Signing out and back in works — proves the Secure cookie is set correctly
[ ] A résumé uploads and gets an ATS score
[ ] One AI action completes (tailor, or a gap report)
[ ] backup.sh runs clean and reports verified tables
[ ] Backups copied off the instance
[ ] curl -sI https://yourdomain/login | grep -i content-security-policy
```

That last one confirms the security headers survived the reverse proxy. Caddy is
deliberately configured not to add its own, so if it is missing, the header
middleware is not running.

---

## Operating notes

**Oracle reclaims idle instances.** Always Free compute can be reclaimed after
7 days of near-zero CPU. Compass's embedding preload and Caddy's certificate
renewals generate some, but an instance nobody visits for weeks is at risk. The
cron backup at 03:00 is enough to keep it counted as active.

**Gemini's free tier is 20 requests per day per model**, and it is shared across
everyone you invite, because it is one key. Two or three people will exhaust it.
Switch `COMPASS_GEMINI_MODEL` for a fresh allowance, or add billing.

**Long requests are expected.** Tailoring runs 25–60s, prep briefs longer, a
ranking pass about a minute. Caddy's timeouts are set to 15 minutes for exactly
this; the defaults would cut off the most valuable requests.

**Logs.** `docker compose logs -f app`. Caddy's access log is errors-only on
purpose: Compass's own paths name the companies being applied to, so a full
access log is career data.

**Sizing.** Idle is roughly 700 MB resident, mostly the embedding model; a
ranking pass peaks around 1.5 GB. The compose file caps the app at 3 GB so a
runaway generation cannot take the host with it. On 24 GB there is a lot of
headroom — you could run several other things beside it.
