"""HyundaiCciApiEU.py — Hyundai EU CCI/GSPA API.

Hyundai-specific EU implementation inheriting the OneApp (CCI) login flow,
the GSPA secure-request layer, and the shared GSPA remote-control layer
from ``GspaApiEU``. This module keeps Hyundai brand constants and
vehicle-read parsers (stored-status, driving info/history, breakdowns,
CCS2 status). OTA and MQTT are handled in later PRs.
"""

# pylint:disable=missing-class-docstring,missing-function-docstring,invalid-name,logging-fstring-interpolation,broad-except,too-many-lines

import datetime as dt
import logging
import uuid
from typing import Any
from urllib.parse import quote

import requests

from .const import (
    BRAND_GENESIS,
    BRAND_HYUNDAI,
    BRAND_KIA,
    BRANDS,
    DISTANCE_UNITS,
    DOMAIN,
    ENGINE_TYPES,
    PRESSURE_SCALES,
    SEAT_STATUS,
    TEMPERATURE_UNITS,
    PressureUnit,
)
from .exceptions import APIError, AuthenticationError
from .GspaApiEU import USER_AGENT_OK_HTTP, GspaApiEU
from .mqtt_service_hub import MqttServiceHubMixin
from .Token import Token
from .utils import (
    bool_or_none,
    get_child_value,
    normalize_battery_soc,
    parse_datetime,
    pressure_or_none,
)
from .Vehicle import DailyDrivingStats, Vehicle

_LOGGER = logging.getLogger(__name__)


class HyundaiCciApiEU(GspaApiEU, MqttServiceHubMixin):
    """Hyundai EU CCI/GSPA API.

    Uses the CCI login flow (OneApp client_id 4f4953b5) confirmed on
    production endpoints. Login, token lifecycle, the GSPA
    secure-request layer, and the GSPA remote-control layer are all
    inherited from ``GspaApiEU``. This subclass carries the brand
    constants and the Hyundai-specific read layer: CCS2 vehicle-property
    parsing, driving info/history, and breakdowns. MQTT Service Hub
    registration and the receive-only MQTT client are provided by
    ``MqttServiceHubMixin`` and ``mqtt_client.py``.
    """

    # Brand constants (Hyundai OneApp EU, confirmed on production endpoints).
    ONEAPP_CLIENT_ID = "4f4953b5-02e1-4dbc-8599-87e983ee1be5"
    ONEAPP_REDIRECT_URI = "https://oneapp.hyundai.com/redirect"
    CCI_API_URL = "https://cci-api-eu.hyundai.com"
    CCI_PACKAGE_ID = "com.hyundai.oneapp.eu"
    GSPA_BASE_URL = "https://gspa-ccs-eu.hyundai.com/"
    LOGIN_FORM_HOST = "https://idpconnect-eu.hyundai.com"
    CIPHER_BRAND = "hyundai"
    REQUEST_ID_HEADER = "X-Request-Id"
    DEVICE_ID_HEADER = "X-Device-Id"
    CCAPI_BASE_URL = "prd.eu-ccapi.hyundai.com:8080"

    # SVM reads confirmed live on the EU GSPA endpoints (na-images);
    # KiaCciApiEU keeps the inherited False until verified there too.
    supports_svm: bool = True

    # ccspServiceId — the service identifier in the X-Service-Id header
    # (Service Hub). Distinct from CCSP_SERVICE_ID (OAuth client_id).
    CCSP_CLIENT_SERVICE_ID = "4f4953b5-02e1-4dbc-8599-87e983ee1be5"
    # pushProviderId — X-Application-Id header value.
    PUSH_PROVIDER_ID = "33258e5f-b8c8-459e-add1-1e94217ba148"

    # Service Hub staging flag (class attr — production endpoints only
    # for now; staging bases live in mqtt_client.SERVICE_HUB_STAGING_BASES).
    staging: bool = False

    # CCS2 EU vehicles support GSPA window control.
    supports_window_control: bool = True

    # Hyundai EU CCI remote control is live-verified.
    GSPA_REMOTE_CONTROL_VERIFIED = True

    # ------------------------------------------------------------------
    # MQTT Service Hub (api/v3/servicehub/*)
    # ------------------------------------------------------------------

    @property
    def _service_hub_brand(self) -> str:
        """Short brand code for Service Hub API (H, K, or G).

        Service Hub endpoints use single-letter brand codes in the body,
        not the full brand names used in HTTP headers.
        """
        if BRANDS[self.brand] == BRAND_KIA:
            return "K"
        if BRANDS[self.brand] == BRAND_GENESIS:
            return "G"
        return "H"

    def _get_service_hub_headers(self, token: Token) -> dict:
        """Headers for Service Hub (api/v3/servicehub/*).

        Service Hub uses the CCS SDK interceptor chain, NOT the CCI API
        headers. Based on the CCS interceptor chain (h.java, j.java,
        d.java): Authorization carries the CCS token, plus client-os-code,
        locale, Accept-Language, X-Application-Id (pushProviderId = FCM),
        EpitVersion, X-Service-Id (ccspServiceId) and a per-request UUID
        in X-Request-Id. exchangeable-token / non-ccs-token are attached
        when the token carries them.
        """
        ccs_access = token.ccs_token or token.access_token or ""
        ccs_access = ccs_access.removeprefix("Bearer ").strip()
        headers = {
            "Authorization": f"Bearer {ccs_access}",
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT_OK_HTTP,
            # CciAuthenticationHeaderInterceptor (h.java)
            "client-os-code": "AOS",
            "locale": self.LANGUAGE,
            # DefaultHeaderInterceptor (j.java)
            "Accept-Language": self.LANGUAGE,
            "X-Application-Id": self.PUSH_PROVIDER_ID,
            "EpitVersion": "EPITV2",
            "X-Service-Id": self.CCSP_CLIENT_SERVICE_ID,
            # X-Request-Id: per-request UUID (TSID)
            "X-Request-Id": str(uuid.uuid4()),
        }
        # CciAuthenticationHeaderInterceptor — exchangeable-token
        exchangeable = getattr(token, "exchangeable_token", None) or ""
        if exchangeable:
            headers["exchangeable-token"] = exchangeable
        # CciAuthenticationHeaderInterceptor — non-ccs-token (optional)
        non_ccs = getattr(token, "non_ccs_token", None) or ""
        if non_ccs:
            headers["non-ccs-token"] = non_ccs
        return headers

    @staticmethod
    def _build_service_hub_url(base_url: str, params: dict) -> str:
        """Build URL with query params matching OkHttp 3.12.0 encoding.

        OkHttp does NOT encode '@' in query parameter values, but Python's
        urllib3 (used by requests) encodes '@' as '%40'. The Service Hub
        server validates the tid session exactly as sent, so '%40' vs '@'
        causes "Invalid Parameter (client-id)" on device/protocol.

        This method uses quote() with safe='@' to match OkHttp's behavior.
        """
        if not params:
            return base_url
        # OkHttp 3.12.0 QUERY_COMPONENT encode set does NOT include @.
        # We add a generous safe set to match: unreserved chars + @ + sub-delims
        safe_chars = "-_.~!$'()*,;=:@/?"
        parts = []
        for k, v in params.items():
            encoded_key = quote(str(k), safe=safe_chars)
            encoded_val = quote(str(v), safe=safe_chars)
            parts.append(f"{encoded_key}={encoded_val}")
        return f"{base_url}?{'&'.join(parts)}"

    def _get_service_hub_url(self) -> str:
        """Get Service Hub base URL for the current brand/region.

        Production URLs from the official EU app (v1.1.4).
        Staging URLs are in plaintext config.
        """
        # Map brand + region to Service Hub key
        # Key format: {H|K|G}_{region_code}
        brand_prefix = {
            BRAND_HYUNDAI: "H",
            BRAND_KIA: "K",
            BRAND_GENESIS: "G",
        }.get(self.brand, "H")

        # Region mapping (EU is default for this API class)
        # The CCI EU API only supports EU region, but we provide
        # the full mapping for future region subclasses
        region_suffix = "EU"  # HyundaiCciApiEU is EU-only

        service_hub_key = f"{brand_prefix}_{region_suffix}"

        from .mqtt_client import SERVICE_HUB_PRODUCTION_BASES, SERVICE_HUB_STAGING_BASES

        if self.staging:
            bases = SERVICE_HUB_STAGING_BASES
        else:
            bases = SERVICE_HUB_PRODUCTION_BASES

        host_port = bases.get(service_hub_key)
        if not host_port:
            _LOGGER.error(f"{DOMAIN} - No Service Hub URL for key={service_hub_key}")
            return ""

        return f"https://{host_port}"

    def get_mqtt_connection_state(self, token: Token) -> str | None:
        """GET api/v3/vstatus/connstate — check MQTT connection state.

        The app sends: @Query("clientId") — no tid or client-id headers.
        Returns: "ONLINE", "OFFLINE", or "UNKNOWN".
        """
        url = self._get_service_hub_url()
        if not url:
            return None
        url += "/api/v3/vstatus/connstate"
        headers = self._get_service_hub_headers(token)

        params = {
            "clientId": token.mqtt_client_id or token.client_device_id or "",
        }
        try:
            full_url = self._build_service_hub_url(url, params)
            response = requests.get(full_url, headers=headers, timeout=(5, 30))
            if response.status_code == 200:
                data = response.json()
                state = (
                    data.get("connState")
                    or data.get("state")
                    or data.get("status")
                    or "UNKNOWN"
                )
                _LOGGER.debug(f"{DOMAIN} - MQTT connection state: {state}")
                return state.upper()
            _LOGGER.warning(
                f"{DOMAIN} - Service Hub connstate failed: "
                f"HTTP {response.status_code} — {response.text[:300]}"
            )
        except Exception as ex:
            _LOGGER.error(f"{DOMAIN} - Service Hub connstate error: {ex}")
        return None

    # ------------------------------------------------------------------
    # Driving info + history (GSPA, read-only)
    # ------------------------------------------------------------------

    def _get_driving_info(
        self, token: Token, vehicle: Vehicle
    ) -> dict[str, Any] | None:
        """Fetch driving info from GSPA driving-info endpoint."""
        self._validate_ccs_token(token)
        try:
            return self._gspa_get(token, vehicle, "driving-info/vehicles/{carId}")
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA driving-info failed")
            return None

    def _update_vehicle_drive_info(
        self, vehicle: Vehicle, state: dict[str, Any]
    ) -> None:
        if isinstance(state, dict):
            driving_info = state.get("drivingInfo", state)
            if driving_info is None:
                return
            if isinstance(driving_info, list) and len(driving_info) > 0:
                driving_info = driving_info[0]
            vehicle.total_driving_range = (
                driving_info.get("totalDistance"),
                DISTANCE_UNITS.get(1, "km"),
            )
            total_consumed = driving_info.get("totalPwrCsp")
            if total_consumed is not None:
                vehicle.total_power_consumed = float(total_consumed)
            total_regen = driving_info.get("regenPwr")
            if total_regen is not None:
                vehicle.total_power_regenerated = float(total_regen)

    def _get_driving_history(
        self, token: Token, vehicle: Vehicle
    ) -> dict[str, Any] | None:
        """Fetch 30-day driving history from GSPA driving-history endpoint."""
        self._validate_ccs_token(token)
        try:
            return self._gspa_get(token, vehicle, "driving-history/vehicles/{carId}")
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA driving-history failed")
            return None

    def _update_vehicle_driving_history(
        self, vehicle: Vehicle, state: dict[str, Any]
    ) -> None:
        """Parse 30-day driving history into power_consumption_30d and daily_stats."""
        # Filter for the summary period (drivingPeriod == 0) which contains
        # total power consumption and calculative odometer.
        driving_info_list = state.get("drivingInfo", [])
        if not driving_info_list:
            return

        for item in driving_info_list:
            if not isinstance(item, dict):
                continue
            if item.get("drivingPeriod") != 0:
                continue
            total_pwr = item.get("totalPwrCsp")
            odo = next(
                (v for k, v in item.items() if k.lower() == "calculativeodo"),
                0,
            )
            if total_pwr and odo and odo > 0:
                vehicle.power_consumption_30d = round(total_pwr / odo)
                break

        detail_list = state.get("drivingInfoDetail", [])
        if detail_list:
            daily_stats = []
            for day in detail_list:
                if not isinstance(day, dict):
                    continue
                try:
                    processed = DailyDrivingStats(
                        date=dt.datetime.strptime(day["drivingDate"], "%Y%m%d").replace(
                            tzinfo=self.data_timezone
                        ),
                        total_consumed=get_child_value(day, "totalPwrCsp"),
                        engine_consumption=get_child_value(day, "motorPwrCsp"),
                        climate_consumption=get_child_value(day, "climatePwrCsp"),
                        onboard_electronics_consumption=get_child_value(
                            day, "eDPwrCsp"
                        ),
                        battery_care_consumption=get_child_value(
                            day, "batteryMgPwrCsp"
                        ),
                        regenerated_energy=get_child_value(day, "regenPwr"),
                        distance=get_child_value(day, "calculativeOdo"),
                        distance_unit=vehicle.odometer_unit,
                    )
                    daily_stats.append(processed)
                except (KeyError, ValueError):
                    continue
            if daily_stats:
                vehicle.daily_stats = daily_stats

    # ------------------------------------------------------------------
    # DTC breakdowns (GSPA, read-only)
    # ------------------------------------------------------------------

    def get_breakdowns(self, token: Token, vehicle: Vehicle) -> dict[str, Any] | None:
        """Get vehicle diagnostic trouble codes (DTCs) from GSPA."""
        self._validate_ccs_token(token)
        try:
            return self._gspa_get(
                token, vehicle, "diagnostics/vehicles/{carId}/breakdowns"
            )
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA breakdowns failed")
            return None

    def _parse_breakdowns(self, vehicle: Vehicle, data: dict[str, Any]) -> None:
        """Parse DTC data from GSPA breakdown response.

        Response structure:
          {"breakdown": [{"ecuName": "...", "ecuIdx": 0, "dtcList": [...]}]}
        """
        breakdown = data.get("breakdown", [])
        if not breakdown:
            return
        vehicle.dtc_count = len(breakdown)
        descriptions = {}
        for item in breakdown:
            if not isinstance(item, dict):
                continue
            ecu_name = item.get("ecuName", item.get("ecuIdx", "unknown"))
            dtc_list = item.get("dtcList", [])
            if dtc_list:
                descriptions[str(ecu_name)] = dtc_list
        if descriptions:
            vehicle.dtc_descriptions = descriptions

    @staticmethod
    def _parse_gspa_widget_lamp_data(vehicle: Vehicle, widget: dict[str, Any]) -> None:
        """Parse lamp/signal/ignition/sleep data from GSPA stored-status-widget.

        Live-probed structure (2026-06-15, Santa Fe HEV):
          state.Vehicle.Body.Lights.Front.Left.Low.Warning  (0.0/1.0)
          state.Vehicle.Body.Lights.Front.Left.High.Warning
          state.Vehicle.Body.Lights.Front.Left.TurnSignal.Warning
          state.Vehicle.Body.Lights.Rear.Left.StopLamp.Warning
          state.Vehicle.Electronics.PowerSupply.Ignition3  (0.0/1.0)
          state.Vehicle.RemoteControl.SleepMode  (0.0/1.0)
          state.Vehicle.Drivetrain.Transmission.ParkingPosition  (0.0/1.0)

        Values are 0.0/1.0 floats. All parsing is defensive.
        """
        # The widget data may be nested under "state.Vehicle" or flat
        state = widget.get("state", widget)
        vehicle_data = state.get("Vehicle", state)

        # Headlamp data from Body.Lights.Front
        lights = vehicle_data.get("Body", {}).get("Lights", {})
        front = lights.get("Front", {})
        front_left = front.get("Left", {})
        front_right = front.get("Right", {})

        if isinstance(front_left, dict):
            low = front_left.get("Low", {})
            if isinstance(low, dict) and "Warning" in low:
                vehicle.headlamp_left_low = bool(low["Warning"])
            high = front_left.get("High", {})
            if isinstance(high, dict) and "Warning" in high:
                vehicle.headlamp_left_high = bool(high["Warning"])
            bifunc = front_left.get("Bifunc", {})
            if isinstance(bifunc, dict) and "Warning" in bifunc:
                vehicle.headlamp_left_bifunc = bool(bifunc["Warning"])
            ts = front_left.get("TurnSignal", {})
            if isinstance(ts, dict) and "Warning" in ts:
                vehicle.turn_signal_left_front = bool(ts["Warning"])

        if isinstance(front_right, dict):
            low = front_right.get("Low", {})
            if isinstance(low, dict) and "Warning" in low:
                vehicle.headlamp_right_low = bool(low["Warning"])
            high = front_right.get("High", {})
            if isinstance(high, dict) and "Warning" in high:
                vehicle.headlamp_right_high = bool(high["Warning"])
            bifunc = front_right.get("Bifunc", {})
            if isinstance(bifunc, dict) and "Warning" in bifunc:
                vehicle.headlamp_right_bifunc = bool(bifunc["Warning"])
            ts = front_right.get("TurnSignal", {})
            if isinstance(ts, dict) and "Warning" in ts:
                vehicle.turn_signal_right_front = bool(ts["Warning"])

        # Headlamp system warning
        head_lamp = front.get("HeadLamp", {})
        if isinstance(head_lamp, dict) and "SystemWarning" in head_lamp:
            vehicle.headlamp_status = head_lamp.get("SystemWarning")

        # Stop lamp data from Body.Lights.Rear
        rear = lights.get("Rear", {})
        rear_left = rear.get("Left", {})
        rear_right = rear.get("Right", {})
        if isinstance(rear_left, dict):
            sl = rear_left.get("StopLamp", {})
            if isinstance(sl, dict) and "Warning" in sl:
                vehicle.stop_lamp_left = bool(sl["Warning"])
            ts = rear_left.get("TurnSignal", {})
            if isinstance(ts, dict) and "Warning" in ts:
                vehicle.turn_signal_left_rear = bool(ts["Warning"])
        if isinstance(rear_right, dict):
            sl = rear_right.get("StopLamp", {})
            if isinstance(sl, dict) and "Warning" in sl:
                vehicle.stop_lamp_right = bool(sl["Warning"])
            ts = rear_right.get("TurnSignal", {})
            if isinstance(ts, dict) and "Warning" in ts:
                vehicle.turn_signal_right_rear = bool(ts["Warning"])

        # Ignition state from Electronics.PowerSupply
        power_supply = vehicle_data.get("Electronics", {}).get("PowerSupply", {})
        if isinstance(power_supply, dict):
            ign3 = power_supply.get("Ignition3")
            if ign3 is not None:
                vehicle.ign3 = bool(ign3)

        # Remote ignition (top-level key in GSPA widget)
        remote_ign = vehicle_data.get("remoteIgnition")
        if remote_ign is not None:
            vehicle.remote_ignition = bool(remote_ign)

        # Sleep mode from RemoteControl
        remote_control = vehicle_data.get("RemoteControl", {})
        if isinstance(remote_control, dict):
            sleep = remote_control.get("SleepMode")
            if sleep is not None:
                vehicle.sleep_mode_check = bool(sleep)

        # Transmission from Drivetrain
        drivetrain = vehicle_data.get("Drivetrain", {})
        if isinstance(drivetrain, dict):
            transmission = drivetrain.get("Transmission", {})
            if isinstance(transmission, dict):
                parking = transmission.get("ParkingPosition")
                if parking is not None:
                    vehicle.transmission_condition = parking

    # ------------------------------------------------------------------
    # CCS2 vehicle property mapping
    # ------------------------------------------------------------------

    def _update_vehicle_properties_ccs2(
        self, vehicle: Vehicle, state: dict[str, Any]
    ) -> None:
        if get_child_value(state, "Offset"):
            offset = float(get_child_value(state, "Offset"))
            hours = int(offset)
            minutes = int((offset - hours) * 60)
            vehicle.timezone = dt.timezone(dt.timedelta(hours=hours, minutes=minutes))
        if get_child_value(state, "Date"):
            vehicle.last_updated_at = parse_datetime(
                get_child_value(state, "Date"), vehicle.timezone
            )
        else:
            vehicle.last_updated_at = dt.datetime.now(self.data_timezone)

        vehicle.odometer = (
            get_child_value(state, "Drivetrain.Odometer"),
            DISTANCE_UNITS[1],
        )
        vehicle.car_battery_percentage = normalize_battery_soc(
            get_child_value(state, "Electronics.Battery.Level")
        )
        vehicle.engine_is_running = get_child_value(state, "DrivingReady")

        air_temp = get_child_value(state, "Cabin.HVAC.Row1.Driver.Temperature.Value")
        if air_temp is not None and air_temp != "OFF":
            air_temp_unit = get_child_value(
                state, "Cabin.HVAC.Row1.Driver.Temperature.Unit"
            )
            vehicle.air_temperature = (
                air_temp,
                TEMPERATURE_UNITS.get(air_temp_unit, TEMPERATURE_UNITS[0]),
            )

        outside_temp = get_child_value(state, "Cabin.HVAC.OutsideTemperature.Value")
        outside_temp_unit = get_child_value(state, "Cabin.HVAC.OutsideTemperature.Unit")
        vehicle.outside_temperature = (
            outside_temp,
            TEMPERATURE_UNITS[outside_temp_unit],
        )

        defrost_is_on = get_child_value(state, "Body.Windshield.Front.Defog.State")
        if defrost_is_on in [0, 2]:
            vehicle.defrost_is_on = False
        elif defrost_is_on == 1:
            vehicle.defrost_is_on = True

        steer_wheel_heat = get_child_value(state, "Cabin.SteeringWheel.Heat.State")
        if steer_wheel_heat in [0, 2]:
            vehicle.steering_wheel_heater_is_on = False
        elif steer_wheel_heat == 1:
            vehicle.steering_wheel_heater_is_on = True

        defrost_rear_is_on = get_child_value(state, "Body.Windshield.Rear.Defog.State")
        if defrost_rear_is_on in [0, 2]:
            vehicle.back_window_heater_is_on = False
        elif defrost_rear_is_on == 1:
            vehicle.back_window_heater_is_on = True

        vehicle.front_left_seat_status = SEAT_STATUS[
            get_child_value(state, "Cabin.Seat.Row1.Driver.Climate.State")
        ]
        vehicle.front_right_seat_status = SEAT_STATUS[
            get_child_value(state, "Cabin.Seat.Row1.Passenger.Climate.State")
        ]
        vehicle.rear_left_seat_status = SEAT_STATUS[
            get_child_value(state, "Cabin.Seat.Row2.Left.Climate.State")
        ]
        vehicle.rear_right_seat_status = SEAT_STATUS[
            get_child_value(state, "Cabin.Seat.Row2.Right.Climate.State")
        ]

        vehicle.front_left_door_is_open = get_child_value(
            state, "Cabin.Door.Row1.Driver.Open"
        )
        vehicle.front_right_door_is_open = get_child_value(
            state, "Cabin.Door.Row1.Passenger.Open"
        )
        vehicle.back_left_door_is_open = get_child_value(
            state, "Cabin.Door.Row2.Left.Open"
        )
        vehicle.back_right_door_is_open = get_child_value(
            state, "Cabin.Door.Row2.Right.Open"
        )

        vehicle.front_left_door_is_locked = (
            not bool(get_child_value(state, "Cabin.Door.Row1.Driver.Lock"))
            if get_child_value(state, "Cabin.Door.Row1.Driver.Lock") is not None
            else None
        )
        vehicle.front_right_door_is_locked = (
            not bool(get_child_value(state, "Cabin.Door.Row1.Passenger.Lock"))
            if get_child_value(state, "Cabin.Door.Row1.Passenger.Lock") is not None
            else None
        )
        vehicle.back_left_door_is_locked = (
            not bool(get_child_value(state, "Cabin.Door.Row2.Left.Lock"))
            if get_child_value(state, "Cabin.Door.Row2.Left.Lock") is not None
            else None
        )
        vehicle.back_right_door_is_locked = (
            not bool(get_child_value(state, "Cabin.Door.Row2.Right.Lock"))
            if get_child_value(state, "Cabin.Door.Row2.Right.Lock") is not None
            else None
        )

        vehicle.is_locked = (
            vehicle.front_left_door_is_locked
            and vehicle.front_right_door_is_locked
            and vehicle.back_left_door_is_locked
            and vehicle.back_right_door_is_locked
        )

        vehicle.hood_is_open = get_child_value(state, "Body.Hood.Open")
        _open = get_child_value(state, "Cabin.Window.Row1.Driver.Open")
        _level = get_child_value(state, "Cabin.Window.Row1.Driver.OpenLevel")
        vehicle.front_left_window_is_open = bool(_open) if _open is not None else None
        if _level and _level > 0 and not _open:
            vehicle.front_left_window_is_open = True  # vented
        _open = get_child_value(state, "Cabin.Window.Row1.Passenger.Open")
        _level = get_child_value(state, "Cabin.Window.Row1.Passenger.OpenLevel")
        vehicle.front_right_window_is_open = bool(_open) if _open is not None else None
        if _level and _level > 0 and not _open:
            vehicle.front_right_window_is_open = True  # vented
        _open = get_child_value(state, "Cabin.Window.Row2.Left.Open")
        _level = get_child_value(state, "Cabin.Window.Row2.Left.OpenLevel")
        vehicle.back_left_window_is_open = bool(_open) if _open is not None else None
        if _level and _level > 0 and not _open:
            vehicle.back_left_window_is_open = True  # vented
        _open = get_child_value(state, "Cabin.Window.Row2.Right.Open")
        _level = get_child_value(state, "Cabin.Window.Row2.Right.OpenLevel")
        vehicle.back_right_window_is_open = bool(_open) if _open is not None else None
        if _level and _level > 0 and not _open:
            vehicle.back_right_window_is_open = True  # vented
        vehicle.sunroof_is_open = (
            bool(get_child_value(state, "Body.Sunroof.Glass.Open"))
            if get_child_value(state, "Body.Sunroof.Glass.Open") is not None
            else None
        )
        vehicle.tire_pressure_rear_left_warning_is_on = bool(
            get_child_value(state, "Chassis.Axle.Row2.Left.Tire.PressureLow")
        )
        vehicle.tire_pressure_front_left_warning_is_on = bool(
            get_child_value(state, "Chassis.Axle.Row1.Left.Tire.PressureLow")
        )
        vehicle.tire_pressure_front_right_warning_is_on = bool(
            get_child_value(state, "Chassis.Axle.Row1.Right.Tire.PressureLow")
        )
        vehicle.tire_pressure_rear_right_warning_is_on = bool(
            get_child_value(state, "Chassis.Axle.Row2.Right.Tire.PressureLow")
        )
        vehicle.tire_pressure_all_warning_is_on = bool(
            get_child_value(state, "Chassis.Axle.Tire.PressureLow")
        )
        _pu_raw = get_child_value(state, "Chassis.Axle.Tire.PressureUnit")
        if _pu_raw is None:
            vehicle.tire_pressure_unit = None
        else:
            try:
                vehicle.tire_pressure_unit = PressureUnit(_pu_raw)
            except ValueError:
                _LOGGER.warning(
                    "%s - Unknown tire PressureUnit %r; tire pressure values ignored",
                    DOMAIN,
                    _pu_raw,
                )
                vehicle.tire_pressure_unit = None
        _scale = PRESSURE_SCALES.get(vehicle.tire_pressure_unit)
        _pfl = pressure_or_none(
            get_child_value(state, "Chassis.Axle.Row1.Left.Tire.Pressure")
        )
        _pfr = pressure_or_none(
            get_child_value(state, "Chassis.Axle.Row1.Right.Tire.Pressure")
        )
        _prl = pressure_or_none(
            get_child_value(state, "Chassis.Axle.Row2.Left.Tire.Pressure")
        )
        _prr = pressure_or_none(
            get_child_value(state, "Chassis.Axle.Row2.Right.Tire.Pressure")
        )
        vehicle.tire_pressure_front_left = (
            round(_pfl * _scale, 1) if _pfl is not None and _scale is not None else None
        )
        vehicle.tire_pressure_front_right = (
            round(_pfr * _scale, 1) if _pfr is not None and _scale is not None else None
        )
        vehicle.tire_pressure_rear_left = (
            round(_prl * _scale, 1) if _prl is not None and _scale is not None else None
        )
        vehicle.tire_pressure_rear_right = (
            round(_prr * _scale, 1) if _prr is not None and _scale is not None else None
        )
        vehicle.trunk_is_open = get_child_value(state, "Body.Trunk.Open")

        # Headlamp / lamp status
        vehicle.headlamp_left_low = get_child_value(
            state, "Body.Lights.Front.Left.Low.Warning"
        )
        vehicle.headlamp_left_high = get_child_value(
            state, "Body.Lights.Front.Left.High.Warning"
        )
        vehicle.headlamp_left_bifunc = get_child_value(
            state, "Body.Lights.Front.Left.Bifunc.Warning"
        )
        vehicle.headlamp_right_low = get_child_value(
            state, "Body.Lights.Front.Right.Low.Warning"
        )
        vehicle.headlamp_right_high = get_child_value(
            state, "Body.Lights.Front.Right.High.Warning"
        )
        vehicle.headlamp_right_bifunc = get_child_value(
            state, "Body.Lights.Front.Right.Bifunc.Warning"
        )
        vehicle.stop_lamp_left = get_child_value(
            state, "Body.Lights.Rear.Left.StopLamp.Warning"
        )
        vehicle.stop_lamp_right = get_child_value(
            state, "Body.Lights.Rear.Right.StopLamp.Warning"
        )
        vehicle.turn_signal_left_front = get_child_value(
            state, "Body.Lights.Front.Left.TurnSignal.Warning"
        )
        vehicle.turn_signal_right_front = get_child_value(
            state, "Body.Lights.Front.Right.TurnSignal.Warning"
        )
        vehicle.turn_signal_left_rear = get_child_value(
            state, "Body.Lights.Rear.Left.TurnSignal.Warning"
        )
        vehicle.turn_signal_right_rear = get_child_value(
            state, "Body.Lights.Rear.Right.TurnSignal.Warning"
        )

        # Drivetrain / ignition state
        vehicle.transmission_condition = get_child_value(
            state, "Drivetrain.Transmission.ParkingPosition"
        )
        vehicle.ign3 = get_child_value(state, "Electronics.PowerSupply.Ignition3")
        accessory_ign = get_child_value(state, "Electronics.PowerSupply.Ignition1")
        if accessory_ign is not None:
            vehicle.accessory_on = bool(accessory_ign)
        vehicle.remote_ignition = get_child_value(
            state, "Drivetrain.RemoteIgnition.State"
        )
        vehicle.sleep_mode_check = bool_or_none(
            get_child_value(state, "RemoteControl.SleepMode")
        )

        vehicle.ev_battery_percentage = get_child_value(
            state, "Green.BatteryManagement.BatteryRemain.Ratio"
        )
        vehicle.ev_battery_remain = get_child_value(
            state, "Green.BatteryManagement.BatteryRemain.Value"
        )
        vehicle.ev_battery_capacity = get_child_value(
            state, "Green.BatteryManagement.BatteryCapacity.Value"
        )
        vehicle.ev_battery_soh_percentage = get_child_value(
            state, "Green.BatteryManagement.SoH.Ratio"
        )
        vehicle.ev_battery_is_plugged_in = get_child_value(
            state, "Green.ChargingInformation.ConnectorFastening.State"
        )
        charging_door_state = get_child_value(state, "Green.ChargingDoor.State")
        if charging_door_state in [0, 2]:
            vehicle.ev_charge_port_door_is_open = False
        elif charging_door_state == 1:
            vehicle.ev_charge_port_door_is_open = True

        dte_total = get_child_value(state, "Drivetrain.FuelSystem.DTE.Total")
        if dte_total is not None:
            vehicle.total_driving_range = (
                float(dte_total),
                DISTANCE_UNITS[
                    get_child_value(state, "Drivetrain.FuelSystem.DTE.Unit")
                ],
            )
        fuel_dte = get_child_value(state, "Drivetrain.FuelSystem.DTE.Fuel")
        if fuel_dte is not None:
            vehicle.fuel_driving_range = (
                float(fuel_dte),
                vehicle.total_driving_range_unit,
            )
        if vehicle.engine_type == ENGINE_TYPES.EV:
            vehicle.ev_driving_range = (
                vehicle.total_driving_range,
                vehicle.total_driving_range_unit,
            )

        vehicle.ev_estimated_current_charge_duration = (
            get_child_value(state, "Green.ChargingInformation.Charging.RemainTime"),
            "m",
        )
        vehicle.ev_estimated_fast_charge_duration = (
            get_child_value(state, "Green.ChargingInformation.EstimatedTime.Quick"),
            "m",
        )
        vehicle.ev_estimated_portable_charge_duration = (
            get_child_value(state, "Green.ChargingInformation.EstimatedTime.ICCB"),
            "m",
        )
        vehicle.ev_estimated_station_charge_duration = (
            get_child_value(state, "Green.ChargingInformation.EstimatedTime.Standard"),
            "m",
        )
        vehicle.ev_charge_limits_ac = get_child_value(
            state, "Green.ChargingInformation.TargetSoC.Standard"
        )
        vehicle.ev_charge_limits_dc = get_child_value(
            state, "Green.ChargingInformation.TargetSoC.Quick"
        )
        vehicle.ev_charging_current = get_child_value(
            state, "Green.ChargingInformation.ElectricCurrentLevel.State"
        )
        ev_charging_power = get_child_value(
            state, "Green.Electric.SmartGrid.RealTimePower"
        )
        if ev_charging_power is not None:
            vehicle.ev_charging_power = float(ev_charging_power)
        vehicle.ev_v2l_discharge_limit = get_child_value(
            state, "Green.Electric.SmartGrid.VehicleToLoad.DischargeLimitation.SoC"
        )
        vehicle.ev_target_range_charge_AC = (
            get_child_value(state, "Green.ChargingInformation.DTE.TargetSoC.Standard"),
            DISTANCE_UNITS[get_child_value(state, "Drivetrain.FuelSystem.DTE.Unit")],
        )
        vehicle.ev_target_range_charge_DC = (
            get_child_value(state, "Green.ChargingInformation.DTE.TargetSoC.Quick"),
            DISTANCE_UNITS[get_child_value(state, "Drivetrain.FuelSystem.DTE.Unit")],
        )
        departure1_enable = get_child_value(
            state, "Green.Reservation.Departure.Schedule1.Enable"
        )
        if departure1_enable is not None:
            vehicle.ev_first_departure_enabled = bool(departure1_enable)
        departure2_enable = get_child_value(
            state, "Green.Reservation.Departure.Schedule2.Enable"
        )
        if departure2_enable is not None:
            vehicle.ev_second_departure_enabled = bool(departure2_enable)

        departure1_hvac_temp = get_child_value(
            state, "Green.Reservation.Departure.Schedule1.HVAC.Temperature.Value"
        )
        if departure1_hvac_temp is not None:
            departure1_unit = get_child_value(
                state, "Green.Reservation.Departure.Schedule1.HVAC.Temperature.Unit"
            )
            vehicle.ev_first_departure_climate_temperature = (
                float(departure1_hvac_temp),
                TEMPERATURE_UNITS.get(departure1_unit, TEMPERATURE_UNITS[0]),
            )
        departure2_hvac_temp = get_child_value(
            state, "Green.Reservation.Departure.Schedule2.HVAC.Temperature.Value"
        )
        if departure2_hvac_temp is not None:
            departure2_unit = get_child_value(
                state, "Green.Reservation.Departure.Schedule2.HVAC.Temperature.Unit"
            )
            vehicle.ev_second_departure_climate_temperature = (
                float(departure2_hvac_temp),
                TEMPERATURE_UNITS.get(departure2_unit, TEMPERATURE_UNITS[0]),
            )

        schedule1_time = get_child_value(
            state, "Green.Reservation.Departure.Schedule1.Time"
        )
        if schedule1_time is not None:
            vehicle.ev_first_departure_time = schedule1_time
        schedule1_days = get_child_value(
            state, "Green.Reservation.Departure.Schedule1.DaysOfWeek"
        )
        if schedule1_days is not None:
            vehicle.ev_first_departure_days = schedule1_days
        schedule1_hvac = get_child_value(
            state, "Green.Reservation.Departure.Schedule1.HVAC"
        )
        if isinstance(schedule1_hvac, dict):
            enable = schedule1_hvac.get("Enable")
            if enable is not None:
                vehicle.ev_first_departure_climate_enabled = bool(enable)
            defrost = schedule1_hvac.get("Defrost")
            if defrost is not None:
                vehicle.ev_first_departure_climate_defrost = bool(defrost)

        schedule2_time = get_child_value(
            state, "Green.Reservation.Departure.Schedule2.Time"
        )
        if schedule2_time is not None:
            vehicle.ev_second_departure_time = schedule2_time
        schedule2_days = get_child_value(
            state, "Green.Reservation.Departure.Schedule2.DaysOfWeek"
        )
        if schedule2_days is not None:
            vehicle.ev_second_departure_days = schedule2_days
        schedule2_hvac = get_child_value(
            state, "Green.Reservation.Departure.Schedule2.HVAC"
        )
        if isinstance(schedule2_hvac, dict):
            enable2 = schedule2_hvac.get("Enable")
            if enable2 is not None:
                vehicle.ev_second_departure_climate_enabled = bool(enable2)
            defrost2 = schedule2_hvac.get("Defrost")
            if defrost2 is not None:
                vehicle.ev_second_departure_climate_defrost = bool(defrost2)

        off_peak_start = get_child_value(
            state, "Green.Reservation.OffPeakPower.StartTime"
        )
        if off_peak_start is not None:
            vehicle.ev_off_peak_start_time = off_peak_start
        off_peak_end = get_child_value(state, "Green.Reservation.OffPeakPower.EndTime")
        if off_peak_end is not None:
            vehicle.ev_off_peak_end_time = off_peak_end
        off_peak_only = get_child_value(
            state, "Green.Reservation.OffPeakPower.OffPeakOnly"
        )
        if off_peak_only is not None:
            vehicle.ev_off_peak_charge_only_enabled = bool(off_peak_only)
        charge_schedule_enable = get_child_value(
            state, "Green.Reservation.ChargeSchedule.Enable"
        )
        if charge_schedule_enable is not None:
            vehicle.ev_schedule_charge_enabled = bool(charge_schedule_enable)

        vehicle.washer_fluid_warning_is_on = get_child_value(
            state, "Body.Windshield.Front.WasherFluid.LevelLow"
        )
        vehicle.brake_fluid_warning_is_on = get_child_value(
            state, "Chassis.Brake.Fluid.Warning"
        )
        vehicle.fuel_level = get_child_value(state, "Drivetrain.FuelSystem.FuelLevel")
        vehicle.fuel_level_is_low = get_child_value(
            state, "Drivetrain.FuelSystem.LowFuelWarning"
        )
        vehicle.air_control_is_on = get_child_value(
            state, "Cabin.HVAC.Row1.Driver.Blower.SpeedLevel"
        )
        vehicle.smart_key_battery_warning_is_on = bool_or_none(
            get_child_value(state, "Electronics.FOB.LowBattery")
        )

        side_mirror_heat = get_child_value(state, "Cabin.SideMirror.Heating.State")
        if side_mirror_heat is not None:
            vehicle.side_mirror_heater_is_on = bool(side_mirror_heat)

        bat_pack_voltage = get_child_value(
            state, "Green.BatteryManagement.BatteryPack.Voltage"
        )
        if bat_pack_voltage is not None:
            vehicle.ev_battery_pack_voltage = int(bat_pack_voltage)

        chiller_rpm = get_child_value(state, "Green.BatteryManagement.Chiller.RPM")
        if chiller_rpm is not None:
            vehicle.ev_battery_chiller_rpm = int(chiller_rpm)

        bat_temp_min = get_child_value(state, "Green.BatteryManagement.Temperature.Min")
        bat_temp_max = get_child_value(state, "Green.BatteryManagement.Temperature.Max")
        if isinstance(bat_temp_min, dict):
            bat_temp_min = bat_temp_min.get("Raw")
        if isinstance(bat_temp_max, dict):
            bat_temp_max = bat_temp_max.get("Raw")
        if bat_temp_min is not None:
            vehicle.ev_battery_temperature_min = (int(bat_temp_min), "C")
        if bat_temp_max is not None:
            vehicle.ev_battery_temperature_max = (int(bat_temp_max), "C")

        bat_water_temp = get_child_value(
            state, "Green.BatteryManagement.Temperature.Water"
        )
        if bat_water_temp is not None:
            vehicle.ev_battery_water_temperature = (int(bat_water_temp), "C")

        battery_heating_state = get_child_value(
            state, "Green.BatteryManagement.HeatingState"
        )
        if battery_heating_state is not None:
            vehicle.ev_battery_heating_state = bool(battery_heating_state)

        ev_power_ac = get_child_value(
            state, "Green.EnergyConsumption.AirConditioning.Value"
        )
        if ev_power_ac is not None:
            vehicle.ev_power_consumption_air_conditioning = float(ev_power_ac)
        ev_power_cooling = get_child_value(
            state, "Green.EnergyConsumption.BatteryCooling.Value"
        )
        if ev_power_cooling is not None:
            vehicle.ev_power_consumption_battery_cooling = float(ev_power_cooling)
        ev_power_heater = get_child_value(
            state, "Green.EnergyConsumption.BatteryHeater.Value"
        )
        if ev_power_heater is not None:
            vehicle.ev_power_consumption_battery_heater = float(ev_power_heater)

        winter_mode = get_child_value(
            state, "Green.BatteryManagement.WinterModeOperation"
        )
        if winter_mode is not None:
            vehicle.ev_battery_winter_mode = bool(winter_mode)

        battery_precondition = get_child_value(
            state, "Green.BatteryManagement.BatteryPreCondition"
        )
        if battery_precondition is not None:
            vehicle.ev_battery_precondition_enabled = bool(battery_precondition)

        v2l_mode = get_child_value(state, "Green.Electric.SmartGrid.VehicleToLoad.mode")
        if v2l_mode is not None:
            vehicle.ev_v2l_status = bool(v2l_mode)
        v2x_mode = get_child_value(state, "Green.Electric.SmartGrid.VehicleToGrid.mode")
        if v2x_mode is not None:
            vehicle.ev_v2x_status = bool(v2x_mode)

        total_consumed = get_child_value(
            state, "Green.Electric.SmartGrid.TotalPowerConsumption"
        )
        if total_consumed is not None:
            vehicle.total_power_consumed = float(total_consumed)
        total_regen = get_child_value(
            state, "Green.Electric.SmartGrid.TotalPowerRegeneration"
        )
        if total_regen is not None:
            vehicle.total_power_regenerated = float(total_regen)

        if vehicle._ev_estimated_current_charge_duration is not None:
            if vehicle._ev_estimated_current_charge_duration == 0:
                vehicle.ev_battery_is_charging = False
            elif vehicle._ev_estimated_current_charge_duration > 0:
                vehicle.ev_battery_is_charging = True

        if get_child_value(state, "Location.GeoCoord.Latitude"):
            location_last_updated_at = dt.datetime(
                2000, 1, 1, tzinfo=self.data_timezone
            )
            timestamp = get_child_value(state, "Location.TimeStamp")
            if timestamp is not None:
                location_last_updated_at = dt.datetime(
                    year=int(get_child_value(timestamp, "Year")),
                    month=int(get_child_value(timestamp, "Mon")),
                    day=int(get_child_value(timestamp, "Day")),
                    hour=int(get_child_value(timestamp, "Hour")),
                    minute=int(get_child_value(timestamp, "Min")),
                    second=int(get_child_value(timestamp, "Sec")),
                    tzinfo=self.data_timezone,
                )
            vehicle.location = (
                get_child_value(state, "Location.GeoCoord.Latitude"),
                get_child_value(state, "Location.GeoCoord.Longitude"),
                location_last_updated_at,
            )
            vehicle._location_last_set_time = location_last_updated_at

        # R1 field gaps: drive_mode, oil_level_warning_is_on,
        # battery_auxiliary_fail_warning_is_on (mirror ApiImplType1).
        vehicle.drive_mode = get_child_value(state, "Chassis.DrivingMode.State")
        vehicle.oil_level_warning_is_on = bool_or_none(
            get_child_value(state, "Chassis.Engine.OilLevel.Status")
        )
        vehicle.battery_auxiliary_fail_warning_is_on = bool_or_none(
            get_child_value(state, "Chassis.Battery.Auxiliary.State")
        )

        vehicle.data = state

    # ------------------------------------------------------------------
    # Force refresh
    # ------------------------------------------------------------------

    def force_refresh_vehicle_state(self, token: Token, vehicle: Vehicle) -> None:
        """Wake the vehicle and re-read GSPA stored-status.

        Prewakeup is best-effort (car may be offline). The status read
        returns the last cached state regardless.
        """
        self._validate_ccs_token(token)
        try:
            self.prewakeup(token, vehicle)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - prewakeup failed (car may be offline)")
        # Reset cached-failure flags so the next cached update retries the
        # OTA + valet reads instead of skipping them.
        vehicle._ota_checked = False
        vehicle._valet_failed = False
        self.update_vehicle_with_cached_state(token, vehicle)

    def prewakeup(self, token: Token, vehicle: Vehicle) -> dict[str, Any] | None:
        """Send a prewakeup command to bring the vehicle online.

        GSPA remote paths are brand-global (the path is shared across EU
        CCI brands, issued on the instance's CCSP host).
        """
        car_id = vehicle.id
        url = self.CCSP_API_URL + f"/gspa/v1/remote/vehicles/{car_id}/prewakeup"
        self._validate_ccs_token(token)
        headers = self._get_authenticated_headers(
            token, vehicle.ccu_ccs2_protocol_support or 0
        )
        try:
            response = requests.post(url, headers=headers, timeout=(5, 60))
            if response.status_code == 401:
                raise AuthenticationError("GSPA: Token expired or invalid")
            if response.status_code >= 400:
                raise APIError(
                    f"GSPA control error: HTTP {response.status_code} - "
                    f"{response.text[:200]}"
                )
            data: dict[str, Any] = response.json()
            rc = data.get("rc")
            if rc and rc != "0000":
                raise APIError(f"GSPA error: rc={rc}, msg={data.get('msg', '')}")
            rs: dict[str, Any] = data.get("rs", data)
            return rs
        except AuthenticationError:
            raise
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA prewakeup failed")
            return None

    # ------------------------------------------------------------------
    # Update vehicle with cached state
    # ------------------------------------------------------------------

    def update_vehicle_with_cached_state(self, token: Token, vehicle: Vehicle) -> None:
        """Fetch GSPA stored-status and update vehicle properties.

        GSPA stored-status is the primary status for all vehicles. The
        response shape is:
          {serviceNo, lastUpdateTime, state:{Vehicle:{Body,Cabin,Chassis,
          Drivetrain,Green,Electronics,Location,...}}}
        state.Vehicle is the ccs2 vehicleStatus — fed to
        _update_vehicle_properties_ccs2.
        """
        if not (token.access_token or token.exchangeable_token):
            raise APIError("No CCS token — cannot fetch GSPA stored-status")
        data = self.get_stored_status(token, vehicle)
        if not data:
            raise APIError("GSPA stored-status returned no data")
        state = data.get("state", {})
        if isinstance(state, dict) and "Vehicle" in state:
            state = state["Vehicle"]
        self._update_vehicle_properties_ccs2(vehicle, state)

        if vehicle.engine_type in (ENGINE_TYPES.EV, ENGINE_TYPES.PHEV):
            try:
                state = self._get_driving_info(token, vehicle)
                if state:
                    self._update_vehicle_drive_info(vehicle, state)
            except Exception:
                _LOGGER.debug(f"{DOMAIN} - Driving info fetch failed")
            try:
                history = self._get_driving_history(token, vehicle)
                if history and isinstance(history, dict):
                    self._update_vehicle_driving_history(vehicle, history)
            except Exception:
                _LOGGER.debug(f"{DOMAIN} - Driving history fetch failed")

        self._update_vehicle_gspa_data(token, vehicle)

    def _update_vehicle_gspa_data(self, token: Token, vehicle: Vehicle) -> None:
        """Populate Vehicle GSPA query fields from server-side cached data.

        These are GET endpoints that read cached data from the GSPA server.
        They do NOT wake the telematics unit or drain the 12V battery.
        """
        try:
            sw = self.get_software_version(token, vehicle)
            if sw and isinstance(sw, dict):
                vehicle.software_version = sw.get("softwareVersion")
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA software_version update failed")

        try:
            if not vehicle._ota_checked:
                vehicle._ota_checked = True  # set before call — concurrent workers skip
                ota = self.get_ota_updates(token, vehicle)
                if ota and isinstance(ota, dict):
                    updates = ota.get("otaUpdateList", [])
                    vehicle.ota_update_available = (
                        len(updates) > 0 if updates else False
                    )
        except Exception:
            vehicle.ota_update_available = None  # HA convention: unavailable = unknown
            _LOGGER.debug(f"{DOMAIN} - GSPA ota_updates update failed")

        try:
            if not vehicle._valet_failed:
                vehicle._valet_failed = True  # pessimistic — concurrent workers skip
                valet = self.get_valet_status(token, vehicle)
                vehicle._valet_failed = False  # success — retry next cached update
                if valet and isinstance(valet, dict):
                    mode = valet.get("valetMode", "").lower()
                    vehicle.valet_mode_active = mode == "active"
        except Exception:
            vehicle.valet_mode_active = None  # HA convention: unavailable = unknown
            _LOGGER.debug(f"{DOMAIN} - GSPA valet_status update failed")

        # Stored-status-widget provides lamp, signal, ignition, sleep data
        try:
            widget = self.get_stored_status_widget(token, vehicle)
            if widget and isinstance(widget, dict):
                self._parse_gspa_widget_lamp_data(vehicle, widget)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA stored-status-widget failed")

        # DTC breakdowns
        try:
            bd = self.get_breakdowns(token, vehicle)
            if bd and isinstance(bd, dict):
                self._parse_breakdowns(vehicle, bd)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - GSPA breakdowns update failed")
