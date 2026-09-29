"""
Production entry point (Linux server, automation.newsband.in). See deploy/DEPLOY.md.

    gunicorn --workers 1 --threads 8 --bind 127.0.0.1:8001 wsgi:app

ONE worker, always: the autopilot, the Instagram publish queue and the live status all live in
this process's memory. A second worker would run a second autopilot - double posts, and two
queues that each think the API gap has passed.

On the Windows PC keep using `python app.py` (dev server + auto-reload); this file is not used there.
"""

import os
import secrets

import Insta_poster  # noqa: F401  - loads .env before app.py reads NB_DASH_PASSWORD
import app as dashboard
from werkzeug.middleware.proxy_fix import ProxyFix

if not dashboard.DASH_PASSWORD:
    raise SystemExit("NB_DASH_PASSWORD is not set in .env - refusing to serve the dashboard "
                     "on the internet without a login. See deploy/DEPLOY.md.")

if not os.environ.get("NB_SECRET"):
    dashboard.app.secret_key = secrets.token_hex(32)   # only signs flash messages

# nginx/Apache sit in front: trust their X-Forwarded-For/-Proto/-Host (one hop) so redirects
# stay on https://automation.newsband.in instead of http://127.0.0.1:8001.
dashboard.app.wsgi_app = ProxyFix(dashboard.app.wsgi_app, x_for=1, x_proto=1, x_host=1)

dashboard.start_background()

app = dashboard.app
