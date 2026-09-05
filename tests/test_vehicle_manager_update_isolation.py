"""Per-vehicle error isolation in the aggregate update loops (#1280).

One unreachable vehicle (resCode 5031) used to abort the whole update, taking
every other vehicle of the account down with it in Home Assistant. The
aggregate loops now isolate vehicle-level API errors; authentication and rate
limiting errors still propagate (they are account-level and must reach the
caller), as does a direct call on a single vehicle.
"""

from unittest.mock import MagicMock

import pytest

from hyundai_kia_connect_api.exceptions import (
    APIError,
    AuthenticationError,
    RateLimitingError,
)
from hyundai_kia_connect_api.Token import Token
from hyundai_kia_connect_api.Vehicle import Vehicle
from hyundai_kia_connect_api.VehicleManager import VehicleManager


def _manager(raise_for, error):
    """A two-vehicle manager whose api raises `error` for vehicle-1."""
    manager = VehicleManager.__new__(VehicleManager)
    manager.api = MagicMock()
    manager.token = Token(access_token="x", refresh_token="y", device_id="z")
    manager.geocode_api_enable = False
    manager.vehicles = {}
    for vehicle_id in ("vehicle-1", "vehicle-2"):
        vehicle = Vehicle()
        vehicle.id = vehicle_id
        vehicle.enabled = True
        manager.vehicles[vehicle_id] = vehicle
    manager.api.update_vehicle_with_cached_state.side_effect = _raise_or_pass(
        raise_for, error
    )
    manager.api.force_refresh_vehicle_state.side_effect = _raise_or_pass(
        raise_for, error
    )
    return manager


def _raise_or_pass(raise_for, error):
    def behavior(token, vehicle, *args, **kwargs):
        if vehicle.id in raise_for:
            raise error

    return behavior


def test_cached_update_isolates_one_failing_vehicle():
    manager = _manager(raise_for=("vehicle-1",), error=APIError("5031 unreachable"))
    manager.update_all_vehicles_with_cached_state()

    manager.api.update_vehicle_with_cached_state.assert_any_call(
        manager.token, manager.vehicles["vehicle-2"]
    )
    assert manager.vehicles["vehicle-2"].last_scanned_at is not None


def test_check_and_force_update_isolates_one_failing_vehicle():
    manager = _manager(raise_for=("vehicle-1",), error=APIError("5031 unreachable"))
    manager.check_and_force_update_vehicles(3600)

    manager.api.update_vehicle_with_cached_state.assert_any_call(
        manager.token, manager.vehicles["vehicle-2"]
    )


def test_force_refresh_isolates_one_failing_vehicle():
    manager = _manager(raise_for=("vehicle-1",), error=APIError("5031 unreachable"))
    manager.force_refresh_all_vehicles_states()

    manager.api.force_refresh_vehicle_state.assert_any_call(
        manager.token, manager.vehicles["vehicle-2"]
    )


@pytest.mark.parametrize(
    "error", [AuthenticationError("token expired"), RateLimitingError("too fast")]
)
def test_account_level_errors_propagate(error):
    manager = _manager(raise_for=("vehicle-1",), error=error)
    with pytest.raises(type(error)):
        manager.update_all_vehicles_with_cached_state()


def test_single_vehicle_update_still_raises():
    manager = _manager(raise_for=("vehicle-1",), error=APIError("5031 unreachable"))
    with pytest.raises(APIError):
        manager.update_vehicle_with_cached_state("vehicle-1")


def test_isolation_logs_a_warning(caplog):
    manager = _manager(raise_for=("vehicle-1",), error=APIError("5031 unreachable"))
    manager.update_all_vehicles_with_cached_state()

    assert any(
        "vehicle-1" in record.message and "5031" in record.message
        for record in caplog.records
    )
