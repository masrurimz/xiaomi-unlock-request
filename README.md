# Xiaomi Bootloader Unlock Request (AQLR)

Sends 4 parallel HTTP requests to Xiaomi's unlock API at exactly **00:00 Beijing time (UTC+8)** with staggered timing offsets to beat the daily quota reset.

Based on the [XDA AQLR method](https://xdaforums.com/t/how-to-unlock-bootloader-on-xiaomi-hyperos-all-devices-except-cn.4654009).

## Setup

```bash
# Install deps (one-time)
uv sync
```

## Get Tokens

You need 2 session tokens from 2 different browsers, both logged into the **same Mi Account** on [Mi Community Global](https://c.mi.com/global).

Run the interactive setup wizard:

```bash
uv run mi-unlock setup
```

Or manually create `tokens.json`:

```json
{
  "firefox": "<new_bbs_serviceToken from Firefox cookies>",
  "chrome": "<popRunToken from Chrome cookies>"
}
```

### Token 1: Firefox (`new_bbs_serviceToken`)
1. Open Firefox → https://c.mi.com/global → Login
2. Press **F12** → **Storage** → **Cookies** → `c.mi.com`
3. Find `new_bbs_serviceToken` → copy the **Value**

### Token 2: Chrome (`popRunToken`)
1. Open Chrome → https://c.mi.com/global → Login (same account)
2. Press **F12** → **Application** → **Cookies** → `https://c.mi.com`
3. Find `popRunToken` → copy the **Value**

⚠️ Tokens expire — get fresh ones right before running the script each night.

## Commands

### Check eligibility
```bash
uv run mi-unlock status
```

### Run the unlock request
```bash
uv run mi-unlock run
```

The script will:
1. Check your account eligibility
2. Sync time via NTP servers
3. Count down to midnight Beijing time
4. Fire 4 parallel requests at offsets: 1400, 900, 400, 100ms before midnight
5. Retry in a tight loop for 30 seconds after midnight

### Options for `run`
```bash
uv run mi-unlock run --dry-run    # Test everything except the actual HTTP POST
uv run mi-unlock run --plain      # Line-based output (no Rich Live display)
uv run mi-unlock run --token-file /path/to/tokens.json
```

## Development

```bash
# Run tests
uv run pytest

# Run with coverage
uv run pytest --cov=src/xiaomi_unlock
```

## After Approval

Once the unlock request is approved:
1. **Sign out** of Mi Account on your phone
2. **Restart** the phone
3. **Sign in** to Mi Account
4. Go to **Developer Options → Mi Unlock Status → Link Account**
5. Use **Mi Unlock Tool** on PC (72-hour waiting period applies)
