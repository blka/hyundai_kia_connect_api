#!/usr/bin/env python3
"""
Universal diagnostic dump for Hyundai/Kia/Genesis Connect API.

Works for all regions: EU, USA, CA, AU, IN, CN, BR, NZ.
Handles OTP automatically for USA and CA.

Usage:
    python diagnostic_dump.py --env-file .env

Required settings (in order of precedence):
    1. Command-line flags: --region, --brand, --username, --password, --pin
    2. Environment variables: CC_REGION, CC_BRAND, CC_USERNAME, CC_PASSWORD, CC_PIN
    3. A .env file with those keys

If a setting is provided by more than one source, command-line flags win,
then env vars, then .env.

Output is written to a timestamped diagnostics/ directory next to this script:
    diagnostics/2026-06-27_153042_eu_hyundai/
    ├── api_capabilities.json
    ├── raw_request_*.json                 (every HTTP call made by the library)
    ├── raw_vehicle_{id}_profile.json   (ApiImplType1 /profile endpoint)
    └── vehicle_{id}.json               (full parsed Vehicle after cached update)
"""

import argparse
import json
import os
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

from hyundai_kia_connect_api import VehicleManager
from hyundai_kia_connect_api.const import BRANDS, OTP_NOTIFY_TYPE, REGIONS
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.Vehicle import Vehicle


# Map string names to library constants.
# Includes common short aliases (e.g. "EU" -> "europe") so .env files match HA naming.
REGION_MAP = {name.lower(): code for code, name in REGIONS.items()}
REGION_MAP.update(
    {
        "eu": REGION_MAP["europe"],
        "usa": REGION_MAP["usa"],
        "ca": REGION_MAP["canada"],
        "au": REGION_MAP["australia"],
        "nz": REGION_MAP["new zealand"],
        "in": REGION_MAP["india"],
        "cn": REGION_MAP["china"],
        "br": REGION_MAP["brazil"],
    }
)
BRAND_MAP = {name.lower(): code for code, name in BRANDS.items()}


def load_dotenv(path: str) -> None:
    """Load KEY=value pairs from a .env file into the process environment."""
    dotenv_path = Path(path).expanduser()
    if not dotenv_path.is_file():
        return
    try:
        from dotenv import load_dotenv as _load_dotenv

        _load_dotenv(dotenv_path)
    except ImportError:
        # Fallback for environments without python-dotenv installed.
        with open(dotenv_path, encoding="utf-8") as f:
            for raw in f:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip().strip("\"'")
                if key and key not in os.environ:
                    os.environ[key] = value


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Dump vehicle diagnostics for any region/brand."
    )
    parser.add_argument(
        "--region",
        default=os.getenv("CC_REGION"),
        choices=sorted(REGION_MAP),
        help="API region (or CC_REGION env var)",
    )
    parser.add_argument(
        "--brand",
        default=os.getenv("CC_BRAND"),
        choices=sorted(BRAND_MAP),
        help="Brand (or CC_BRAND env var)",
    )
    parser.add_argument("--username", default=os.getenv("CC_USERNAME"))
    parser.add_argument("--password", default=os.getenv("CC_PASSWORD"))
    parser.add_argument("--pin", default=os.getenv("CC_PIN", ""))
    parser.add_argument("--token-file", help="Path to cache the access token")
    parser.add_argument(
        "--output-dir",
        default=None,
        help="Override output directory (default: diagnostics/ next to script)",
    )
    parser.add_argument("--env-file", help="Path to .env file")
    args = parser.parse_args()

    env_file = args.env_file or ".env"
    load_dotenv(env_file)

    # .env is the lowest-precedence source; fill anything still missing.
    if not args.region:
        args.region = os.getenv("CC_REGION")
    if not args.brand:
        args.brand = os.getenv("CC_BRAND")
    if not args.username:
        args.username = os.getenv("CC_USERNAME")
    if not args.password:
        args.password = os.getenv("CC_PASSWORD")
    if not args.pin:
        args.pin = os.getenv("CC_PIN", "")

    if not args.region:
        parser.error("--region is required (or CC_REGION env var)")
    if not args.brand:
        parser.error("--brand is required (or CC_BRAND env var)")
    if not args.username or not args.password:
        parser.error(
            "--username and --password are required (or CC_USERNAME/CC_PASSWORD env vars / .env file)"
        )

    # Normalize to lower case so "EU" in .env maps to the "eu" alias.
    args.region = args.region.lower()
    args.brand = args.brand.lower()
    return args


def default_output_dir(args: argparse.Namespace) -> Path:
    if args.output_dir:
        return Path(args.output_dir).expanduser()
    script_dir = Path(__file__).resolve().parent
    ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
    return script_dir / "diagnostics" / f"{ts}_{args.region}_{args.brand}"


def token_path(args: argparse.Namespace) -> str:
    if args.token_file:
        return args.token_file
    out_dir = default_output_dir(args)
    return str(out_dir / f"token_{args.region}_{args.brand}.json")


def load_token(path: str) -> Token | None:
    if not os.path.exists(path):
        return None
    with open(path) as f:
        data = json.load(f)
    return Token.from_dict(data)


def save_token(token: Token, path: str) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(path, "w") as f:
        json.dump(token.to_dict(), f, indent=2)


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str, ensure_ascii=False)


def ask_otp_channel(otp_request) -> OTP_NOTIFY_TYPE:
    print("OTP required. Available channels:")
    if otp_request.has_email:
        print("  [E] email")
    if otp_request.has_sms:
        print("  [S] SMS")
    choice = input("Select channel: ").strip().upper()
    if choice == "E":
        return OTP_NOTIFY_TYPE.EMAIL
    if choice == "S":
        return OTP_NOTIFY_TYPE.SMS
    raise ValueError("Invalid OTP channel selected")


def login(manager: VehicleManager) -> None:
    """Login, prompting for OTP if the region requires it."""
    result = manager.login()
    if result is True:
        return

    channel = ask_otp_channel(result)
    manager.send_otp(channel)
    otp_code = input("Enter OTP code: ").strip()
    manager.verify_otp_and_complete_login(otp_code)


def collect_api_capabilities(api) -> dict:
    """Collect capability flags and metadata from the API implementation."""
    capabilities = {
        "api_class": type(api).__name__,
        "supports_window_control": getattr(api, "supports_window_control", None),
        "supports_valet_mode": getattr(api, "supports_valet_mode", None),
    }
    for attr in (
        "BASE_URL",
        "BASE_DOMAIN",
        "API_URL",
        "SPA_API_URL",
        "SPA_API_URL_V2",
        "APP_ID",
        "CCSP_SERVICE_ID",
    ):
        if hasattr(api, attr):
            capabilities[attr.lower()] = getattr(api, attr)
    return capabilities


def install_http_recorder(out_dir: Path) -> None:
    """Monkey-patch requests.Session.request so every HTTP call is saved to disk.

    This is more universal than patching api.session because some regions build
    headers inline or use separate session objects.
    """
    original_request = requests.Session.request
    counter = 0

    def _recorded_request(self, method, url, **kwargs):
        nonlocal counter
        counter += 1
        try:
            response = original_request(self, method, url, **kwargs)
            try:
                body = response.json()
            except Exception:
                body = {"_raw_text": response.text}
            record = {
                "index": counter,
                "method": method,
                "url": url,
                "request_headers": dict(kwargs.get("headers", {})),
                "status_code": response.status_code,
                "response_headers": dict(response.headers),
                "body": body,
            }
        except Exception as exc:
            record = {
                "index": counter,
                "method": method,
                "url": url,
                "request_headers": dict(kwargs.get("headers", {})),
                "error": str(exc),
            }

        write_json(out_dir / f"raw_request_{counter:04d}_{method.lower()}.json", record)
        return response

    requests.Session.request = _recorded_request


def fetch_api_endpoint(url: str, headers: dict, label: str, timeout: int = 30) -> dict:
    """Generic GET fetch with structured diagnostic output."""
    resp = None
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        body: Any
        try:
            body = resp.json()
        except Exception:
            body = {"_raw_text": resp.text if resp else ""}
        return {
            "url": url,
            "status_code": resp.status_code,
            "body": body,
        }
    except Exception as exc:
        return {
            "url": url,
            "status_code": getattr(resp, "status_code", None),
            "error": f"{label}: {exc}",
        }


def type1_extra_endpoints(api: Any, token: Token, vehicle: Vehicle) -> dict:
    """Extra read-only endpoints available in ApiImplType1 regions (EU, AU, IN, CN)."""
    from hyundai_kia_connect_api.ApiImplType1 import ApiImplType1

    if not isinstance(api, ApiImplType1):
        return {}

    ccs2 = bool(vehicle.ccu_ccs2_protocol_support)
    endpoints = {}

    endpoints["location_park"] = (
        api.SPA_API_URL + "vehicles/" + vehicle.id + "/location/park",
        api._get_authenticated_headers(token),
    )
    endpoints["location"] = (
        api.SPA_API_URL + "vehicles/" + vehicle.id + "/location",
        api._get_authenticated_headers(token, ccs2),
    )
    endpoints["drivinginfo"] = (
        api.SPA_API_URL + "vehicles/" + vehicle.id + "/drivinginfo",
        api._get_authenticated_headers(token, ccs2),
    )
    endpoints["profile"] = (
        api.SPA_API_URL + "vehicles/" + vehicle.id + "/profile",
        api._get_authenticated_headers(token),
    )

    return endpoints


def dump_type1_extras(api: Any, token: Token, vehicle: Vehicle) -> dict:
    """Fetch extra Type1 endpoints and return labelled results."""
    endpoints = type1_extra_endpoints(api, token, vehicle)
    results = {}
    for label, (url, headers) in endpoints.items():
        results[label] = fetch_api_endpoint(url, headers, label)
    return results


def full_vehicle_dict(vehicle: Vehicle) -> dict:
    """Return every field on Vehicle, including None values."""
    return asdict(vehicle)


def main() -> None:
    args = parse_args()

    region_code = REGION_MAP[args.region]
    brand_code = BRAND_MAP[args.brand]
    out_dir = default_output_dir(args)
    tpath = token_path(args)

    manager = VehicleManager(
        region=region_code,
        brand=brand_code,
        username=args.username,
        password=args.password,
        pin=args.pin,
        token=load_token(tpath),
    )

    api = manager.api
    out_dir.mkdir(parents=True, exist_ok=True)

    # Record every HTTP call the library makes from now on.
    install_http_recorder(out_dir)

    login(manager)
    save_token(manager.token, tpath)

    manager.check_and_refresh_token()
    save_token(manager.token, tpath)

    # API capability / metadata snapshot
    write_json(out_dir / "api_capabilities.json", collect_api_capabilities(api))

    # Update each vehicle with cached state, then save full dump
    for vehicle in manager.vehicles.values():
        try:
            api.update_vehicle_with_cached_state(manager.token, vehicle)
        except Exception as exc:
            print(f"  Warning: cached state update failed for {vehicle.id}: {exc}")

        write_json(
            out_dir / f"vehicle_{vehicle.id}.json",
            full_vehicle_dict(vehicle),
        )

        from hyundai_kia_connect_api.ApiImplType1 import ApiImplType1

        if isinstance(api, ApiImplType1):
            extras = dump_type1_extras(api, manager.token, vehicle)
            for label, result in extras.items():
                write_json(
                    out_dir / f"raw_vehicle_{vehicle.id}_{label}.json",
                    result,
                )

    print(f"Diagnostics written to {out_dir}")
    print(f"Token cached in {tpath}")


if __name__ == "__main__":
    main()
