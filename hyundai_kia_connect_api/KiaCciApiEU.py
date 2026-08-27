"""KiaCciApiEU.py — Kia EU CCI/GSPA API.

Kia-specific EU implementation inheriting the OneApp (CCI) login flow
and the GSPA secure-request layer from ``GspaApiEU``. This module keeps
Kia brand constants; vehicle listing and cached-state parsing require a
live response fixture (captured during integration validation) and
raise NotImplementedError until then. Extended reads (driving info,
history, breakdowns, DTC) inherit NotImplementedError from ApiImpl.
"""

# pylint:disable=missing-class-docstring,invalid-name

from .GspaApiEU import GspaApiEU
from .Token import Token
from .Vehicle import Vehicle


class KiaCciApiEU(GspaApiEU):
    """Kia EU CCI/GSPA API.

    Uses the CCI login flow (OneApp client_id 01b36c86) confirmed on
    production endpoints. Login, token lifecycle, and the GSPA
    secure-request layer are inherited from ``GspaApiEU``.
    """

    # Brand constants (Kia OneApp EU, confirmed on production endpoints).
    ONEAPP_CLIENT_ID = "01b36c86-79e8-486c-8009-15f2ad88d670"
    ONEAPP_REDIRECT_URI = "https://oneapp.kia.com/redirect"
    CCI_API_URL = "https://cci-api-eu.kia.com"
    CCI_PACKAGE_ID = "com.kia.oneapp.eu"
    GSPA_BASE_URL = "https://gspa-ccs-eu.kia.com/"
    LOGIN_FORM_HOST = "https://idpconnect-eu.kia.com"
    CIPHER_BRAND = "kia"
    REQUEST_ID_HEADER = "DD-REQUEST-ID"
    DEVICE_ID_HEADER = "X-Userdevice-Id"

    # ------------------------------------------------------------------
    # Vehicle list
    # ------------------------------------------------------------------

    def get_vehicles(self, token: Token) -> list[Vehicle]:
        """Fetch vehicle list from the Kia CCI profile endpoint.

        The Kia response parser requires a live response fixture, which
        is captured during integration validation on a real vehicle.
        Until that fixture exists, this method raises.
        """
        raise NotImplementedError(
            "Kia vehicle parser requires a live response fixture "
            "(captured during integration validation)."
        )

    def update_vehicle_with_cached_state(self, token: Token, vehicle: Vehicle) -> None:
        """Update the vehicle with the cached stored-status state.

        Thin wrapper: fetches stored-status via ``get_stored_status``
        (inherited from the base) and parses the Kia-specific response.
        The Kia response parser requires a live response fixture, which
        is captured during integration validation on a real vehicle.
        """
        raise NotImplementedError(
            "Kia cached-state parser requires a live response fixture "
            "(captured during integration validation)."
        )
