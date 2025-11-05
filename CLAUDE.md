# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HypurrSeeker is a minimal Telegram bot that monitors Hyperliquid Perps positions. Users can subscribe and add up to 5 wallet addresses to monitor. The bot polls the Hyperliquid Info API every 20 minutes, compares positions with saved snapshots, and sends personalized alerts when any token's position changes by more than 5%.

**Architecture:** Single-file Python application (`hypurrseeker.py`) with CSV-based storage for simplicity and maintainability.

## Commands

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment (copy and edit)
cp .env.example .env
# Edit .env to add your TELEGRAM_BOT_TOKEN
```

### Running
```bash
# Run the bot (blocking, runs indefinitely)
python hypurrseeker.py

# Run in background (Windows)
start /B python hypurrseeker.py

# Run in background (Linux/Mac with nohup)
nohup python hypurrseeker.py &

# Run in background (tmux)
tmux new -s hypurrseeker
python hypurrseeker.py
# Detach with Ctrl+B, D
```

### Bot Usage
Users interact with the bot via Telegram:
```
/start   - Show welcome message and help
/sub     - Subscribe to alerts
/wallet  - Add a wallet address to monitor (max 5 per user)
/cancel  - Cancel wallet addition
```

### Development
```bash
# Test API connection manually (replace with actual address)
curl -s https://api.hyperliquid.xyz/info \
  -H 'Content-Type: application/json' \
  -d '{"type":"clearinghouseState","user":"0xb317d2bc2d3d2df5fa441b5bae0ab9d8b07283ae","dex":""}'

# Check CSV files
cat data/snapshots.csv
cat data/subscribers.csv
cat data/wallets.csv
```

## Architecture

### Single-File Design
All logic is contained in `hypurrseeker.py`:
- **API client:** Fetches Perps data via Hyperliquid Info API (`clearinghouseState` endpoint)
- **Storage layer:** CSV-based persistence for snapshots, subscribers, and wallets
- **Wallet management:** Add/remove wallets with 5-wallet limit per user
- **Diff logic:** Compares token amounts between runs, calculates percentage changes
- **Telegram bot:** Handles `/sub` and `/wallet` commands with conversation handler
- **Scheduler:** Async loop running every 20 minutes with random jitter (0-60s)

### Data Flow
1. **Retrieve** all active user-wallet pairs from `data/wallets.csv`
2. **For each pair:**
   - **Fetch** current positions from Hyperliquid API for the wallet address
   - **Load** most recent snapshot for that user-wallet pair from `data/snapshots.csv`
   - **Compare** positions: calculate percentage change using absolute values
   - **Alert** if any token changes exceed `CHANGE_THRESHOLD_PCT` (default: 5%)
   - **Send** personalized alert to that specific user
   - **Append** new snapshot to CSV with user_id, address, and timestamp
   - **Sleep** 1 second between API calls to avoid rate limiting
3. **Sleep** for `POLL_INTERVAL_MIN` minutes plus random jitter (0-60s)
4. **Repeat**

### Key Functions (hypurrseeker.py)

**API Client:**
- `fetch_positions(address)` - Calls Hyperliquid API, returns `Dict[symbol -> Decimal(amount)]`
  - Includes retry logic with exponential backoff (3 attempts)
  - Handles HTTP 429 rate limiting and 5xx server errors
  - Normalizes symbols to uppercase
  - Located at hypurrseeker.py:56

**Storage - Snapshots:**
- `load_latest_snapshot(user_id, address)` - Reads last snapshot for a specific user-wallet pair
  - Filters by user_id and address
  - Returns positions dict with latest timestamp
  - Located at hypurrseeker.py:138

- `append_snapshot(user_id, address, positions, timestamp)` - Appends positions to CSV
  - Includes user_id and address for multi-user support
  - Located at hypurrseeker.py:174

**Storage - Subscribers:**
- `load_subscribers()` - Returns list of active subscriber user IDs
  - Located at hypurrseeker.py:210

- `add_subscriber(user_id, username)` - Adds new subscriber to CSV
  - Checks for duplicates
  - Located at hypurrseeker.py:227

**Storage - Wallets:**
- `validate_evm_address(address)` - Validates EVM address format (0x + 40 hex chars)
  - Located at hypurrseeker.py:243

- `get_user_wallets(user_id)` - Returns list of (address, added_at) tuples for a user
  - Sorted chronologically by added_at
  - Located at hypurrseeker.py:264

- `add_wallet(user_id, address)` - Adds wallet for user, removes oldest if at max (5)
  - Returns (success, removed_address) tuple
  - Normalizes address to lowercase
  - Marks oldest as inactive if user has MAX_WALLETS_PER_USER
  - Located at hypurrseeker.py:289

- `get_all_user_wallet_pairs()` - Returns all (user_id, address) pairs for monitoring
  - Filters to active subscribers and active wallets only
  - Located at hypurrseeker.py:362

**Diff & Alert Logic:**
- `detect_changes(prev, curr, threshold_pct, compare_abs)` - Identifies position changes
  - Uses absolute values by default (`COMPARE_ABS=true`)
  - Formula: `pct = (abs(curr) - abs(prev)) / abs(prev) * 100`
  - Returns list of `(token, prev_amount, curr_amount, pct_change)` tuples
  - Located at hypurrseeker.py:390

- `render_alert_message(address, changes, timestamp)` - Formats alert for Telegram
  - Includes abbreviated wallet address (first 6 + last 4 chars)
  - Located at hypurrseeker.py:462

**Telegram Bot:**
- `cmd_start(update, context)` - Handles `/start` command
  - Shows welcome message and instructions
  - Located at hypurrseeker.py:504

- `cmd_sub(update, context)` - Handles `/sub` command
  - Subscribes user to alerts
  - Auto-adds DEFAULT_WALLET_ADDRESS if user has no wallets
  - Located at hypurrseeker.py:527

- `cmd_wallet_start(update, context)` - Handles `/wallet` command
  - Shows current wallets
  - Starts conversation to add new wallet
  - Located at hypurrseeker.py:568

- `cmd_wallet_address(update, context)` - Handles wallet address input
  - Validates address format
  - Adds wallet and notifies if oldest was removed
  - Located at hypurrseeker.py:597

- `cmd_wallet_cancel(update, context)` - Handles `/cancel` command
  - Cancels wallet addition conversation
  - Located at hypurrseeker.py:636

- `send_alert(app, user_id, message)` - Sends alert to specific user
  - Located at hypurrseeker.py:642

**Monitoring Loop:**
- `job_once(app)` - Single monitoring cycle
  - Iterates through all user-wallet pairs
  - Orchestrates fetch → compare → alert → save for each pair
  - Located at hypurrseeker.py:661

- `monitoring_loop(app)` - Infinite async loop
  - Calls `job_once()` every `POLL_INTERVAL_MIN` minutes
  - Adds 0-60s random jitter to prevent API timing patterns
  - Located at hypurrseeker.py:711

### CSV Storage

**data/subscribers.csv:**
- Columns: `user_id`, `username`, `subscribed_at`, `active`
- Append-only when adding subscribers
- No unsubscribe logic implemented yet

**data/wallets.csv:**
- Columns: `user_id`, `address`, `added_at`, `active`
- One row per user-wallet pair
- When user reaches max wallets (5), oldest is marked `active=false`
- Address normalized to lowercase

**data/snapshots.csv:**
- Columns: `timestamp`, `user_id`, `address`, `token`, `amount`
- Append-only (one row per token per user-wallet-timestamp)
- No cleanup logic (grows indefinitely)

### Hyperliquid API

**Endpoint:** `POST https://api.hyperliquid.xyz/info`

**Request:**
```json
{
  "type": "clearinghouseState",
  "user": "<EVM_address>",
  "dex": ""
}
```

**Response fields used:**
- `assetPositions[].position.coin` - Token symbol
- `assetPositions[].position.szi` - Position size (signed: + for long, - for short)

**Rate limiting:**
- 20-minute polling cadence is well below API limits
- 1-second delay between consecutive wallet API calls
- Retries use exponential backoff (1s → 2s → 4s)

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | - | Bot token from @BotFather |
| `DEFAULT_WALLET_ADDRESS` | No | `0xb317d2bc2d3d2df5fa441b5bae0ab9d8b07283ae` | Auto-added for new subscribers |
| `POLL_INTERVAL_MIN` | No | 20 | Minutes between monitoring cycles |
| `CHANGE_THRESHOLD_PCT` | No | 5.0 | Alert threshold (%) |
| `API_BASE` | No | `https://api.hyperliquid.xyz/info` | API endpoint |
| `COMPARE_ABS` | No | true | Use absolute values for comparison |
| `MAX_WALLETS_PER_USER` | No | 5 | Maximum wallets per user |

## Alert Logic

**Comparison mode:** By default, compares absolute position sizes (ignoring long/short direction).

**Trigger conditions:**
- Percentage change exceeds threshold: `abs(pct_change) > CHANGE_THRESHOLD_PCT`
- New positions (0 → non-zero) treated as 100% change
- Closed positions (non-zero → 0) treated as -100% change

**Alert format:**
```
[HypurrSeeker]
Wallet: 0xb317...3ae

BTC: 1.23 → 1.37 (+11.4%)
ETH: 12.34 → 11.10 (-10.1%)

(2025-11-04 09:20 KST)
```

**Personalization:** Each user only receives alerts for their own wallets.

## User Workflow

1. User sends `/start` to bot → receives welcome message with instructions
2. User sends `/sub` to bot → subscribed to alerts
3. If user has no wallets, default wallet (`DEFAULT_WALLET_ADDRESS`) is automatically added
4. User sends `/wallet` → bot asks for address
5. User sends EVM address (0x...) → wallet added
6. User repeats steps 4-5 for up to 5 wallets total
7. If user adds 6th wallet → oldest is automatically removed and user is notified
8. Bot monitors all wallets every 20 minutes
9. User receives personalized alerts for their wallets only

## Error Handling

- **Missing TELEGRAM_BOT_TOKEN:** Logs error and exits immediately in `main()`
- **API failures:** Exponential backoff (1s → 2s → 4s), max 3 retries, then skip that wallet
- **Invalid address format:** Bot prompts user to re-enter valid address
- **Schema drift:** Warns and skips individual malformed positions (via try/except in parsing)
- **Alert send failures:** Logs per-user errors but continues with other users
- **CSV errors:** Will crash (no error handling) - assumes single process, append-only writes

## Known Limitations

- **No unsubscribe:** Users cannot opt out via bot command
- **No wallet removal:** Users cannot manually remove specific wallets (only auto-removal of oldest)
- **No wallet list command:** No dedicated command to view all wallets (shown when adding new wallet)
- **No snapshot cleanup:** CSV grows indefinitely
- **Single process:** No concurrency protection on CSV files
- **No persistence layer:** Uses CSV instead of database
- **Manual deployment:** No systemd service file or deployment automation
- **No direction tracking:** Does not flag sign flips (long ↔ short) explicitly
- **Fixed timezone:** Alerts show "KST" but uses system time
- **Wallet limit enforcement:** Uses read-modify-write pattern which could fail under concurrent access

## Future Extensions

The minimal CSV + alert architecture can be extended to:
1. Add `/wallets` command to list all user's wallets with full addresses
2. Add `/remove <address>` command to manually remove specific wallets
3. Add `/unsub` command to unsubscribe
4. Implement database (SQLite/PostgreSQL) for better concurrent access
5. Add per-wallet or per-token custom thresholds
6. Monitor other data sources by adding new fetch functions
7. Add historical position charts
8. Implement webhook-based monitoring instead of polling
