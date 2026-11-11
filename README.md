# Xiaomi Bootloader Unlock Request (AQLR)

Runs parallel workers against Xiaomi's unlock API around **00:00 Beijing time (UTC+8)** with configurable timing offsets. The default is four workers.

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
3. Count down to midnight Beijing time, logging a new timestamped line each second
4. Arm the workers alongside the countdown, with HTTP clients initialized before their send times. The default first-request offsets are 1400, 900, 400, 100ms before midnight.
5. Log request-attempt timestamps and retry as needed until 30 seconds after midnight (approval, rejection or quota exhaustion can stop workers earlier).

### Options for `run`
```bash
uv run mi-unlock run --dry-run    # Test everything except the actual HTTP POST
uv run mi-unlock run --plain      # Disable the live worker table; countdown always logs new lines
uv run mi-unlock run --token-file /path/to/tokens.json
uv run mi-unlock run --offsets-ms="1400,900,400,100"
```

### Request timing and worker count

`--offsets-ms` accepts comma-separated milliseconds **before** midnight Beijing.
Each value creates one worker. The sign convention is:

| Offset | Scheduled first request, Beijing time |
|--------|--------------------------------------|
| `500` | 23:59:59.500 (500ms **before** midnight) |
| `0` | 00:00:00.000 |
| `-500` | 00:00:00.500 (500ms **after** midnight) |

For example, six workers, all before midnight:

```bash
uv run mi-unlock run --offsets-ms="2000,1400,900,500,250,100"
```

Or five workers spanning midnight:

```bash
uv run mi-unlock run --offsets-ms="1400,900,400,0,-500"
```

Offsets must be finite numbers, greater than `-30000` (the retry deadline)
and less than `86400000` (24 hours before midnight). Decimal values are accepted.
Workers alternate between the two configured tokens; additional workers do not
require additional tokens. Each worker may retry, so the number of workers is
not a limit on the total request count.

The countdown runs independently and cannot hold up the workers. All workers
use the same midnight target. Scheduling controls local request dispatch;
OS scheduling and network latency affect the actual timing and server arrival.

The same setting is available through `MI_UNLOCK_OFFSETS_MS`; an explicit
`--offsets-ms` argument takes precedence. For the installed user service, use
`systemctl --user edit xiaomi-unlock.service` and add:

```ini
[Service]
Environment="MI_UNLOCK_OFFSETS_MS=1400,900,400,100"
```

After saving, apply the new setting:

```bash
systemctl --user daemon-reload
systemctl --user restart xiaomi-unlock.service
journalctl --user -u xiaomi-unlock.service -n 30 --no-pager
```

Startup logs show the offsets, worker count and exact scheduled times. Updating
the service environment does not change its daily start timer.

## Download (Pre-built Binaries)

No Python needed — download the binary for your platform from the [Releases page](https://github.com/masrurimz/xiaomi-unlock-request/releases/latest).

| Platform | File |
|----------|------|
| 🐧 Linux (x86_64) | `mi-unlock-linux-x86_64` |
| 🍎 macOS (Apple Silicon / Intel via Rosetta) | `mi-unlock-macos-arm64` |
| 🪟 Windows (x86_64) | `mi-unlock-windows-x86_64.exe` |
| 🤖 Android / Termux | `mi-unlock-termux-android.whl` |

**Linux / macOS:**
```bash
chmod +x mi-unlock-*
./mi-unlock-linux-x86_64 --help
```

**Windows:** double-click or run `mi-unlock-windows-x86_64.exe --help` in PowerShell.

**Android / Termux:**
```bash
pkg install python
pip install mi-unlock-termux-android.whl
mi-unlock --help
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
