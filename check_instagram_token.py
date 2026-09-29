"""Print the validity and remaining lifetime of the Instagram token in ``.env``.

Run from this project directory:

    python check_instagram_token.py

The script sends the token to Meta only to inspect it.  It does not refresh, replace,
or print the token, app secret, or the request URL.  It needs the four Instagram
variables documented in .env.example:

    INSTAGRAM_ACCESS_TOKEN
    INSTAGRAM_ACCOUNT_ID
    INSTAGRAM_APP_ID
    INSTAGRAM_APP_SECRET
"""

import hashlib
import hmac
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

BASE_DIR = Path(__file__).resolve().parent
ENV_PATH = BASE_DIR / ".env"
TIMEOUT_SECONDS = 30
WARN_DAYS = 7


class MetaRequestError(RuntimeError):
    """A credential-safe summary of a rejected Meta API request."""

    def __init__(self, status_code, code=None):
        self.status_code = status_code
        self.code = code
        suffix = f" (code {code})" if code is not None else ""
        super().__init__(f"Meta rejected the request (HTTP {status_code}){suffix}.")


def load_settings():
    """Load only the values needed for token inspection, without ever displaying them."""
    try:
        from dotenv import dotenv_values
    except ImportError as exc:
        raise RuntimeError(
            "python-dotenv is not installed. Run: python -m pip install -r requirements.txt"
        ) from exc

    if not ENV_PATH.is_file():
        raise RuntimeError(".env was not found next to this script (see .env.example).")

    # dotenv_values reads the file without changing the current process environment.
    values = dotenv_values(ENV_PATH)
    settings = {
        name: (values.get(name) or os.environ.get(name) or "").strip()
        for name in (
            "INSTAGRAM_ACCESS_TOKEN",
            "INSTAGRAM_ACCOUNT_ID",
            "INSTAGRAM_APP_ID",
            "INSTAGRAM_APP_SECRET",
            "INSTAGRAM_API_HOST",
            "INSTAGRAM_API_VERSION",
        )
    }
    missing = [
        name
        for name in (
            "INSTAGRAM_ACCESS_TOKEN",
            "INSTAGRAM_ACCOUNT_ID",
            "INSTAGRAM_APP_ID",
            "INSTAGRAM_APP_SECRET",
        )
        if not settings[name]
    ]
    if missing:
        raise RuntimeError("Missing " + ", ".join(missing) + " in .env (see .env.example).")

    settings["INSTAGRAM_API_HOST"] = (settings["INSTAGRAM_API_HOST"] or "graph.instagram.com").lower()
    settings["INSTAGRAM_API_HOST"] = re.sub(r"^https?://", "", settings["INSTAGRAM_API_HOST"]).strip("/")
    if settings["INSTAGRAM_API_HOST"] not in {"graph.instagram.com", "graph.facebook.com"}:
        raise RuntimeError("INSTAGRAM_API_HOST must be graph.instagram.com or graph.facebook.com.")

    settings["INSTAGRAM_API_VERSION"] = settings["INSTAGRAM_API_VERSION"] or "v26.0"
    if not re.fullmatch(r"v\d+\.\d+", settings["INSTAGRAM_API_VERSION"]):
        raise RuntimeError("INSTAGRAM_API_VERSION must look like v26.0.")
    return settings


def human_duration(total_seconds):
    """Format a positive time span compactly and without rounding expiry up."""
    remaining = max(0, int(total_seconds))
    days, remaining = divmod(remaining, 86_400)
    hours, remaining = divmod(remaining, 3_600)
    minutes, _ = divmod(remaining, 60)

    parts = []
    if days:
        parts.append(f"{days} day{'s' if days != 1 else ''}")
    if hours or parts:
        parts.append(f"{hours} hour{'s' if hours != 1 else ''}")
    parts.append(f"{minutes} minute{'s' if minutes != 1 else ''}")
    return ", ".join(parts)


def as_datetime(timestamp):
    """Meta returns Unix seconds; zero means no expiry was supplied."""
    try:
        timestamp = int(timestamp or 0)
    except (TypeError, ValueError):
        return None
    return datetime.fromtimestamp(timestamp, tz=timezone.utc) if timestamp > 0 else None


def meta_error(response):
    """Build a useful error while ensuring no credential can appear in output."""
    try:
        payload = response.json()
    except ValueError:
        return MetaRequestError(response.status_code)

    error = payload.get("error", {}) if isinstance(payload, dict) else {}
    code = error.get("code") if isinstance(error, dict) else None
    # Do not reuse Meta's free-form message: an unusual proxy/API error could echo
    # a request parameter, and this program's contract is never to reveal credentials.
    return MetaRequestError(response.status_code, code)


def inspect_token(settings):
    """Inspect the token through Meta's read-only debug_token endpoint."""
    app_token = f"{settings['INSTAGRAM_APP_ID']}|{settings['INSTAGRAM_APP_SECRET']}"
    appsecret_proof = hmac.new(
        settings["INSTAGRAM_APP_SECRET"].encode("utf-8"),
        app_token.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    url = f"https://graph.facebook.com/{settings['INSTAGRAM_API_VERSION']}/debug_token"
    try:
        response = requests.get(
            url,
            params={
                "input_token": settings["INSTAGRAM_ACCESS_TOKEN"],
                "access_token": app_token,
                "appsecret_proof": appsecret_proof,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.Timeout as exc:
        raise RuntimeError(f"Meta did not answer within {TIMEOUT_SECONDS} seconds.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Meta: {type(exc).__name__}.") from exc

    if response.status_code >= 400:
        raise meta_error(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Meta returned a non-JSON response.") from exc

    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, dict):
        raise RuntimeError("Meta returned an unexpected token-inspection response.")
    return data


def check_instagram_api_access(settings):
    """Confirm the token works at its configured Instagram API host without posting anything."""
    url = (
        f"https://{settings['INSTAGRAM_API_HOST']}/{settings['INSTAGRAM_API_VERSION']}"
        f"/{settings['INSTAGRAM_ACCOUNT_ID']}"
    )
    params = {"fields": "id", "access_token": settings["INSTAGRAM_ACCESS_TOKEN"]}
    if settings["INSTAGRAM_API_HOST"] == "graph.facebook.com":
        params["appsecret_proof"] = hmac.new(
            settings["INSTAGRAM_APP_SECRET"].encode("utf-8"),
            settings["INSTAGRAM_ACCESS_TOKEN"].encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
    try:
        response = requests.get(url, params=params, timeout=TIMEOUT_SECONDS)
    except requests.Timeout as exc:
        raise RuntimeError(f"Instagram did not answer within {TIMEOUT_SECONDS} seconds.") from exc
    except requests.RequestException as exc:
        raise RuntimeError(f"Could not reach Instagram: {type(exc).__name__}.") from exc
    if response.status_code >= 400:
        raise meta_error(response)

    try:
        body = response.json()
    except ValueError as exc:
        raise RuntimeError("Instagram returned a non-JSON response.") from exc
    if not isinstance(body, dict) or not body.get("id"):
        raise RuntimeError("Instagram returned an unexpected account-check response.")


def print_expiry(label, expires_at, now):
    """Print a safe, human-friendly expiry line and return seconds remaining."""
    if expires_at is None:
        print(f"{label}: Meta did not provide an expiry timestamp.")
        return None

    remaining = (expires_at - now).total_seconds()
    timestamp = expires_at.strftime("%Y-%m-%d %H:%M UTC")
    if remaining <= 0:
        print(f"{label}: expired at {timestamp}.")
    else:
        print(f"{label}: {timestamp} ({human_duration(remaining)} remaining).")
    return remaining


def main():
    try:
        settings = load_settings()
        token = inspect_token(settings)
    except MetaRequestError:
        # The generic Facebook debug endpoint sometimes does not provide metadata for
        # Instagram-Login tokens. Check the configured Instagram endpoint before
        # declaring the token expired or invalid.
        try:
            check_instagram_api_access(settings)
        except MetaRequestError as account_error:
            if account_error.code == 190:
                print("Instagram token: INVALID or expired")
                print("Token expiry: cannot be determined because Instagram rejected the token.")
                return 2
            print(f"Token check failed: the Instagram account check also failed: {account_error}", file=sys.stderr)
            return 1
        except RuntimeError as account_error:
            print(f"Token check failed: {account_error}", file=sys.stderr)
            return 1

        print("Instagram token: valid (confirmed with a direct account check)")
        print("Token expiry: Meta did not provide expiry metadata for this token.")
        print("No refresh was performed.")
        return 0
    except RuntimeError as exc:
        print(f"Token check failed: {exc}", file=sys.stderr)
        return 1

    now = datetime.now(timezone.utc)
    valid = bool(token.get("is_valid"))
    print(f"Instagram token: {'valid' if valid else 'INVALID'}")
    remaining = print_expiry("Token expiry", as_datetime(token.get("expires_at")), now)

    # Facebook-login tokens can also have a separate data-access expiration.
    data_access = as_datetime(token.get("data_access_expires_at"))
    if data_access is not None:
        print_expiry("Data-access expiry", data_access, now)

    if not valid:
        return 2
    if remaining is not None and remaining <= 0:
        return 2
    if remaining is not None and remaining < WARN_DAYS * 86_400:
        print(f"Warning: fewer than {WARN_DAYS} days remain; refresh or replace the token soon.")
        return 3
    return 0


if __name__ == "__main__":
    sys.exit(main())
