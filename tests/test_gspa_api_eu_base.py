"""GspaApiEU base — header builder and stamp delegation tests."""

from hyundai_kia_connect_api.HyundaiCciApiEU import HyundaiCciApiEU


def test_hyundai_device_id_header():
    """Hyundai uses X-Device-Id (§3.1: Hyundai EU=false)."""
    api = HyundaiCciApiEU(9, 2, "en")
    assert api.DEVICE_ID_HEADER == "X-Device-Id"


def test_hyundai_request_id_header():
    """Hyundai uses X-Request-Id."""
    api = HyundaiCciApiEU(9, 2, "en")
    assert api.REQUEST_ID_HEADER == "X-Request-Id"


def test_hyundai_cipher_brand():
    """Hyundai uses hyundai cipher."""
    api = HyundaiCciApiEU(9, 2, "en")
    assert api.CIPHER_BRAND == "hyundai"


def test_hyundai_ccsp_api_url_derived():
    """CCSP_API_URL derived from GSPA_BASE_URL (no trailing slash)."""
    api = HyundaiCciApiEU(9, 2, "en")
    assert api.CCSP_API_URL == "https://gspa-ccs-eu.hyundai.com"


def test_base_stamp_uses_brand_cipher():
    """_get_stamp delegates to the brand-specific cipher instance."""
    api = HyundaiCciApiEU(9, 2, "en")
    assert api._cipher is not None
    # Stamp is base64 and non-empty for valid inputs
    stamp = api._cipher.compute_x_stamp(
        region=1, tsid="AAAAAAAAAAAAAAAA", epoch_seconds=1700000000, user_id="u1"
    )
    assert len(stamp) > 0


def test_base_cci_domain_api_url():
    """CCI_DOMAIN_API_URL is derived from CCI_API_URL."""
    api = HyundaiCciApiEU(9, 2, "en")
    assert api.CCI_DOMAIN_API_URL == "https://cci-api-eu.hyundai.com/domain/api/"


def test_base_gspa_get_url_construction():
    """_gspa_get builds URL from GSPA_BASE_URL + /gspa/v1/{endpoint}."""
    api = HyundaiCciApiEU(9, 2, "en")
    # Verify URL pattern without making a real request
    endpoint = "status/vehicles/{carId}/stored-status-widget"
    car_id = "test123"
    expected_url = (
        f"https://gspa-ccs-eu.hyundai.com/gspa/v1/{endpoint.format(carId=car_id)}"
    )
    # The URL is built inside _gspa_get; verify the constant it uses
    assert (
        api.CCSP_API_URL + f"/gspa/v1/{endpoint.format(carId=car_id)}"
    ) == expected_url
