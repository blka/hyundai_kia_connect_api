# `diagnostic_dump.py`

A diagnostic dump tool for the Hyundai/Kia/Genesis Connect API.
It saves raw responses from every read-only endpoint used by the library, the parsed vehicle state, and API metadata.

## What it's for

- Debugging `kia_uvo` / `hyundai_kia_connect_api` issues.
- Checking which features the vehicle actually exposes through the API.
- Comparing vehicle configuration against its real-time state.
- Providing data for upstream bug reports.

## Where output is saved

All output goes to a `diagnostics/` directory next to the script, inside a timestamped sub-directory:

```text
scripts/diagnostics/2026-06-29_142327_eu_hyundai/
├── api_capabilities.json
├── raw_request_0001_post.json      # every HTTP call made by the library
├── raw_request_0002_get.json
├── ...
├── raw_vehicle_{id}_status.json    # /vehicles/{id}/status/latest or /ccs2/carstatus/latest
├── raw_vehicle_{id}_location.json
├── raw_vehicle_{id}_location_park.json
├── raw_vehicle_{id}_drivinginfo.json
├── raw_vehicle_{id}_profile.json # /vehicles/{id}/profile (ApiImplType1 regions only)
└── vehicle_{id}.json               # full parsed Vehicle object
```

The access token cache (only written with `--save-token`) lives **outside** this folder, in `scripts/tokens/token_{region}_{brand}.json`, so it is not picked up when the `diagnostics/` folder is zipped or shared.

## Security warning

By default the script **redacts** the highest-risk fields before writing any file:

- `Authorization` / `Set-Cookie` / `Stamp` / `CCSP-Stamp` request and response headers (the access token lives here)
- `access_token`, `refresh_token`, `device_id`, `sid`, `rmtoken`, `sessionId` body keys
- GPS coordinates (`lat`, `lon`, `latitude`, `longitude`, `coord`, `gpsLatitude`, `gpsLongitude`, `_location_latitude`, `_location_longitude`)

These are replaced with `<REDACTED>` so the dump is safe to share without leaking credentials or location. The access token is **not written to disk at all** unless you pass `--save-token` (and even then it goes to `scripts/tokens/`, outside the shared `diagnostics/` folder).

**The dump still contains data you must review manually before sharing:**

- VIN
- `vehicle.id`, `registration_date`, `username`, e-mail / user ID
- door, window, climate, fuel, battery, tire pressure status
- registration date / license plate (if provided by the API)

Before attaching to a GitHub issue, forum post, or sending to anyone:

1. Remove or mask `VIN`, `vehicle.id`, `registration_date`, `username`, e-mail.
2. Make sure the file name itself does not contain the vehicle ID.

If you are debugging auth and need the raw token/headers, pass `--no-redact` — but **never share** output produced with that flag.

## Requirements

- Python 3.10+
- `hyundai_kia_connect_api` installed in editable mode:

```bash
cd /path/to/hyundai_kia_connect_api
pip install -e .
```

## `.env` setup

Create a `.env` file in any directory, for example:

```bash
# tests/integration/.env
CC_USERNAME=your@email.com
CC_PASSWORD=your_password
CC_PIN=1234
CC_REGION=EU
CC_BRAND=hyundai
```

Supported regions:

- `EU`, `USA`, `CA`, `AU`, `NZ`, `IN`, `CN`, `BR`

Supported brands:

- `hyundai`, `kia`, `genesis`

`CC_PIN` is only required for some regions.

## Running the script

From the `hyundai_kia_connect_api` directory:

```bash
python scripts/diagnostic_dump.py --env-file tests/integration/.env
```

If `.env` is in the same directory you run the script from:

```bash
cd scripts
python diagnostic_dump.py
```

You can also use environment variables:

```bash
export CC_USERNAME=your@email.com
export CC_PASSWORD=your_password
export CC_PIN=1234
export CC_REGION=EU
export CC_BRAND=hyundai

python scripts/diagnostic_dump.py
```

Or pass values as CLI flags (highest precedence):

```bash
python scripts/diagnostic_dump.py \
  --region eu \
  --brand hyundai \
  --username your@email.com \
  --password your_password
```

Precedence: **CLI flags > environment variables > `.env` file**.

## OTP

For **USA** and **CA**, the script prompts for an OTP channel (`E` for e-mail, `S` for SMS), sends the code, and asks you to type it in. The token is cached only if you pass `--save-token`, so later runs on the same device can skip OTP.

## What the script does and does not do

### It does

- Log in to the API.
- Refresh the token if needed.
- Fetch the vehicle list.
- For each vehicle, fetch the cached state via `update_vehicle_with_cached_state`.
- On `ApiImplType1` regions, also call `profile`, `location/park`, `location`, and `drivinginfo`.
- Record every HTTP call made by the library.

### It does not

- **Send any command to the vehicle** (no lock/unlock, no climate start, no force refresh). The script is read-only.
- Redact **everything** — credentials and GPS coordinates are redacted by default, but VIN, `vehicle.id`, `registration_date`, username, and e-mail are left in place. Review and mask those manually before sharing (see Security warning).
- Cache the access token unless you pass `--save-token`.

## Full example session

```bash
# 1. Clone your fork (or the main repository)
git clone git@github.com:YOUR_FORK/hyundai_kia_connect_api.git
cd hyundai_kia_connect_api

# 2. Install in editable mode
pip install -e .

# 3. Create a .env file
cat > .env <<EOF
CC_USERNAME=your@email.com
CC_PASSWORD=your_password
CC_PIN=1234
CC_REGION=EU
CC_BRAND=hyundai
EOF

# 4. Run the script
python scripts/diagnostic_dump.py --env-file .env

# 5. Inspect the output
ls -la scripts/diagnostics/
```

## Sanitizing data before sharing — example

Credentials (`access_token`, `refresh_token`, `device_id`, `sid`, `rmtoken`, `Authorization`/`Set-Cookie`/`Stamp` headers) and GPS coordinates are already redacted by default. After running the script, manually edit the JSON files (or use a redaction tool) to clear the remaining identifying fields:

```text
VIN
id
key
username
registration_date
e-mail
```

Share only the response structure and non-identifying values, e.g.:

```json
{
    "body": {
        "resMsg": {
            "vinInfo": [
                {
                    "basic": {
                        "modelName": "SANTA FE",
                        "modelYear": "2026",
                        "brand": "H",
                        "country": "pl"
                    }
                }
            ]
        }
    }
}
```

## Known limitations

- `location` and `drivinginfo` may return errors (timeout, duplicate request, 404) depending on the model and vehicle state. This is expected — the library only calls them under specific conditions.
- `/profile` is only available on `ApiImplType1` regions: EU, AU, IN, CN.
