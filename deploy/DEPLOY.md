# Running NB-Automation on the website server (automation.newsband.in)

This runs the dashboard and autopilot **next to** the main website on the same Linux server. The
main site's files and config are not touched. The app runs as its own user, on
`127.0.0.1:8001`, and the web server that already serves newsband.in forwards
`automation.newsband.in` to it.

```
browser ──https──> Cloudflare ──> nginx/Apache (already running the main site)
                                     └── automation.newsband.in ──> gunicorn 127.0.0.1:8001 (this app, 1 worker)
```

## Before you start

- **The server needs:** Linux with systemd, Python **3.11 or newer** (3.12 recommended), **1.5 GB of RAM
  for this app** (measured peak ~640 MB), **3 GB of disk** (~2 GB installed), and a share of
  one CPU core (it is idle almost all the time).
- **You need shell access with sudo.** Shared cPanel hosting cannot run this: the autopilot is a
  process that runs all the time.
- **⚠ Only one copy of the autopilot may run.** When the server copy starts, **close the
  dashboard on the office PC for good**. (Alternatively, put `NB_AUTOPILOT=0` in the PC's `.env`
  so it starts paused, and never press Resume there.) Two copies
  would publish the same stories twice. Their 2–3 minute publish spacing would also not know
  about each other, and that burst posting is what got the account suspended on 2026-09-19.

## 1. DNS (Cloudflare)

In the newsband.in zone, add an `A` record: `automation` → the server's IP. Leave it proxied
(orange cloud). Set SSL/TLS mode to **Full (strict)** after step 5 has issued the certificate.

## 2. Code and Python environment

```bash
sudo useradd --system --no-create-home --home-dir /opt/nb-automation --shell /usr/sbin/nologin nbauto
sudo mkdir /opt/nb-automation && sudo chown nbauto:nbauto /opt/nb-automation
sudo -u nbauto git clone https://github.com/Manas236/NB-Automation.git /opt/nb-automation
# (for a private repo, clone with a deploy key or a personal access token)

cd /opt/nb-automation
sudo -u nbauto python3 -m venv venv
sudo -u nbauto venv/bin/pip install --upgrade pip
sudo -u nbauto venv/bin/pip install torch --index-url https://download.pytorch.org/whl/cpu   # CPU-only, ~200 MB instead of ~3 GB
sudo -u nbauto venv/bin/pip install -r deploy/requirements-server.txt
sudo -u nbauto venv/bin/python -m spacy download en_core_web_sm
```

## 3. `.env` (secrets: never commit this file)

Copy the office PC's `.env` to `/opt/nb-automation/.env`, for example with `scp` or WinSCP. Then
add these lines:

```ini
# Dashboard login (required on the server; the app will not start without it)
NB_DASH_USER=newsband
NB_DASH_PASSWORD=<a long random password>
```

To generate a password, run `python3 -c "import secrets; print(secrets.token_urlsafe(24))"`.

```bash
sudo chown nbauto:nbauto /opt/nb-automation/.env && sudo chmod 600 /opt/nb-automation/.env
```

## 4. Bring the state over from the office PC

Stop the dashboard on the PC first. Then copy these from the PC's project folder into
`/opt/nb-automation/`:

| Copy | Why |
|---|---|
| `seen_urls.json` | Without it the server treats every story currently in the sitemap as new. |
| `meta/` and `output/` | Publish history: the duplicate guards and the 2–3 min gap read it from here. |
| `autopilot_mode.json`, `autopost_state.json` | The chosen mode (Create only / Create & post) and today's counters. |

```bash
sudo chown -R nbauto:nbauto /opt/nb-automation
```

If you skip this step nothing breaks badly: on first start the server marks everything in the
sitemap as seen and only handles stories published after that. The gallery starts empty.

## 5. Service and web server

```bash
sudo cp /opt/nb-automation/deploy/nb-automation.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now nb-automation
sudo journalctl -u nb-automation -f          # should print "autopilot: polling every 15 min ..."
curl -I http://127.0.0.1:8001/               # expect 401 (login required) = working
```

Then use **one** of the two web-server configs, whichever serves the main website. Each file's
header has the exact commands:

- nginx: `deploy/nginx-automation.newsband.in.conf`
- Apache: `deploy/apache-automation.newsband.in.conf`

Finally, get the certificate: `sudo certbot --nginx -d automation.newsband.in` (or `--apache`).

Open https://automation.newsband.in. The browser asks for the login from step 3.

## Day to day

| Task | Command |
|---|---|
| Update to the latest code | `cd /opt/nb-automation && sudo -u nbauto git pull && sudo systemctl restart nb-automation` |
| Live log | `sudo journalctl -u nb-automation -f` |
| Restart / stop | `sudo systemctl restart nb-automation` / `sudo systemctl stop nb-automation` |
| After editing `.env` or `tags.yaml` | `sudo systemctl restart nb-automation` |

A restart (or `git pull` + restart) stops the autopilot mid-pass, just like a code edit did on
the PC. Posts that were already queued get queued again automatically. Check the dashboard strip
or log first, so you don't restart halfway through a publish.

## Things to know

- **Cloudflare cuts any request at 100 seconds.** Pasting many links into *Generate* at once can
  show a "524" error page. The posts are still made: refresh and they are in the gallery. Paste a
  few links at a time.
- **The login is the only thing protecting the Instagram account from strangers.** Use a strong
  password and don't share it. For an extra layer, put the subdomain behind **Cloudflare Access**
  (Zero Trust, free for small teams): it adds an email-code login in front of this one.
- `robots`: the web-server configs send `X-Robots-Tag: noindex`, so search engines won't list
  the dashboard.
- The office PC can still run `python app.py` for testing, but only with `NB_AUTOPILOT=0` in its
  `.env`, and don't press Resume there. The autopilot must run in one place only.
