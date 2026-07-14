"""Redaction in diagnostic_dump.py: credentials and GPS must be masked by default.

The dump tool writes raw HTTP records and parsed vehicles to a shareable
folder. redact_sensitive() is the guard that stops access tokens (Authorization
/ Set-Cookie / Stamp headers, token body keys) and GPS coordinates from
leaking. These tests pin that behaviour so a future edit can't silently
re-enable the leak.
"""

import importlib.util
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "diagnostic_dump.py"


@pytest.fixture(scope="module")
def dd():
    spec = importlib.util.spec_from_file_location("diagnostic_dump", _SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_redact_authorization_and_set_cookie(dd):
    record = {
        "request_headers": {
            "Authorization": "Bearer eyJabc.def.ghi",
            "Content-Type": "application/json",
        },
        "response_headers": {"Set-Cookie": "JSESSIONID=xyz; HttpOnly"},
        "body": {"ok": True},
    }
    out = dd.redact_sensitive(record)
    assert out["request_headers"]["Authorization"] == "<REDACTED>"
    assert out["request_headers"]["Content-Type"] == "application/json"
    assert out["response_headers"]["Set-Cookie"] == "<REDACTED>"
    assert out["body"] == {"ok": True}


def test_redact_stamp_header_case_insensitive(dd):
    record = {"request_headers": {"CCSP-Stamp": "secret-stamp", "Cookie": "a=b"}}
    out = dd.redact_sensitive(record)
    assert out["request_headers"]["CCSP-Stamp"] == "<REDACTED>"
    assert out["request_headers"]["Cookie"] == "<REDACTED>"


def test_redact_token_keys_in_body(dd):
    body = {
        "access_token": "tok",
        "refresh_token": "ref",
        "device_id": "dev",
        "sid": "s1",
        "rmtoken": "rm",
        "sessionId": "ss",
    }
    out = dd.redact_sensitive(body)
    for key in (
        "access_token",
        "refresh_token",
        "device_id",
        "sid",
        "rmtoken",
        "sessionId",
    ):
        assert out[key] == "<REDACTED>"


def test_redact_gps_coord_wholesale(dd):
    body = {
        "resMsg": {
            "state": {"Vehicle": {"Date": "20260701120000"}},
            "coord": {"lat": 52.2297, "lon": 21.0122, "time": "20260701120000"},
        }
    }
    out = dd.redact_sensitive(body)
    assert out["resMsg"]["coord"] == "<REDACTED>"
    assert out["resMsg"]["state"]["Vehicle"]["Date"] == "20260701120000"


def test_redact_gps_top_level_keys(dd):
    body = {
        "gpsLatitude": 52.23,
        "gpsLongitude": 21.01,
        "_location_latitude": 1.0,
        "_location_longitude": 2.0,
    }
    out = dd.redact_sensitive(body)
    assert out["gpsLatitude"] == "<REDACTED>"
    assert out["gpsLongitude"] == "<REDACTED>"
    assert out["_location_latitude"] == "<REDACTED>"
    assert out["_location_longitude"] == "<REDACTED>"


def test_redact_preserves_tire_pressure(dd):
    body = {
        "Chassis": {
            "Axle": {
                "Row1": {"Left": {"Tire": {"Pressure": 27, "PressureLow": 0}}},
                "Tire": {"PressureUnit": 2, "PressureLow": 0},
            }
        }
    }
    out = dd.redact_sensitive(body)
    assert out["Chassis"]["Axle"]["Row1"]["Left"]["Tire"]["Pressure"] == 27
    assert out["Chassis"]["Axle"]["Tire"]["PressureUnit"] == 2


def test_redact_recurses_lists(dd):
    body = [{"lat": 1.0, "name": "a"}, {"Authorization": "Bearer x"}]
    out = dd.redact_sensitive(body)
    assert out[0]["lat"] == "<REDACTED>"
    assert out[0]["name"] == "a"
    assert out[1]["Authorization"] == "<REDACTED>"


def test_redact_is_pure_does_not_mutate_input(dd):
    body = {"Authorization": "Bearer x", "coord": {"lat": 1.0}}
    dd.redact_sensitive(body)
    assert body["Authorization"] == "Bearer x"
    assert body["coord"] == {"lat": 1.0}
