"""HyundaiCciApiEU.py — EU CCI API implementation (new architecture)

Uses prd.eu-ccapi.hyundai.com for login (IDPConnect OAuth2),
cci-api-eu.hyundai.com for dashboard/vehicle status, and
gspa-ccs-eu.hyundai.com for CCSP REST control commands.
"""

# pylint:disable=invalid-name,missing-function-docstring,missing-class-docstring,broad-exception-caught,logging-fstring-interpolation

import base64
import json
import logging
import math
import random
import re
import uuid
import datetime as dt
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import requests
from Crypto.Cipher import PKCS1_v1_5
from Crypto.PublicKey import RSA

from .ApiImpl import ApiImpl, ClimateRequestOptions, WindowRequestOptions
from .Token import Token
from .Vehicle import Vehicle
from .const import (
    BRAND_HYUNDAI,
    BRAND_KIA,
    BRAND_GENESIS,
    BRANDS,
    DOMAIN,
    ENGINE_TYPES,
    VEHICLE_LOCK_ACTION,
    CHARGE_PORT_ACTION,
    VALET_MODE_ACTION,
    ORDER_STATUS,
    DISTANCE_UNITS,
    TEMPERATURE_UNITS,
    SEAT_STATUS,
)
from .exceptions import APIError, AuthenticationError
from .KiaUvoApiEU import KiaUvoApiEU
from .utils import get_child_value, parse_datetime

_LOGGER = logging.getLogger(__name__)

USER_AGENT_OK_HTTP = "okhttp/3.12.0"
USER_AGENT_MOZILLA = "Mozilla/5.0 (Linux; Android 12; SM-G973F) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.120 Mobile Safari/537.36"
USER_AGENT_WEBVIEW = (
    "Mozilla/5.0 (Linux; Android 12; SM-G973F Build/SE1A.220630.001.A1; wv) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Version/4.0 Chrome/91.0.4472.114 "
    "Mobile Safari/537.36 HKMCOneApp/1.1.4 "
    "(packageID=com.hyundai.oneapp.eu,locale=PL,lang=en-GB,platform=android,"
    "brand=hyundai,theme=light,isUWB=false,isNFC=false,region=EU,"
    "distanceUnitType=km,speedUnitType=km/h,timezone=+02:00,appVersion=1.1.4)"
    "/HMG_GA_AOS"
)

SUPPORTED_LANGUAGES_LIST = [
    "en",
    "cs",
    "da",
    "de",
    "el",
    "es",
    "fi",
    "fr",
    "hu",
    "it",
    "ko",
    "nb",
    "nl",
    "pl",
    "pt",
    "ro",
    "sk",
    "sv",
    "tr",
]


class HyundaiCciApiEU(ApiImpl):
    data_timezone = ZoneInfo("Europe/Warsaw")
    temperature_range = [x * 0.5 for x in range(28, 60)]
    supports_window_control = True

    def __init__(
        self, region: int, brand: int, language: str, staging: bool = False
    ) -> None:
        language = language.lower()
        if len(language) > 2:
            language = language[0:2]
        if language not in SUPPORTED_LANGUAGES_LIST:
            _LOGGER.warning(f"Unsupported language: {language}, fallback to en")
            language = "en"

        self.LANGUAGE: str = language
        self.brand: int = brand
        self.staging: bool = staging

        if BRANDS[self.brand] == BRAND_HYUNDAI:
            if staging:
                # Staging URLs (from APK decompilation — plaintext config)
                self.CCI_API_URL: str = "https://stg-cci-api-eu.hyundai.com"
                self.CCSP_API_URL: str = "https://stg-gspa-ccs-eu.hyundai.com"
                self.CCAPI_BASE_DOMAIN: str = "stg.eu-ccapi.hyundai.com"
                self.CCAPI_PORT: int = 8080
                self.LOGIN_FORM_HOST: str = "https://stg-idpconnect-eu.hyundai.com"
                self.KEYCLOAK_BASE: str = "https://stg-eu-account.hyundai.com"
                self.KEYCLOAK_REALM: str = "euhyundaiidm"
                self.KEYCLOAK_CLIENT_ID: str = "d858aa54-7ac1-4c3c-bb3b-f3ffe5e68611"
                self.KEYCLOAK_SCOPE: str = (
                    "account.token.transfer account.id.generate account.puid.userinfos "
                    "account.userinfo read account.userinfos puid email name mobileNum "
                    "birthdate lang country signUpDate gender nationInfo certProfile offline"
                )
                self.CCSP_SERVICE_ID: str = "50ebe2d2-a60c-45e7-a950-a341e5d8f119"
                self.CCS_SERVICE_SECRET: str = ""
                self.APP_ID: str = "50ebe2d2-a60c-45e7-a950-a341e5d8f119"
                self.CFB: str = b""
                self.BASIC_AUTHORIZATION: str = (
                    "Basic NTBlYmUyZDItYTYwYy00NWU3LWE5NTAtYTM0MWU1ZDhmMTE5OnNlY3JldA=="
                )
                self.CCSP_APPLICATION_ID: str = "50ebe2d2-a60c-45e7-a950-a341e5d8f119"
                self.PUSH_TYPE: str = "GCM"
                self.PUSH_PROVIDER_ID: str = "838146c3-a519-4eca-8132-ead536d05ce4"
                self._oauth_redirect_uri: str = (
                    "https://stg.eu-ccapi.hyundai.com:8080/api/v1/user/oauth2/redirect"
                )
                _LOGGER.info(f"{DOMAIN} - Using STAGING environment URLs")
            else:
                # Production URLs
                self.CCI_API_URL: str = "https://cci-api-eu.hyundai.com"
                self.CCSP_API_URL: str = "https://gspa-ccs-eu.hyundai.com"
                self.CCAPI_BASE_DOMAIN: str = "prd.eu-ccapi.hyundai.com"
                self.CCAPI_PORT: int = 8080
                self.LOGIN_FORM_HOST: str = "https://idpconnect-eu.hyundai.com"
                self.KEYCLOAK_BASE: str = "https://eu-account.hyundai.com"
                self.KEYCLOAK_REALM: str = "euhyundaiidm"
                self.KEYCLOAK_CLIENT_ID: str = "64621b96-0f0d-11ec-82a8-0242ac130003"
                self.KEYCLOAK_SCOPE: str = (
                    "account.token.transfer account.id.generate account.puid.userinfos "
                    "account.userinfo read account.userinfos puid email name mobileNum "
                    "birthdate lang country signUpDate gender nationInfo certProfile offline"
                )
                self.CCSP_SERVICE_ID: str = "6d477c38-3ca4-4cf3-9557-2a1929a94654"
                self.CCS_SERVICE_SECRET: str = (
                    "KUy49XxPzLpLuoK0xhBC77W6VXhmtQR9iQhmIFjjoY4IpxsV"
                )
                self.APP_ID: str = "014d2225-8495-4735-812d-2616334fd15d"
                self.CFB: str = base64.b64decode(
                    "RFtoRq/vDXJmRndoZaZQyfOot7OrIqGVFj96iY2WL3yyH5Z/pUvlUhqmCxD2t+D65SQ="
                )
                self.BASIC_AUTHORIZATION: str = "Basic NmQ0NzdjMzgtM2NhNC00Y2YzLTk1NTctMmExOTI5YTk0NjU0OktVeTQ5WHhQekxwTHVvSzB4aEJDNzdXNlZYaG10UVI5aVFobUlGampvWTRJcHhzVg=="
                self.CCSP_APPLICATION_ID: str = "6d477c38-3ca4-4cf3-9557-2a1929a94654"
                self.PUSH_TYPE: str = "GCM"
                # OneApp client — used for CCI authorize flow (discovered from HAR)
                self.ONEAPP_CLIENT_ID: str = "4f4953b5-02e1-4dbc-8599-87e983ee1be5"
                self.ONEAPP_REDIRECT_URI: str = "https://oneapp.hyundai.com/redirect"
                self.ONEAPP_SCOPE: str = (
                    "account.token.transfer account.id.generate account.puid.userinfos "
                    "account.userinfo read account.userinfos puid email name mobileNum "
                    "birthdate lang country signUpDate gender nationInfo certProfile offline"
                )
        elif BRANDS[self.brand] == BRAND_KIA:
            raise NotImplementedError(
                "Kia CCI API not yet implemented — traffic capture needed. "
                "Use REGION_EUROPE (old API) for Kia."
            )
        elif BRANDS[self.brand] == BRAND_GENESIS:
            raise NotImplementedError(
                "Genesis CCI API not yet implemented — traffic capture needed. "
                "Use REGION_EUROPE (old API) for Genesis."
            )
        else:
            raise APIError(f"Unknown brand {BRANDS[self.brand]} for CCI EU API")

        # Derived URL constants
        self.CCAPI_BASE_URL: str = f"{self.CCAPI_BASE_DOMAIN}:{self.CCAPI_PORT}"
        self.USER_API_URL: str = f"https://{self.CCAPI_BASE_URL}/api/v1/user/"
        self.SPA_API_URL: str = f"https://{self.CCAPI_BASE_URL}/api/v1/spa/"
        self.SPA_API_URL_V2: str = f"https://{self.CCAPI_BASE_URL}/api/v2/spa/"
        self.CLIENT_ID: str = self.CCSP_SERVICE_ID
        self.GCM_SENDER_ID = 199360397125

        # OAuth2 redirect URI (staging sets this above; production derives it)
        if not hasattr(self, "_oauth_redirect_uri"):
            self._oauth_redirect_uri: str = self.USER_API_URL + "oauth2/token"

        # CCI API paths
        self.CCI_DOMAIN_API_URL = self.CCI_API_URL + "/domain/api/"
        self.CCI_ONEAPP_API_URL = self.CCI_API_URL + "/oneapp/api/"
        # CCSP REST paths
        self.CCSP_SPA_API_URL = self.CCSP_API_URL + "/api/v1/spa/"
        self.CCSP_SPA_API_URL_V2 = self.CCSP_API_URL + "/api/v2/spa/"

        # Delegate for legacy vehicle property mapping (CCAPI format)
        self._legacy_delegate: KiaUvoApiEU | None = None

    # ------------------------------------------------------------------
    # Login — Keycloak (primary) / IDPConnect (fallback)
    # ------------------------------------------------------------------

    def login(
        self,
        username: str,
        password: str,
        pin: str | None = None,
    ) -> Token:
        # Try Keycloak login first (new CCI flow → all 10 token fields)
        try:
            token = self._login_keycloak(username, password, pin)
            return token
        except Exception as ex:
            _LOGGER.warning(
                f"{DOMAIN} - Keycloak login failed: {ex}, falling back to IDPConnect"
            )

        # Fallback: IDPConnect login (old flow → CCAPI tokens only)
        return self._login_idpconnect(username, password, pin)

    def _login_idpconnect(
        self, username: str, password: str, pin: str | None = None
    ) -> Token:
        """Login via IDPConnect with dual auth code flow.

        Signs in twice: once with OneApp client_id (for CCI tokens)
        and once with CCSP client_id (for CCAPI tokens). CCI auth codes
        must come from client 4f4953b5-... and CCSP auth codes from
        client 6d477c38-... — they are NOT interchangeable.
        """
        if self.staging and not self.CCS_SERVICE_SECRET:
            device_id = str(uuid.uuid4())
            _LOGGER.debug(
                f"{DOMAIN} - Staging: skipping stamp/device_id (no secret key)"
            )
        else:
            stamp = self._get_stamp()
            device_id = self._get_device_id(stamp)
        cookies = self._get_cookies()
        self._set_session_language(cookies)

        if self._looks_like_refresh_token(password):
            temp_token = Token(
                username=username,
                password=password,
                refresh_token=password,
                device_id=device_id,
                client_device_id=device_id,
                pin=pin,
            )
            refreshed = self._refresh_idpconnect_token(temp_token)
            return refreshed

        # Step 1-3: Sign in via IDPConnect with OneApp client for CCI
        # CCI requires auth codes from client 4f4953b5-... (OneApp/web)
        cci_auth_code = self._signin_idpconnect(
            username,
            password,
            client_id=self.ONEAPP_CLIENT_ID,
            redirect_uri=self.ONEAPP_REDIRECT_URI,
            scope=self.ONEAPP_SCOPE,
        )

        # Step 4a: Try CCI exchange FIRST (auth codes are one-time use)
        temp_token = Token(device_id=device_id, client_device_id=device_id)
        self._exchange_auth_code_for_cci_tokens(temp_token, cci_auth_code)

        # Step 4b: Sign in again with CCSP client for CCAPI tokens
        # CCSP requires auth codes from client 6d477c38-... (CCSP/Android)
        ccsp_auth_code = self._signin_idpconnect(username, password)

        # Step 4c: Exchange CCSP auth code at CCAPI
        host = self.LOGIN_FORM_HOST
        client_id = self.CCSP_SERVICE_ID
        client_secret = self.CCS_SERVICE_SECRET
        redirect_uri = self._oauth_redirect_uri

        resp = requests.post(
            f"{host}/auth/api/v2/user/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": ccsp_auth_code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

        if resp.status_code != 200:
            raise AuthenticationError(
                f"API error: token exchange failed: HTTP {resp.status_code} — "
                f"{resp.text[:200]}. This may indicate a Hyundai API change."
            )

        tokens = resp.json()
        access_token = tokens["token_type"] + " " + tokens["access_token"]
        new_refresh_token = tokens["refresh_token"]
        expires_in = int(tokens.get("expires_in", 86400))

        valid_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=expires_in
        )

        token = Token(
            username=username,
            password=password,
            access_token=access_token,
            refresh_token=new_refresh_token,
            device_id=device_id,
            valid_until=valid_until,
            pin=pin,
            client_device_id=device_id,
            id_token=temp_token.id_token,
            exchangeable_token=temp_token.exchangeable_token,
            non_ccs_token=temp_token.non_ccs_token,
            exchangeable_refresh_token=temp_token.exchangeable_refresh_token,
            non_ccs_refresh_token=temp_token.non_ccs_refresh_token,
        )

        # Exchange CCS token and register device
        self._exchange_ccs_token(token)
        self._register_device(token)
        self._initialize_session(token)

        return token

    @staticmethod
    def _looks_like_refresh_token(value: str) -> bool:
        return bool(re.match(r"^[A-Z0-9]{48}$", value))

    def _login_keycloak(
        self, username: str, password: str, pin: str | None = None
    ) -> Token:
        """Login via Keycloak OAuth2 → CCI /domain/api/v1/auth/token.

        Returns a Token with all CCI fields populated (exchangeable_token,
        id_token, non_ccs_token, etc.).
        """
        # Generate stable client_device_id
        client_device_id = str(uuid.uuid4())

        # Step 1: Keycloak Authorization Code flow (headless)
        auth_code = self._keycloak_authorize(username, password)

        # Step 2: Exchange auth code at CCI /domain/api/v1/auth/token
        temp_token = Token(
            device_id=client_device_id, client_device_id=client_device_id
        )
        self._exchange_auth_code_for_cci_tokens(temp_token, auth_code)

        # If CCI exchange failed, we won't have CCI-specific fields
        if not temp_token.id_token:
            raise AuthenticationError(
                "CCI auth/token exchange failed — no id_token returned"
            )

        # Build Token from CCI response — use temp_token fields
        token = Token(
            username=username,
            password=password,
            access_token=temp_token.access_token or "",
            refresh_token=temp_token.refresh_token,
            device_id=client_device_id,
            valid_until=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=23),
            pin=pin,
            id_token=temp_token.id_token,
            exchangeable_token=temp_token.exchangeable_token,
            non_ccs_token=temp_token.non_ccs_token,
            exchangeable_refresh_token=temp_token.exchangeable_refresh_token,
            non_ccs_refresh_token=temp_token.non_ccs_refresh_token,
            client_device_id=client_device_id,
        )

        # Exchange CCS token and register device
        self._exchange_ccs_token(token)
        self._register_device(token)
        self._initialize_session(token)

        return token

    def _keycloak_authorize(self, username: str, password: str) -> str:
        """Headless Keycloak OAuth2 Authorization Code flow.

        1. GET /auth/realms/{realm}/protocol/openid-connect/auth → login form
        2. POST credentials → redirect with authorization_code
        """
        base = f"{self.KEYCLOAK_BASE}/auth/realms/{self.KEYCLOAK_REALM}"
        redirect_uri = self.CCI_API_URL + "/domain/api/v1/auth/token"
        state = str(uuid.uuid4())

        auth_url = (
            f"{base}/protocol/openid-connect/auth"
            f"?response_type=code"
            f"&client_id={self.KEYCLOAK_CLIENT_ID}"
            f"&redirect_uri={redirect_uri}"
            f"&scope={self.KEYCLOAK_SCOPE}"
            f"&state={state}"
        )

        s = requests.Session()
        s.headers.update({"User-Agent": USER_AGENT_WEBVIEW})

        # Load the auth page — Keycloak will redirect to login form
        resp = s.get(auth_url, allow_redirects=True)
        if resp.status_code != 200:
            raise AuthenticationError(
                f"Keycloak auth page failed: HTTP {resp.status_code}"
            )

        # Parse the login form — extract action URL and hidden fields
        form_action, form_data = self._parse_keycloak_login_form(resp.text)
        form_data["username"] = username
        form_data["password"] = password

        # POST login credentials
        resp = s.post(
            form_action,
            data=form_data,
            allow_redirects=False,
        )

        # Keycloak returns 302 with Location containing ?code=...
        # May need to follow multiple redirects
        max_redirects = 10
        for _ in range(max_redirects):
            if resp.status_code == 302:
                location = resp.headers.get("location", "")

                # Check if redirect contains authorization code
                parsed = urlparse(location)
                params = parse_qs(parsed.query)
                if "code" in params:
                    return params["code"][0]

                # Check for login errors
                if "error" in params:
                    error_desc = params.get("error_description", ["unknown"])[0]
                    raise AuthenticationError(
                        f"Keycloak authentication rejected: {error_desc}"
                    )

                # Follow redirect
                if location.startswith("/"):
                    location = f"{self.KEYCLOAK_BASE}{location}"
                resp = s.get(location, allow_redirects=False)
            elif resp.status_code == 200:
                # Maybe the form was re-rendered (wrong credentials)
                if "login" in resp.text.lower() and "error" in resp.text.lower():
                    raise AuthenticationError(
                        "Keycloak login failed — check username and password"
                    )
                break
            else:
                break

        raise AuthenticationError(
            "Keycloak login failed — no authorization code in redirect chain"
        )

    def _parse_keycloak_login_form(self, html: str) -> tuple[str, dict]:
        """Extract form action URL and hidden fields from Keycloak login page."""
        action_match = re.search(r'<form[^>]+action="([^"]+)"', html)
        if not action_match:
            raise AuthenticationError("Cannot find Keycloak login form")

        form_action = action_match.group(1).replace("&amp;", "&")

        # Extract hidden input fields
        form_data = {}
        for match in re.finditer(
            r'<input[^>]+type="hidden"[^>]+name="([^"]+)"[^>]+value="([^"]*)"', html
        ):
            form_data[match.group(1)] = match.group(2)

        return form_action, form_data

    def _exchange_auth_code_for_cci_tokens(self, token: Token, auth_code: str) -> None:
        """POST authorization_code to CCI /domain/api/v1/auth/token.

        Per live traffic (HAR), the auth code goes in the URL query parameter
        only (NOT the request body). The body is empty (Content-Length: 0).

        X-Fingerprint HMAC header is REQUIRED on this endpoint.
        Without it: error 9035. With invalid HMAC: error 9036.
        The HMAC constants are xshield-encrypted — Frida needed to extract them.
        When X-Fingerprint is not available, this method will log a warning
        and attempt the request anyway (which will fail with 9035 on production).

        Populates CCI fields on the token object (id_token, exchangeable_token,
        non_ccs_token, etc.) if successful. Fails silently if CCI rejects.
        """
        # Auth code in URL query parameter per HAR capture (NOT in body)
        url = self.CCI_DOMAIN_API_URL + f"v1/auth/token?code={auth_code}"

        timestamp = str(int(dt.datetime.now(dt.timezone.utc).timestamp() * 1000))

        headers = {
            "client-id": "com.hyundai.oneapp.eu",
            "client-name": "HyundaiEU",
            "client-version": "1.1.5",
            "client-os-code": "ios",
            "client-os-version": "26.5.0",
            "client-device-id": token.client_device_id or token.device_id or "",
            "client-notification-provider-type": "GCM",
            "X-Timestamp": timestamp,
            "Accept": "application/json",
            "Accept-Language": "en",
            "app-request-id": str(uuid.uuid4()),
            "User-Agent": (
                "HyundaiEU/1.1.5 (com.hyundai.oneapp.eu; build:1312; "
                "iOS 26.5.0) Alamofire/5.11.1"
            ),
        }

        # X-Fingerprint: HMAC-SHA256 required on auth/token endpoint.
        # Cannot compute without xshield-decrypted constants (need Frida).
        fingerprint = self._compute_x_fingerprint(headers, "")
        if fingerprint:
            headers["X-Fingerprint"] = fingerprint
        else:
            _LOGGER.warning(
                f"{DOMAIN} - X-Fingerprint HMAC not available — "
                "CCI auth/token will fail with 9035 on production. "
                "Frida required to extract xshield constants."
            )

        try:
            response = requests.post(url, headers=headers, data=b"", timeout=30)
            if response.status_code != 200:
                _LOGGER.debug(
                    f"{DOMAIN} - CCI auth/token failed: HTTP {response.status_code}"
                )
                return

            data = response.json()
            # Handle concatenated JSON response (3 objects separated by }{)
            if isinstance(data, dict) and not data.get("accessToken"):
                text = response.text
                if text.count("}{") > 0:
                    parts = text.replace("}{", "}|{").split("|")
                    data = json.loads(parts[-1])

            token.id_token = data.get("idToken")
            token.exchangeable_token = data.get("exchangeableAccessToken")
            token.non_ccs_token = data.get("nonCcsToken")
            token.exchangeable_refresh_token = data.get("exchangeableRefreshToken")
            token.non_ccs_refresh_token = data.get("nonCcsRefreshToken")
            _LOGGER.info(
                f"{DOMAIN} - CCI tokens obtained from auth code exchange: "
                f"id_token={bool(token.id_token)}, "
                f"exchangeable={bool(token.exchangeable_token)}"
            )
        except Exception as ex:
            _LOGGER.debug(f"{DOMAIN} - CCI auth/token exchange failed: {ex}")

    @staticmethod
    def _compute_x_fingerprint(headers: dict, body: str) -> str | None:
        """Compute X-Fingerprint HMAC-SHA256 for CCI auth/token requests.

        Requires xshield-decrypted constants from the Android APK:
        - C28417dc.m39750(1672787045) — static secret (part of HMAC key)
        - C28417dc.m39743(-620636908) — key suffix (part of HMAC key)
        - 5 xshield-encrypted header names (key + message components)
        - client-id value used in HMAC message

        These constants are encrypted via xshield runtime string encryption
        and can only be extracted by hooking the APK at runtime (Frida).

        Returns None when constants are unavailable (current state).
        Once Frida dumps the values, implement the full computation here.

        HMAC computation (from decompiled HmacWrapper.kt):
        1. Remove X-Fingerprint and X-Timestamp from request headers
        2. Lowercase all header keys
        3. HMAC key = concat(5 header values + static_secret + key_suffix)
        4. HMAC message = client-id + 4 header values + timestamp + regexStrip(body)
        5. X-Fingerprint = hex(HMAC-SHA256(key, message))
        """
        # TODO: Implement once xshield constants are extracted via Frida
        # Key classes: C27173c (HmacWrapper), C27178a (HmacKeyHelper),
        # C27179b (HmacMessageHelper), C27190c (HmacAlgorithmHelper)
        return None

    def _login_with_password(
        self, username: str, password: str
    ) -> tuple[str, str, int]:
        """Headless login via IDPConnect — exchanges auth code at CCAPI internally.

        Returns (access_token, refresh_token, expires_in).
        NOTE: The auth code is consumed by CCAPI in this method.
        Use _signin_idpconnect() directly if you need the auth code
        for CCI exchange before CCAPI consumes it.
        """
        code = self._signin_idpconnect(username, password)

        host = self.LOGIN_FORM_HOST
        client_id = self.CCSP_SERVICE_ID
        client_secret = self.CCS_SERVICE_SECRET
        redirect_uri = self._oauth_redirect_uri

        resp = requests.post(
            f"{host}/auth/api/v2/user/oauth2/token",
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": client_id,
                "client_secret": client_secret,
            },
        )

        if resp.status_code != 200:
            raise AuthenticationError(
                f"API error: token exchange failed: HTTP {resp.status_code} — "
                f"{resp.text[:200]}. This may indicate a Hyundai API change."
            )

        tokens = resp.json()
        access_token = tokens["token_type"] + " " + tokens["access_token"]
        new_refresh_token = tokens["refresh_token"]
        expires_in = int(tokens.get("expires_in", 86400))

        return access_token, new_refresh_token, expires_in

    def _signin_idpconnect(
        self,
        username: str,
        password: str,
        client_id: str | None = None,
        redirect_uri: str | None = None,
        scope: str | None = None,
    ) -> str:
        """Sign in via IDPConnect and return the authorization code.

        Steps: authorize → certs → signin → auth code.
        The auth code has NOT been exchanged yet — caller decides order.

        Args:
            client_id: OAuth2 client_id (default: CCSP_SERVICE_ID for CCAPI).
                Use ONEAPP_CLIENT_ID for CCI authorize flow.
            redirect_uri: OAuth2 redirect URI (default: self._oauth_redirect_uri).
                Use ONEAPP_REDIRECT_URI for CCI authorize flow.
            scope: OAuth2 scope string (default: empty, which works for CCSP).
                Use ONEAPP_SCOPE for CCI authorize flow.
        """
        host = self.LOGIN_FORM_HOST
        client_id = client_id or self.CCSP_SERVICE_ID
        redirect_uri = redirect_uri or self._oauth_redirect_uri

        mobile_ua = USER_AGENT_MOZILLA + "_CCS_APP_AOS"

        s = requests.Session()
        s.headers.update({"User-Agent": mobile_ua})

        # Step 1: Load authorize page to get session cookies and connector_session_key
        scope_param = f"&scope={scope}" if scope else ""
        auth_url = (
            f"{host}/auth/api/v2/user/oauth2/authorize"
            f"?response_type=code&client_id={client_id}"
            f"&redirect_uri={redirect_uri}&lang=en&state=ccsp&country=de"
            f"{scope_param}"
        )
        auth_resp = s.get(auth_url, allow_redirects=True)

        # Extract connector_session_key from redirect URL (bluelinky pattern)
        connector_session_key = ""
        final_url = auth_resp.url
        cskey_match = re.search(
            r"connector_session_key(?:%3D|=)([0-9a-fA-F-]{36})", final_url
        )
        if cskey_match:
            connector_session_key = cskey_match.group(1)

        # Step 2: Get RSA public key for password encryption
        resp = s.get(f"{host}/auth/api/v1/accounts/certs")
        if resp.status_code != 200:
            raise AuthenticationError(
                f"API error: failed to fetch RSA certs: HTTP {resp.status_code}. "
                "This may indicate a Hyundai API change."
            )
        jwk = resp.json().get("retValue", {})
        kid = jwk.get("kid", "")

        # Convert JWK to RSA key
        n_bytes = base64.urlsafe_b64decode(jwk["n"] + "==")
        e_bytes = base64.urlsafe_b64decode(jwk["e"] + "==")
        n = int.from_bytes(n_bytes, "big")
        e = int.from_bytes(e_bytes, "big")
        key = RSA.construct((n, e))
        cipher = PKCS1_v1_5.new(key)
        encrypted_pw = cipher.encrypt(password.encode("utf-8")).hex()

        # Step 3: POST signin with encrypted password
        resp = s.post(
            f"{host}/auth/account/signin",
            data={
                "client_id": client_id,
                "encryptedPassword": "true",
                "password": encrypted_pw,
                "redirect_uri": redirect_uri,
                "scope": "",
                "nonce": "",
                "state": "ccsp",
                "username": username,
                "connector_session_key": connector_session_key,
                "kid": kid,
                "_csrf": "",
            },
            allow_redirects=False,
        )

        if resp.status_code != 302:
            raise AuthenticationError(
                f"Signin failed: HTTP {resp.status_code} — {resp.text[:300]}. "
                "Check username and password."
            )

        location = resp.headers.get("location", "")
        code_list = parse_qs(urlparse(location).query).get("code")
        if not code_list:
            if "error" in location.lower():
                error_desc = parse_qs(urlparse(location).query).get(
                    "error_description", ["unknown"]
                )[0]
                raise AuthenticationError(
                    f"Authentication rejected: {error_desc}. "
                    "Check username and password."
                )
            if "authorize" in location:
                raise AuthenticationError(
                    "Authentication failed — returned to login page. "
                    "Check username and password."
                )
            raise AuthenticationError(
                f"API error: unexpected redirect after signin: {location[:250]}"
            )

        return code_list[0]

    def refresh_access_token(self, token: Token) -> Token:
        """Refresh access token — tries Keycloak/CCI first, then IDPConnect."""
        # 1. Try CCI token refresh (if we have exchangeable_token from Keycloak login)
        if token.exchangeable_token and token.exchangeable_refresh_token:
            try:
                return self._refresh_cci_token(token)
            except Exception as ex:
                _LOGGER.warning(
                    f"{DOMAIN} - CCI token refresh failed: {ex}, "
                    "trying IDPConnect refresh"
                )

        # 2. Try IDPConnect refresh_token grant
        if token.refresh_token:
            try:
                return self._refresh_idpconnect_token(token)
            except Exception:
                _LOGGER.warning(
                    f"{DOMAIN} - IDPConnect refresh failed, falling back to full login"
                )

        # 3. Full login
        return self.login(token.username, token.password, token.pin)

    def _refresh_cci_token(self, token: Token) -> Token:
        """Refresh CCI tokens via /domain/api/v2/auth/token-refresh.

        Sends all token fields per live traffic capture (HAR).
        v2 confirmed in production iOS traffic (not v1 as APK suggests).
        """
        url = self.CCI_DOMAIN_API_URL + "v2/auth/token-refresh"
        headers = self._get_cci_headers(token)
        headers["Content-Type"] = "application/json"

        # Live traffic (HAR) sends all token fields in refresh request
        body = {
            "accessToken": (token.access_token or "").removeprefix("Bearer "),
            "refreshToken": token.refresh_token or "",
            "exchangeableAccessToken": token.exchangeable_token or "",
            "exchangeableRefreshToken": token.exchangeable_refresh_token or "",
            "nonCcsToken": token.non_ccs_token or "",
            "nonCcsRefreshToken": token.non_ccs_refresh_token or "",
            "idToken": token.id_token or "",
        }

        response = requests.post(url, headers=headers, json=body, timeout=30)
        if response.status_code != 200:
            raise AuthenticationError(
                f"CCI token refresh failed: HTTP {response.status_code}"
            )

        data = response.json()
        expires_in = int(data.get("expiresIn", 86400))
        valid_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=expires_in
        )

        # New exchangeable-token may come via set-cookie header
        set_cookie = response.headers.get("set-cookie", "")
        if "t=" in set_cookie:
            match = re.search(r"t=([^;]+)", set_cookie)
            if match:
                new_exchangeable = match.group(1)
                if new_exchangeable:
                    token.exchangeable_token = new_exchangeable

        # Also check response body for updated tokens
        if data.get("exchangeableAccessToken"):
            token.exchangeable_token = data["exchangeableAccessToken"]
        if data.get("accessToken"):
            access_token = data["accessToken"]
            if not access_token.startswith("Bearer"):
                access_token = "Bearer " + access_token
            token.access_token = access_token
        if data.get("refreshToken"):
            token.refresh_token = data["refreshToken"]
        if data.get("exchangeableRefreshToken"):
            token.exchangeable_refresh_token = data["exchangeableRefreshToken"]
        if data.get("idToken"):
            token.id_token = data["idToken"]
        if data.get("nonCcsToken"):
            token.non_ccs_token = data["nonCcsToken"]

        token.valid_until = valid_until

        # Re-exchange CCS token
        self._exchange_ccs_token(token)

        return token

    def _refresh_idpconnect_token(self, token: Token) -> Token:
        """Refresh access token via IDPConnect refresh_token grant."""
        host = self.LOGIN_FORM_HOST
        client_id = self.CCSP_SERVICE_ID
        client_secret = self.CCS_SERVICE_SECRET

        resp = requests.post(
            f"{host}/auth/api/v2/user/oauth2/token",
            data={
                "grant_type": "refresh_token",
                "refresh_token": token.refresh_token,
                "client_id": client_id,
                "client_secret": client_secret,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            raise AuthenticationError(
                f"IDPConnect refresh failed: HTTP {resp.status_code}"
            )

        tokens = resp.json()
        access_token = tokens["token_type"] + " " + tokens["access_token"]
        new_refresh_token = tokens.get("refresh_token") or token.refresh_token
        expires_in = int(tokens.get("expires_in", 86400))
        valid_until = dt.datetime.now(dt.timezone.utc) + dt.timedelta(
            seconds=expires_in
        )

        new_token = Token(
            username=token.username,
            password=token.password,
            access_token=access_token,
            refresh_token=new_refresh_token,
            device_id=token.device_id,
            valid_until=valid_until,
            pin=token.pin,
            id_token=token.id_token,
            exchangeable_token=token.exchangeable_token,
            non_ccs_token=token.non_ccs_token,
            exchangeable_refresh_token=token.exchangeable_refresh_token,
            non_ccs_refresh_token=token.non_ccs_refresh_token,
            client_device_id=token.client_device_id,
            ccs_token=token.ccs_token,
        )

        # Re-exchange CCS token for CCI API
        self._exchange_ccs_token(new_token)

        return new_token

    def test_token(self, token: Token) -> bool:
        # Try CCI API first
        url = self.CCI_DOMAIN_API_URL + "v1/vehicle/available-vehicles?detail=false"
        headers = self._get_cci_headers(token)
        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                return True
        except Exception:
            pass

        # Fallback: try CCSP REST
        url = self.SPA_API_URL + "vehicles/" + (token.device_id or "")
        headers = self._get_authenticated_headers(token)
        try:
            response = requests.get(url, headers=headers, timeout=30)
            return response.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------
    # CCSP legacy helpers (stamp, device_id, cookies, headers)
    # These are used for IDPConnect login and CCSP REST fallback
    # ------------------------------------------------------------------

    def _get_stamp(self) -> str:
        raw_data = f"{self.APP_ID}:{int(dt.datetime.now().timestamp())}".encode()
        result = bytes(b1 ^ b2 for b1, b2 in zip(self.CFB, raw_data))
        return base64.b64encode(result).decode("utf-8")

    def _get_device_id(self, stamp: str) -> str:
        my_hex = "%064x" % random.randrange(10**80)
        registration_id = my_hex[:64]
        url = self.SPA_API_URL + "notifications/register"
        payload = {
            "pushRegId": registration_id,
            "pushType": self.PUSH_TYPE,
            "uuid": str(uuid.uuid4()),
        }

        headers = {
            "ccsp-service-id": self.CCSP_SERVICE_ID,
            "ccsp-application-id": self.APP_ID,
            "Stamp": stamp,
            "Content-Type": "application/json;charset=UTF-8",
            "Host": self.CCAPI_BASE_URL,
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "User-Agent": USER_AGENT_OK_HTTP,
        }

        response = requests.post(url, headers=headers, json=payload, timeout=30)
        response = response.json()
        if "resMsg" not in response or "deviceId" not in response["resMsg"]:
            raise APIError(f"Device registration failed: {response}")
        return response["resMsg"]["deviceId"]

    def _get_cookies(self) -> dict:
        url = (
            self.USER_API_URL
            + "oauth2/authorize?response_type=code&state=test&client_id="
            + self.CLIENT_ID
            + "&redirect_uri="
            + self.USER_API_URL
            + "oauth2/token&lang="
            + self.LANGUAGE
        )
        session = requests.Session()
        _ = session.get(url, timeout=30)
        return session.cookies.get_dict()

    def _set_session_language(self, cookies: dict) -> None:
        url = self.USER_API_URL + "language"
        headers = {"Content-Type": "application/json", "User-Agent": USER_AGENT_OK_HTTP}
        try:
            requests.put(url, headers=headers, json={"lang": self.LANGUAGE}, timeout=30)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Set session language failed (non-critical)")

    def _get_authenticated_headers(self, token: Token, ccs2_support: int = 0) -> dict:
        return {
            "Authorization": token.access_token,
            "ccsp-device-id": token.device_id,
            "ccsp-service-id": self.CCSP_SERVICE_ID,
            "ccsp-application-id": self.APP_ID,
            "Stamp": self._get_stamp(),
            "Ccuccs2protocolsupport": str(ccs2_support),
            "Content-Type": "application/json",
            "Host": self.CCAPI_BASE_URL,
            "Connection": "keep-alive",
            "Accept": "application/json",
            "Accept-Encoding": "gzip, deflate",
            "User-Agent": USER_AGENT_OK_HTTP,
        }

    def _get_control_headers(self, token: Token, vehicle: Vehicle) -> dict:
        control_token, _ = self._get_control_token(token)
        headers = self._get_authenticated_headers(
            token, vehicle.ccu_ccs2_protocol_support or 0
        )
        headers["Authorization"] = control_token
        headers["AuthorizationCCSP"] = control_token
        return headers

    # ------------------------------------------------------------------
    # CCI API headers (for cci-api-eu.hyundai.com)
    # ------------------------------------------------------------------

    def _get_cci_headers(self, token: Token, vehicleinfo: str | None = None) -> dict:
        """Headers for CCI API (cci-api-eu.hyundai.com).

        Header names confirmed from live iOS traffic (HAR capture):
        - exchangeable-token (kebab-case, NOT exchangeableAccessToken)
        - non-ccs-token (kebab-case, NOT nonCcsToken)
        - Authentication (PascalCase)
        """
        headers = {
            "Authentication": token.id_token or "",
            "exchangeable-token": token.exchangeable_token or "",
            "non-ccs-token": token.non_ccs_token or "",
            "authorization": f"Bearer {token.exchangeable_token or ''}",
            "client-id": self.CLIENT_ID,
            "client-name": "HyundaiEU",
            "client-version": "1.1.5",
            "client-os-code": "ios",
            "client-device-id": token.client_device_id or token.device_id or "",
            "client-device-model": "iPhone",
            "client-os-version": "26.5",
            "client-notification-provider-type": "IOS_APPSTORE",
            "locale": self.LANGUAGE.upper(),
            "timezone": self._get_timezone_offset(),
            "Accept-Language": self.LANGUAGE,
            "User-Agent": (
                "HyundaiEU/1.1.5 (com.hyundai.oneapp.eu; build:1312; "
                "iOS 26.5.0) Alamofire/5.11.1"
            ),
        }
        if token.client_device_id:
            app_req_id = f"{uuid.uuid4()}-{token.client_device_id[:8]}_iOS"
            headers["app-request-id"] = app_req_id
        if vehicleinfo:
            headers["vehicleinfo"] = vehicleinfo
        return headers

    def _get_ccsp_headers(self, token: Token, ccs2_support: int = 0) -> dict:
        """Headers for CCSP REST (gspa-ccs-eu.hyundai.com) using CCS token.

        Based on CCS SDK interceptor chain from APK decompilation.
        """
        headers = {
            "Authorization": f"Bearer {token.ccs_token or ''}",
            "ccsp-service-id": self.CCSP_SERVICE_ID,
            "ccsp-application-id": self.CCSP_APPLICATION_ID,
            "ccsp-device-id": token.client_device_id or token.device_id or "",
            "Ccuccs2protocolsupport": str(ccs2_support),
            "client-id": self.CCSP_SERVICE_ID,
            "client-version": "1.1.4",
            "Language": self.LANGUAGE,
            "Content-Type": "application/json",
            "User-Agent": USER_AGENT_OK_HTTP,
        }
        return headers

    def _get_ccsp_control_headers(self, token: Token, vehicle: Vehicle) -> dict:
        control_token, _ = self._get_control_token(token)
        headers = self._get_ccsp_headers(token, vehicle.ccu_ccs2_protocol_support or 0)
        headers["Authorization"] = control_token
        headers["AuthorizationCCSP"] = control_token
        return headers

    def _get_timezone_offset(self) -> str:
        offset = dt.datetime.now().utcoffset()
        if offset is None:
            return "+00:00"
        sign = "+" if offset.total_seconds() >= 0 else "-"
        total = int(abs(offset.total_seconds()) // 60)
        hours, minutes = divmod(total, 60)
        return f"{sign}{hours:02d}:{minutes:02d}"

    def _build_vehicleinfo_header(self, vehicle_data: dict) -> str:
        return base64.b64encode(json.dumps(vehicle_data).encode()).decode()

    # ------------------------------------------------------------------
    # Token Exchange & Device Registration (CCI-specific)
    # ------------------------------------------------------------------

    def _exchange_ccs_token(self, token: Token) -> None:
        """Exchange CCI tokens for a CCS token used on gspa-ccs-eu."""
        url = self.CCI_DOMAIN_API_URL + "v1/auth/token-exchange?serviceType=CCS"
        headers = self._get_cci_headers(token)
        headers["Content-Length"] = "0"

        try:
            response = requests.post(url, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                # APK decompilation confirms field name is "accessToken" (camelCase)
                token.ccs_token = data.get("accessToken") or data.get("ccsAccessToken")
                if not token.exchangeable_token and data.get("exchangeableAccessToken"):
                    token.exchangeable_token = data["exchangeableAccessToken"]
                if not token.id_token and data.get("idToken"):
                    token.id_token = data["idToken"]
                if not token.non_ccs_token and data.get("nonCcsToken"):
                    token.non_ccs_token = data["nonCcsToken"]
            else:
                _LOGGER.debug(
                    f"{DOMAIN} - CCS token exchange failed: HTTP {response.status_code}, "
                    "falling back to CCAPI access_token for CCSP"
                )
        except Exception as ex:
            _LOGGER.debug(f"{DOMAIN} - CCS token exchange failed: {ex}")

    def _register_device(self, token: Token) -> None:
        """Register device on CCI API for push notifications.

        Body format confirmed from live iOS traffic (HAR capture):
        - appToken: FCM/APNs registration token or stable identifier
        - deviceToken: client_device_id (stable UUID)
        - deviceModel, deviceAppVer, deviceOsVer
        Response: {"deviceId": "...", "userDeviceId": "..."}
        """
        url = self.CCI_DOMAIN_API_URL + "v3/notifications/bases/devices"
        headers = self._get_cci_headers(token)
        headers["Content-Type"] = "application/json"

        device_id = token.client_device_id or token.device_id
        body = {
            "appToken": str(uuid.uuid4()),
            "deviceModel": "iPhone",
            "deviceAppVer": "1.1.5",
            "deviceOsVer": "26.5",
            "deviceToken": device_id,
        }
        try:
            response = requests.post(url, headers=headers, json=body, timeout=30)
            if response.status_code == 200:
                data = response.json()
                # Server returns deviceId/userDeviceId — store for language pref
                server_device_id = data.get("deviceId") or data.get("userDeviceId")
                if server_device_id and not token.ccs_token:
                    # Store server-assigned device ID for language preference
                    token._server_device_id = server_device_id
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Device registration failed (non-critical)")

    def _initialize_session(self, token: Token) -> None:
        """Set language preference on CCI API.

        Body format confirmed from live iOS traffic (HAR capture):
        {"countryCd": "PL", "deviceId": "<server_device_id>", "langCd": "pl"}
        """
        lang_url = (
            self.CCI_DOMAIN_API_URL + "v3/notifications/settings/preferences/language"
        )
        headers = self._get_cci_headers(token)
        headers["Content-Type"] = "application/json"

        server_device_id = getattr(token, "_server_device_id", None)
        body = {
            "countryCd": self.LANGUAGE.upper(),
            "deviceId": server_device_id or token.client_device_id or token.device_id,
            "langCd": self.LANGUAGE,
        }
        try:
            requests.put(lang_url, headers=headers, json=body, timeout=30)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Session language set failed (non-critical)")

    # ------------------------------------------------------------------
    # Control Token (PIN verification)
    # ------------------------------------------------------------------

    def _get_control_token(self, token: Token) -> tuple:
        """PIN verification — tries CCI endpoint first, then CCSP, then CCAPI.

        CCI endpoint (from APK decompilation):
          POST {cciBaseUrl}domain/api/v1/auth/pin
          Body: {"pin": "<pin_code>"}
          Response: {"controlTokenInfo": {"controlToken": "...", "expiresTime": <ms_timestamp>}}

        CCSP/CCAPI endpoints use older format:
          {"controlToken": "...", "expiresTime": <seconds_duration>}
        """
        data = {"pin": token.pin}

        # 1. CCI endpoint (domain/api/v1/auth/pin)
        cci_url = self.CCI_DOMAIN_API_URL + "v1/auth/pin"
        cci_headers = self._get_cci_headers(token)
        cci_headers["Content-Type"] = "application/json"

        try:
            response = requests.post(
                cci_url, json=data, headers=cci_headers, timeout=30
            )
            if response.status_code == 200:
                resp = response.json()
                if resp.get("isMatched") is True:
                    info = resp.get("controlTokenInfo", {})
                    ct = info.get("controlToken")
                    if ct:
                        # expiresTime is ms timestamp in CCI, convert to epoch seconds
                        expires_ms = info.get("expiresTime", 0)
                        expire_at = (
                            expires_ms // 1000 if expires_ms > 1e12 else expires_ms
                        )
                        return "Bearer " + ct, expire_at
                elif resp.get("controlToken"):
                    # Fallback format: controlToken at top level
                    ct = resp["controlToken"]
                    expires_s = resp.get("expiresTime", 300)
                    expire_at = math.floor(dt.datetime.now().timestamp() + expires_s)
                    return "Bearer " + ct, expire_at
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Control token from CCI failed, trying CCSP")

        # 2. CCSP REST endpoint (gspa-ccs-eu)
        ccsp_url = self.CCSP_SPA_API_URL + "vehicles/pin?token="
        ccsp_headers = {
            "Authorization": f"Bearer {token.ccs_token or ''}",
            "CCSP-Device-Id": token.client_device_id or token.device_id or "",
            "CCSP-Application-Id": self.CCSP_APPLICATION_ID,
            "Content-type": "application/json",
            "User-Agent": USER_AGENT_OK_HTTP,
        }
        ccsp_data = {
            "deviceId": token.client_device_id or token.device_id,
            "pin": token.pin,
        }

        try:
            response = requests.put(
                ccsp_url, json=ccsp_data, headers=ccsp_headers, timeout=30
            )
            response_data = response.json()
            if response_data.get("controlToken"):
                control_token = "Bearer " + response_data["controlToken"]
                expire_at = math.floor(
                    dt.datetime.now().timestamp() + response_data["expiresTime"]
                )
                return control_token, expire_at
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Control token from CCSP failed, trying CCAPI")

        # 3. CCAPI endpoint (prd.eu-ccapi) — legacy fallback
        ccapi_url = self.USER_API_URL + "pin?token="
        ccapi_headers = {
            "Authorization": token.access_token,
            "Content-type": "application/json",
            "User-Agent": USER_AGENT_OK_HTTP,
        }
        response = requests.put(
            ccapi_url, json=ccsp_data, headers=ccapi_headers, timeout=30
        )
        response_data = response.json()
        if response_data.get("controlToken") is None:
            _LOGGER.debug(f"{DOMAIN} - Get Control Token Response {response_data}")
            raise APIError("PIN verification failed, ensure PIN is entered correctly.")
        control_token = "Bearer " + response_data["controlToken"]
        expire_at = math.floor(
            dt.datetime.now().timestamp() + response_data["expiresTime"]
        )
        return control_token, expire_at

    # ------------------------------------------------------------------
    # Vehicles
    # ------------------------------------------------------------------

    def get_vehicles(self, token: Token) -> list[Vehicle]:
        # Try CCI API first
        url = self.CCI_DOMAIN_API_URL + "v1/vehicle/available-vehicles?detail=true"
        headers = self._get_cci_headers(token)

        try:
            response = requests.get(url, headers=headers, timeout=30)
            if response.status_code == 200:
                data = response.json()
                return self._parse_vehicles_from_cci(data)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - CCI get_vehicles failed, trying CCAPI")

        # Fallback: CCAPI
        url = self.SPA_API_URL + "vehicles"
        headers = self._get_authenticated_headers(token)
        response = requests.get(url, headers=headers, timeout=30)
        response = response.json()
        _check_response_for_errors(response)

        vehicles = []
        for entry in response["resMsg"]["vehicles"]:
            vehicle = Vehicle()
            vehicle.id = entry["vehicleId"]
            vehicle.VIN = entry.get("vin", "")
            vehicle.name = entry.get("nickname", "")
            vehicle.model = entry.get("modelName", "")
            vehicle.ccu_ccs2_protocol_support = entry.get(
                "ccuCCS2ProtocolSupport",
                entry.get("ccs2ProtocolSupport", 0),
            )

            fuel_type = entry.get("fuelType", entry.get("type", ""))
            if fuel_type in ("EV",):
                vehicle.engine_type = ENGINE_TYPES.EV
            elif fuel_type in ("PHEV", "HEV+PHEV"):
                vehicle.engine_type = ENGINE_TYPES.PHEV
            elif fuel_type in ("HEV",):
                vehicle.engine_type = ENGINE_TYPES.HEV
            else:
                vehicle.engine_type = ENGINE_TYPES.ICE

            vehicles.append(vehicle)

        return vehicles

    def _parse_vehicles_from_cci(self, data: dict) -> list[Vehicle]:
        vehicles = []
        vehicle_list = data if isinstance(data, list) else data.get("vehicles", [])
        if isinstance(vehicle_list, dict):
            vehicle_list = [vehicle_list]

        for entry in vehicle_list:
            vehicle = Vehicle()
            vehicle.id = entry.get("vin", entry.get("vehicleId", ""))
            vehicle.VIN = entry.get("vin", "")
            vehicle.name = entry.get(
                "nickname", entry.get("vehicleName", entry.get("modelName", ""))
            )
            vehicle.model = entry.get("modelName", entry.get("vehicleModelName", ""))
            vehicle.ccu_ccs2_protocol_support = entry.get(
                "ccs2ProtocolSupport", entry.get("ccu_ccs2_protocol_support", 0)
            )

            fuel_type = entry.get("fuelType", entry.get("engineFuelCode", ""))
            if fuel_type in ("EV",):
                vehicle.engine_type = ENGINE_TYPES.EV
            elif fuel_type in ("PHEV", "HEV+PHEV"):
                vehicle.engine_type = ENGINE_TYPES.PHEV
            elif fuel_type in ("HEV",):
                vehicle.engine_type = ENGINE_TYPES.HEV
            else:
                vehicle.engine_type = ENGINE_TYPES.ICE

            vehicle._cci_vehicle_data = entry  # noqa: SLF001

            vehicles.append(vehicle)

        return vehicles

    def update_vehicle_with_cached_state(self, token: Token, vehicle: Vehicle) -> None:
        vin = vehicle.VIN or vehicle.id

        # Try CCI API first if we have CCI tokens
        if token.ccs_token or token.exchangeable_token:
            if vehicle.ccu_ccs2_protocol_support:
                # CCS2 vehicles — use CCSP REST cached status
                url = self.CCSP_SPA_API_URL_V2 + f"vehicles/{vin}/ccs2/carstatus/latest"
                headers = self._get_ccsp_headers(
                    token, vehicle.ccu_ccs2_protocol_support
                )
                try:
                    response = requests.get(url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        data = response.json()
                        state = data.get("resMsg", data)
                        self._update_vehicle_properties_ccs2(vehicle, state)
                        vehicle.data = state
                        return
                except Exception:
                    _LOGGER.debug(f"{DOMAIN} - CCSP cached status failed")

                # Try CCI OneApp as second fallback
                vehicleinfo = None
                cci_data = getattr(vehicle, "_cci_vehicle_data", None)
                if cci_data:
                    vehicleinfo = self._build_vehicleinfo_header(cci_data)

                url = (
                    self.CCI_ONEAPP_API_URL
                    + f"v1/component/MAIN_VEHICLE_INFO/MAIN_VEHICLE_INFO?layoutId=HOME"
                    f"&ccId={token.client_device_id or token.device_id}_BLU"
                )
                headers = self._get_cci_headers(token, vehicleinfo)
                try:
                    response = requests.get(url, headers=headers, timeout=30)
                    if response.status_code == 200:
                        data = response.json()
                        self._update_vehicle_properties(vehicle, data)
                        return
                except Exception:
                    _LOGGER.debug(f"{DOMAIN} - CCI OneApp status failed")

        # Final fallback: CCAPI (old API)
        # Always use v1 /status/latest — v2 /ccs2/carstatus/latest
        # returns 403 without CCI tokens
        url = self.SPA_API_URL + "vehicles/" + vehicle.id + "/status/latest"
        headers = self._get_authenticated_headers(
            token, vehicle.ccu_ccs2_protocol_support
        )
        response = requests.get(url, headers=headers, timeout=30)
        if response.status_code != 200:
            raise APIError(f"CCAPI cached status failed: HTTP {response.status_code}")
        response_data = response.json()
        _check_response_for_errors(response_data)

        # v1 endpoint always returns legacy vehicleStatusInfo format
        state = response_data["resMsg"]["vehicleStatusInfo"]
        self._update_vehicle_properties(vehicle, state)

        self._set_cached_location_park(token, vehicle)

        if vehicle.engine_type in (ENGINE_TYPES.EV, ENGINE_TYPES.PHEV):
            try:
                state = self._get_driving_info(token, vehicle)
                self._update_vehicle_drive_info(vehicle, state)
            except Exception:
                _LOGGER.debug(f"{DOMAIN} - Driving info fetch failed")

    def force_refresh_vehicle_state(self, token: Token, vehicle: Vehicle) -> None:
        self._ensure_ccs_token(token)
        vin = vehicle.VIN or vehicle.id

        # Try CCSP REST with CCS token first
        if token.ccs_token:
            if vehicle.ccu_ccs2_protocol_support:
                url = self.CCSP_SPA_API_URL_V2 + f"vehicles/{vin}/ccs2/carstatus"
            else:
                url = self.CCSP_SPA_API_URL + f"vehicles/{vin}/status"
            headers = self._get_ccsp_headers(
                token, vehicle.ccu_ccs2_protocol_support or 0
            )

            try:
                response = requests.get(url, headers=headers, timeout=60)
                if response.status_code == 200:
                    data = response.json()
                    state = data.get("resMsg", data)
                    self._update_vehicle_properties_ccs2(vehicle, state)

                    # Get location
                    try:
                        loc_url = self.CCSP_SPA_API_URL + f"vehicles/{vin}/location"
                        loc_headers = self._get_ccsp_headers(token)
                        loc_response = requests.get(
                            loc_url, headers=loc_headers, timeout=30
                        )
                        if loc_response.status_code == 200:
                            loc_data = loc_response.json().get(
                                "resMsg", loc_response.json()
                            )
                            gps = loc_data.get("gpsDetail", loc_data.get("coord", {}))
                            if gps:
                                vehicle.location_latitude = gps.get(
                                    "lat", gps.get("latitude")
                                )
                                vehicle.location_longitude = gps.get(
                                    "lon", gps.get("longitude")
                                )
                                vehicle.location_heading = gps.get(
                                    "head", gps.get("heading", {}).get("value")
                                )
                    except Exception:
                        _LOGGER.debug(f"{DOMAIN} - CCI location fetch failed")
                    return
            except Exception:
                _LOGGER.debug(f"{DOMAIN} - CCSP force refresh failed, trying CCAPI")

        # Fallback: CCAPI (old API) — trigger refresh then read cached state
        headers = self._get_authenticated_headers(
            token, vehicle.ccu_ccs2_protocol_support or 0
        )

        # Trigger vehicle to refresh state from CCU
        trigger_url = self.SPA_API_URL + "vehicles/" + vehicle.id + "/status"
        try:
            trigger_resp = requests.get(trigger_url, headers=headers, timeout=60)
            trigger_data = trigger_resp.json()
            _check_response_for_errors(trigger_data)
        except Exception:
            _LOGGER.debug(
                f"{DOMAIN} - CCAPI force refresh trigger failed (may be duplicate)"
            )

        # Read updated state from cache — always use v1 endpoint
        # (v2 /ccs2/carstatus/latest returns 403 without CCI tokens)
        status_url = self.SPA_API_URL + "vehicles/" + vehicle.id + "/status/latest"
        response = requests.get(status_url, headers=headers, timeout=60)
        if response.status_code != 200:
            raise APIError(f"CCAPI cached status failed: HTTP {response.status_code}")
        response_data = response.json()
        _check_response_for_errors(response_data)

        # v1 endpoint always returns legacy vehicleStatusInfo format
        state = response_data["resMsg"]["vehicleStatusInfo"]
        self._update_vehicle_properties(vehicle, state)

        # Update location
        try:
            loc = self._get_location(token, vehicle)
            state["vehicleLocation"] = loc
            self._update_vehicle_properties(vehicle, state)
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Location fetch after force refresh failed")

    # ------------------------------------------------------------------
    # CCAPI legacy vehicle property mapping (for fallback)
    # ------------------------------------------------------------------

    def _get_legacy_delegate(self) -> KiaUvoApiEU:
        """Lazily create a KiaUvoApiEU delegate for legacy CCAPI property mapping."""
        if self._legacy_delegate is None:
            from .const import REGION_EUROPE

            self._legacy_delegate = KiaUvoApiEU(
                REGION_EUROPE, self.brand, self.LANGUAGE
            )
        return self._legacy_delegate

    def _update_vehicle_properties(self, vehicle: Vehicle, state: dict) -> None:
        """Legacy CCAPI vehicle property mapping — delegates to KiaUvoApiEU."""
        delegate = self._get_legacy_delegate()
        delegate._update_vehicle_properties(vehicle, state)

    def _set_cached_location_park(self, token: Token, vehicle: Vehicle) -> None:
        url = self.SPA_API_URL + "vehicles/" + vehicle.id + "/location/park"
        headers = self._get_authenticated_headers(token)
        try:
            response = requests.get(url, headers=headers, timeout=30).json()
            _check_response_for_errors(response)
            park = response.get("resMsg", {})
            if park:
                vehicle.location_latitude = park.get(
                    "lat", park.get("coord", {}).get("lat")
                )
                vehicle.location_longitude = park.get(
                    "lon", park.get("coord", {}).get("lon")
                )
                vehicle.location_heading = park.get(
                    "head", park.get("coord", {}).get("head")
                )
        except Exception:
            _LOGGER.debug(f"{DOMAIN} - Cached location park fetch failed")

    def _get_location(self, token: Token, vehicle: Vehicle) -> dict:
        url = self.SPA_API_URL + "vehicles/" + vehicle.id + "/location"
        headers = self._get_authenticated_headers(token)
        response = requests.get(url, headers=headers, timeout=30).json()
        _check_response_for_errors(response)
        return response.get("resMsg", response)

    def _get_driving_info(self, token: Token, vehicle: Vehicle) -> dict:
        url = self.SPA_API_URL + "vehicles/" + vehicle.id + "/drvinfo"
        headers = self._get_authenticated_headers(token)
        response = requests.get(url, headers=headers, timeout=30).json()
        _check_response_for_errors(response)
        return response.get("resMsg", response)

    def _update_vehicle_drive_info(self, vehicle: Vehicle, state: dict) -> None:
        if isinstance(state, dict):
            driving_info = state.get("drivingInfo", state)
            if isinstance(driving_info, list) and len(driving_info) > 0:
                driving_info = driving_info[0]
            vehicle.total_driving_range = (
                driving_info.get("totalDistance"),
                DISTANCE_UNITS.get(1, "km"),
            )

    # ------------------------------------------------------------------
    # CCS2 vehicle property mapping
    # ------------------------------------------------------------------

    def _update_vehicle_properties_ccs2(self, vehicle: Vehicle, state: dict) -> None:
        if get_child_value(state, "Offset"):
            offset = float(get_child_value(state, "Offset"))
            hours = int(offset)
            minutes = int((offset - hours) * 60)
            vehicle.timezone = dt.timezone(timedelta(hours=hours, minutes=minutes))
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
        vehicle.car_battery_percentage = get_child_value(
            state, "Electronics.Battery.Level"
        )
        vehicle.engine_is_running = get_child_value(state, "DrivingReady")

        air_temp = get_child_value(state, "Cabin.HVAC.Row1.Driver.Temperature.Value")
        if air_temp != "OFF":
            vehicle.air_temperature = (air_temp, TEMPERATURE_UNITS[1])

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
        vehicle.front_left_window_is_open = get_child_value(
            state, "Cabin.Window.Row1.Driver.Open"
        )
        vehicle.front_right_window_is_open = get_child_value(
            state, "Cabin.Window.Row1.Passenger.Open"
        )
        vehicle.back_left_window_is_open = get_child_value(
            state, "Cabin.Window.Row2.Left.Open"
        )
        vehicle.back_right_window_is_open = get_child_value(
            state, "Cabin.Window.Row2.Right.Open"
        )
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
        vehicle.trunk_is_open = get_child_value(state, "Body.Trunk.Open")

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
            state, "Green.ChargingInformation.ElectricCurrentLevel.State"
        )
        vehicle.ev_battery_is_plugged_in = get_child_value(
            state, "Green.ChargingInformation.ConnectorFastening.State"
        )
        charging_door_state = get_child_value(state, "Green.ChargingDoor.State")
        if charging_door_state in [0, 2]:
            vehicle.ev_charge_port_door_is_open = False
        elif charging_door_state == 1:
            vehicle.ev_charge_port_door_is_open = True

        vehicle.total_driving_range = (
            float(get_child_value(state, "Drivetrain.FuelSystem.DTE.Total")),
            DISTANCE_UNITS[get_child_value(state, "Drivetrain.FuelSystem.DTE.Unit")],
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
        vehicle.ev_first_departure_enabled = bool(
            get_child_value(state, "Green.Reservation.Departure.Schedule1.Enable")
        )
        vehicle.ev_second_departure_enabled = bool(
            get_child_value(state, "Green.Reservation.Departure.Schedule2.Enable")
        )

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
        vehicle.smart_key_battery_warning_is_on = bool(
            get_child_value(state, "Electronics.FOB.LowBattery")
        )

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

        vehicle.data = state

    # ------------------------------------------------------------------
    # Control commands (CCAPI primary, CCSP REST when CCS token available)
    # ------------------------------------------------------------------

    def _ensure_ccs_token(self, token: Token) -> None:
        if not token.ccs_token:
            self._exchange_ccs_token(token)

    def _control_command(
        self, token: Token, vehicle: Vehicle, endpoint: str, body: dict
    ) -> str:
        """Send a control command via CCAPI (primary) or CCSP REST (if CCS token)."""
        # Try CCSP REST first if we have a CCS token
        if token.ccs_token:
            try:
                return self._ccsp_control_command(token, vehicle, endpoint, body)
            except Exception:
                _LOGGER.debug(f"{DOMAIN} - CCSP control failed, falling back to CCAPI")

        # CCAPI path (same as KiaUvoApiEU / ApiImplType1)
        return self._ccapi_control_command(token, vehicle, endpoint, body)

    def _ccapi_control_command(
        self, token: Token, vehicle: Vehicle, endpoint: str, body: dict
    ) -> str:
        """Send control command via CCAPI (prd.eu-ccapi.hyundai.com)."""
        is_ccs2 = vehicle.ccu_ccs2_protocol_support != 0

        if is_ccs2:
            url = self.SPA_API_URL_V2 + f"vehicles/{vehicle.id}/ccs2/control/{endpoint}"
            headers = self._get_control_headers(token, vehicle)
        else:
            url = self.SPA_API_URL + f"vehicles/{vehicle.id}/control/{endpoint}"
            headers = self._get_authenticated_headers(
                token, vehicle.ccu_ccs2_protocol_support or 0
            )

        response = requests.post(url, headers=headers, json=body, timeout=30)
        response_data = response.json()
        _check_response_for_errors(response_data)

        # Regenerate device_id after each control command (CCAPI requires it)
        token.device_id = self._get_device_id(self._get_stamp())

        return response_data.get("msgId", "")

    def _ccsp_control_command(
        self, token: Token, vehicle: Vehicle, endpoint: str, body: dict
    ) -> str:
        """Send control command via CCSP REST (gspa-ccs-eu.hyundai.com)."""
        self._ensure_ccs_token(token)
        vin = vehicle.VIN or vehicle.id

        if vehicle.ccu_ccs2_protocol_support:
            url = self.CCSP_SPA_API_URL_V2 + f"vehicles/{vin}/ccs2/control/{endpoint}"
            headers = self._get_ccsp_control_headers(token, vehicle)
        else:
            url = self.CCSP_SPA_API_URL + f"vehicles/{vin}/control/{endpoint}"
            headers = self._get_ccsp_headers(
                token, vehicle.ccu_ccs2_protocol_support or 0
            )

        response = requests.post(url, headers=headers, json=body, timeout=30)
        self._check_ccsp_response(response)

        return response.json().get("msgId", "")

    def lock_action(
        self, token: Token, vehicle: Vehicle, action: VEHICLE_LOCK_ACTION
    ) -> str:
        if vehicle.ccu_ccs2_protocol_support:
            command = "close" if action == VEHICLE_LOCK_ACTION.LOCK else "open"
            body = {"command": command}
        else:
            action_str = "close" if action == VEHICLE_LOCK_ACTION.LOCK else "open"
            body = {
                "action": action_str,
                "deviceId": token.client_device_id or token.device_id,
            }
        return self._control_command(token, vehicle, "door", body)

    def start_climate(
        self, token: Token, vehicle: Vehicle, options: ClimateRequestOptions
    ) -> str:
        if vehicle.ccu_ccs2_protocol_support:
            body = {"command": "start"}
            if options.set_temp is not None:
                body["hvacTemp"] = options.set_temp
            if options.duration is not None:
                body["duration"] = options.duration
            if options.defrost is not None:
                body["defrost"] = options.defrost
        else:
            body = {
                "action": "start",
                "deviceId": token.client_device_id or token.device_id,
            }
            if options.set_temp is not None:
                body["airCtrlValue"] = options.set_temp
        return self._control_command(token, vehicle, "temperature", body)

    def stop_climate(self, token: Token, vehicle: Vehicle) -> str:
        if vehicle.ccu_ccs2_protocol_support:
            body = {"command": "stop"}
        else:
            body = {
                "action": "stop",
                "deviceId": token.client_device_id or token.device_id,
            }
        return self._control_command(token, vehicle, "temperature", body)

    def start_charge(self, token: Token, vehicle: Vehicle) -> str:
        if vehicle.ccu_ccs2_protocol_support:
            body = {"command": "start"}
        else:
            body = {
                "action": "start",
                "deviceId": token.client_device_id or token.device_id,
            }
        return self._control_command(token, vehicle, "charge", body)

    def stop_charge(self, token: Token, vehicle: Vehicle) -> str:
        if vehicle.ccu_ccs2_protocol_support:
            body = {"command": "stop"}
        else:
            body = {
                "action": "stop",
                "deviceId": token.client_device_id or token.device_id,
            }
        return self._control_command(token, vehicle, "charge", body)

    def charge_port_action(
        self, token: Token, vehicle: Vehicle, action: CHARGE_PORT_ACTION
    ) -> str:
        command = "open" if action == CHARGE_PORT_ACTION.OPEN else "close"
        return self._control_command(token, vehicle, "portdoor", {"action": command})

    def set_windows_state(
        self, token: Token, vehicle: Vehicle, options: WindowRequestOptions
    ) -> str:
        if not self.supports_window_control:
            raise APIError("Window control not supported")
        body = {}
        for field in ("front_left", "front_right", "back_left", "back_right"):
            val = getattr(options, field, None)
            body[field] = val.value if val is not None else 0
        return self._control_command(token, vehicle, "windowcurtain", body)

    def start_hazard_lights(self, token: Token, vehicle: Vehicle) -> str:
        return self._control_command(token, vehicle, "light", {"command": "on"})

    def start_hazard_lights_and_horn(self, token: Token, vehicle: Vehicle) -> str:
        return self._control_command(token, vehicle, "hornlight", {"command": "on"})

    def valet_mode_action(
        self, token: Token, vehicle: Vehicle, action: VALET_MODE_ACTION
    ) -> str:
        action_str = (
            "activate" if action == VALET_MODE_ACTION.ACTIVATE else "deactivate"
        )
        return self._control_command(token, vehicle, "valet", {"action": action_str})

    def check_action_status(
        self,
        token: Token,
        vehicle: Vehicle,
        action_id: str,
        synchronous: bool = False,
        timeout: int = 0,
    ) -> ORDER_STATUS:
        return ORDER_STATUS.UNKNOWN

    # ------------------------------------------------------------------
    # Error handling
    # ------------------------------------------------------------------

    def _check_cci_response(self, response: requests.Response) -> None:
        if response.status_code == 401:
            raise AuthenticationError("CCI API: Token expired or invalid")
        if response.status_code == 403:
            raise AuthenticationError("CCI API: Access denied")
        if response.status_code >= 400:
            raise APIError(f"CCI API error: HTTP {response.status_code}")

    def _check_ccsp_response(self, response: requests.Response) -> None:
        if response.status_code == 401:
            raise AuthenticationError("CCSP: Token expired or invalid")
        if response.status_code >= 400:
            try:
                data = response.json()
                ret_code = data.get("retCode")
                res_code = data.get("resCode")
                res_msg = data.get("resMsg", "")
                if ret_code != "S":
                    raise APIError(f"CCSP error: resCode={res_code} {res_msg}")
            except ValueError, KeyError:
                raise APIError(f"CCSP error: HTTP {response.status_code}")


def _check_response_for_errors(response: dict) -> None:
    """Check CCAPI-style response for errors (retCode/resCode format)."""
    if isinstance(response, dict):
        ret_code = response.get("retCode")
        if ret_code and ret_code != "S":
            res_code = response.get("resCode", "")
            res_msg = response.get("resMsg", "")
            # 4004 = Duplicate request — not fatal, just a retry indicator
            if str(res_code) == "4004":
                _LOGGER.debug(f"{DOMAIN} - Duplicate request (resCode=4004), ignoring")
                return
            raise APIError(
                f"API error: retCode={ret_code} resCode={res_code} {res_msg}"
            )
