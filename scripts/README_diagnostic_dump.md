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
├── vehicle_{id}.json               # full parsed Vehicle object
└── token_{region}_{brand}.json     # cached token
```

## Security warning

**The generated files contain sensitive data:**

- VIN
- precise GPS location (coordinates + timestamp)
- access tokens and refresh tokens
- device IDs, session IDs
- e-mail / user ID
- door, window, climate, fuel, battery, tire pressure status
- registration date / license plate (if provided by the API)

**Do not share these files before sanitizing.** Before attaching them to a GitHub issue, forum post, or sending them to anyone:

1. Remove or mask `VIN`, `vehicle.id`, `registration_date`, `username`, e-mail.
2. Shift or mask GPS coordinates (`_location_latitude`, `_location_longitude`).
3. Remove `access_token`, `refresh_token`, `device_id`, `sid`, `rmtoken`.
4. Make sure the file name itself does not contain the vehicle ID.

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

For **USA** and **CA**, the script prompts for an OTP channel (`E` for e-mail, `S` for SMS), sends the code, and asks you to type it in. The token is cached afterwards, so later runs on the same device usually skip OTP.

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
- Automatically redact sensitive data — you must do that before sharing.

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

After running the script, manually edit the JSON files or use a redaction tool. Fields you typically want to clear:

```text
VIN
id
key
username
access_token
refresh_token
device_id
sid
rmtoken
registration_date
_location_latitude
_location_longitude
_location_last_set_time
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
