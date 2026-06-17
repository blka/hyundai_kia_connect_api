"""Diagnostic script for Hyundai USA (BlueLink) refresh_access_token — round 2.

Re-investigation of issue #1186. The first diagnostic concluded refresh is
unsupported, but every refresh variant failed with a *field-validation* error
(password/username required) — the endpoint validates username+password
before inspecting grant_type. This round tests the combinations never sent:

  T1: all-fields JSON (username, password, grant_type, refresh_token)
  T2: all-fields form-urlencoded (the EU/Type1 transport)
  T3: Basic auth + form-urlencoded, no username/password (the Type1 pattern)

Run: cd hyundai_kia_connect_api && python scripts/hyundai_usa_refresh_diagnostic.py

Requires .env with HYUNDAI_USA_USERNAME / HYUNDAI_USA_PASSWORD / HYUNDAI_USA_PIN.

Output is paste-safe: only HTTP status, errorSubCode/errorSubMessage, masked
token prefixes, and timing are logged. No raw response bodies. urllib3/requests
debug logging is disabled — /enrollment/details responses contain VIN,
address, phone and must not be logged.
"""

import base64
import logging
import os
import sys
import time

try:
    from dotenv import load_dotenv

    load_dotenv(
        os.path.join(os.path.dirname(__file__), "..", "tests", "integration", ".env")
    )
except ImportError:
    load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d %(levelname)-5s %(message)s",
    datefmt="%H:%M:%S",
)
# HTTP-layer debug logging logs raw response bodies (VIN/address/phone in
# /enrollment/details). Paste-safety depends on suppressing it.
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("requests").setLevel(logging.WARNING)
L = logging.getLogger("USA_REFRESH_DIAG")


def _mask(value: str | None) -> str:
    if not value:
        return "None"
    if len(value) <= 8:
        return "***"
    return f"{value[:6]}...{value[-4:]}"


def _mask_username(u: str | None) -> str:
    if not u:
        return "None"
    if len(u) <= 6:
        return "***"
    return f"{u[:3]}***{u[-3:]}"


def _post(session, url, headers, **kwargs):
    """POST and return (response, elapsed_seconds). kwargs are json= or data=."""
    t0 = time.time()
    resp = session.post(url, headers=headers, **kwargs)
    return resp, time.time() - t0


def _log_result(label, resp, elapsed, login_access, login_refresh) -> bool:
    """Log curated fields only — never the raw body. Return True if 200 + token."""
    if resp.status_code == 200:
        try:
            body = resp.json()
        except ValueError:
            L.info(f"[{label}] HTTP 200 but non-JSON body (not logged)  {elapsed:.2f}s")
            return False
        new_access = body.get("access_token")
        new_refresh = body.get("refresh_token")
        L.info(f"[{label}] HTTP 200 ✅  {elapsed:.2f}s")
        L.info(
            f"  access_token:  {_mask(new_access)}  "
            f"(changed vs login: {new_access != login_access})"
        )
        L.info(
            f"  refresh_token: {_mask(new_refresh)}  "
            f"(rotated: {bool(new_refresh) and new_refresh != login_refresh})"
        )
        L.info(f"  expires_in:    {body.get('expires_in')}")
        L.info(f"  token_type:     {body.get('token_type', 'Bearer')}")
        return bool(new_access)
    try:
        body = resp.json()
        L.info(
            f"[{label}] HTTP {resp.status_code}  {elapsed:.2f}s  "
            f"errorSubCode={body.get('errorSubCode')!r}  "
            f"errorSubMessage={body.get('errorSubMessage')!r}"
        )
    except ValueError:
        L.info(
            f"[{label}] HTTP {resp.status_code}  {elapsed:.2f}s  "
            f"(non-JSON, body not logged)"
        )
    return False


def run():
    L.info("=" * 70)
    L.info("Hyundai USA (BlueLink) — refresh_access_token diagnostic (round 2)")
    L.info("=" * 70)

    username = os.environ.get("HYUNDAI_USA_USERNAME") or os.environ.get(
        "HYUNDAI_USERNAME"
    )
    password = os.environ.get("HYUNDAI_USA_PASSWORD") or os.environ.get(
        "HYUNDAI_PASSWORD"
    )
    pin = os.environ.get("HYUNDAI_USA_PIN") or os.environ.get("HYUNDAI_PIN")

    if not username or not password:
        L.error(
            "Missing credentials. Set HYUNDAI_USA_USERNAME and HYUNDAI_USA_PASSWORD in .env"
        )
        L.error("Also set HYUNDAI_USA_PIN if you have one.")
        sys.exit(1)

    L.info(f"Username: {_mask_username(username)}")

    # Derive region/brand ints from the library's own constants — never hardcode.
    # (The first diagnostic hardcoded REGION_USA = 1, which is EUROPE; USA is 3.)
    from hyundai_kia_connect_api import VehicleManager
    from hyundai_kia_connect_api.const import BRANDS, REGIONS, BRAND_HYUNDAI, REGION_USA

    region_id = next(k for k, v in REGIONS.items() if v == REGION_USA)
    brand_id = next(k for k, v in BRANDS.items() if v == BRAND_HYUNDAI)
    L.info(
        f"Region int: {region_id} ({REGION_USA}), Brand int: {brand_id} ({BRAND_HYUNDAI})"
    )

    # ── Phase 1: login via the real HyundaiBlueLinkApiUSA ──────────────
    L.info("\n── Phase 1: login via VehicleManager (HyundaiBlueLinkApiUSA)")
    vm = VehicleManager(
        username=username,
        password=password,
        pin=pin or "",
        region=region_id,
        brand=brand_id,
    )
    api = vm.api  # HyundaiBlueLinkApiUSA instance
    L.info(f"API impl: {type(api).__name__}  LOGIN_API: {api.LOGIN_API}")

    t0 = time.time()
    try:
        vm.login()
    except Exception as e:  # noqa: BLE001 — diagnostic, want all failures
        L.error(f"Login failed: {e!r}  — cannot proceed")
        sys.exit(1)
    login_time = time.time() - t0
    token = vm.token
    if not token or not token.refresh_token:
        L.error("Login succeeded but no refresh_token returned — cannot test refresh")
        sys.exit(1)
    login_access = token.access_token
    login_refresh = token.refresh_token
    L.info(
        f"Login: {login_time:.2f}s  access_token={_mask(login_access)}  "
        f"refresh_token={_mask(login_refresh)}"
    )

    session = api.sessions  # NOTE: plural — HyundaiBlueLinkApiUSA uses self.sessions
    base_headers = dict(api.API_HEADERS)
    oauth_url = api.LOGIN_API + "oauth/token"
    client_id = api.API_HEADERS["client_id"]
    client_secret = api.API_HEADERS["clientSecret"]

    # ── Phase 2: 3-call matrix ────────────────────────────────────────
    L.info("\n── Phase 2: refresh matrix (3 attempts)")

    # T1: all-fields JSON — does grant_type get honored when username+password
    # satisfy the validator? (The combination the first diagnostic never sent.)
    t1_headers = dict(base_headers)
    t1_body = {
        "username": username,
        "password": password,
        "grant_type": "refresh_token",
        "refresh_token": login_refresh,
    }
    resp1, elapsed1 = _post(session, oauth_url, t1_headers, json=t1_body)
    ok1 = _log_result(
        "T1 json all-fields", resp1, elapsed1, login_access, login_refresh
    )

    # T2: all-fields form-urlencoded — the EU/Type1 transport.
    t2_headers = dict(base_headers)
    t2_headers["content-type"] = "application/x-www-form-urlencoded"
    t2_body = {
        "username": username,
        "password": password,
        "grant_type": "refresh_token",
        "refresh_token": login_refresh,
    }
    resp2, elapsed2 = _post(session, oauth_url, t2_headers, data=t2_body)
    ok2 = _log_result(
        "T2 form all-fields", resp2, elapsed2, login_access, login_refresh
    )

    # T3: Type1 pattern — Basic auth (client creds) + form-urlencoded, no
    # username/password. Tests whether a standard OAuth2 refresh mode exists
    # on this endpoint at all. The custom client_id/clientSecret headers are
    # dropped and replaced by Authorization: Basic.
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    t3_headers = {
        k: v for k, v in base_headers.items() if k not in ("client_id", "clientSecret")
    }
    t3_headers["Authorization"] = f"Basic {basic}"
    t3_headers["content-type"] = "application/x-www-form-urlencoded"
    t3_body = {"grant_type": "refresh_token", "refresh_token": login_refresh}
    resp3, elapsed3 = _post(session, oauth_url, t3_headers, data=t3_body)
    ok3 = _log_result(
        "T3 basic+form (Type1)", resp3, elapsed3, login_access, login_refresh
    )

    winner = None
    if ok1:
        winner = ("T1", resp1, elapsed1)
    elif ok2:
        winner = ("T2", resp2, elapsed2)
    elif ok3:
        winner = ("T3", resp3, elapsed3)

    # ── Phase 3: verify a winning token works ────────────────────────
    if winner:
        label, resp, elapsed = winner
        new_access = resp.json().get("access_token")
        L.info(f"\n── Phase 3: verify new access_token from {label}")
        verify_headers = dict(base_headers)
        verify_headers["username"] = username
        verify_headers["accessToken"] = new_access
        if pin:
            verify_headers["blueLinkServicePin"] = pin
        vr = session.get(
            api.API_URL + "enrollment/details/" + username, headers=verify_headers
        )
        L.info(f"enrollment/details: HTTP {vr.status_code}")
        if vr.status_code == 200:
            L.info("✅ new access_token is valid — can fetch vehicle list")
        else:
            L.warning(f"⚠️ new access_token may not work (HTTP {vr.status_code})")

        L.info("\n" + "=" * 70)
        L.info("SUMMARY")
        L.info("=" * 70)
        L.info(f"Login:    {login_time:.2f}s")
        L.info(
            f"Refresh:  {elapsed:.2f}s ({label})  "
            f"{login_time / max(elapsed, 0.001):.1f}x faster than login"
        )
        L.info("Result: ✅ refresh_access_token WORKS — implement the override")
    else:
        L.info("\n" + "=" * 70)
        L.info("SUMMARY")
        L.info("=" * 70)
        L.info(f"Login:    {login_time:.2f}s")
        L.info(
            "Refresh:  all 3 attempts failed (see errorSubCode/errorSubMessage above)"
        )
        L.info(
            "Result: ❌ endpoint requires username+password and honors no "
            "refresh grant on /v2/ac/oauth/token — wontfix confirmed"
        )
    L.info("=" * 70)

    L.info("\nPaste-safe output — no credentials or raw response bodies logged.")
    L.info("Please share this full output in the GitHub issue.")


if __name__ == "__main__":
    run()
