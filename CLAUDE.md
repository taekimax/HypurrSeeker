# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

HypurrSeeker is a minimal Telegram bot that monitors Hyperliquid Perps positions. Users can subscribe and add up to 5 wallet addresses to monitor. The bot checks positions at scheduled times (1, 21, and 41 minutes past each hour), compares with saved snapshots, and sends personalized alerts when any token's position changes by more than 5%.

**Architecture:** Single-file Python application (`hypurrseeker.py`) with CSV-based storage for simplicity and maintainability.

## Commands

### Setup
```bash
# Install dependencies
pip install -r requirements.txt

# Configure environment (copy and edit)
cp .env.example .env
# Edit .env to add your TELEGRAM_BOT_TOKEN

# Note: CSV files are auto-created on first run with correct schema
# No need to manually create data/ or CSV files
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
/unsub   - Unsubscribe from alerts
/wallet  - Add or remove wallet addresses (max 5 per user)
           • Reply with address (0x...) to ADD
           • Reply with number (1-5) to REMOVE
/cancel  - Cancel wallet operation
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
- **Telegram bot:** Handles `/sub`, `/unsub`, and `/wallet` commands with conversation handler
- **Scheduler:** Async loop running at scheduled times (1, 21, 41 minutes past hour) with ±30s jitter

### Multi-Token Architecture

**Key Design Principle:** Users subscribe to **wallet addresses**, not individual tokens. Each wallet can contain multiple token positions (BTC, ETH, etc.).

- **wallets.csv** tracks wallet addresses only (one row per user-wallet pair)
- **snapshots.csv** stores one row per token per wallet (multiple rows for multi-token wallets)
- API fetches all tokens for a wallet in a single call
- Change detection compares all tokens and includes all changes in one alert message
- When users sub/unsub or add/remove wallets, the `followers_count` is updated for ALL tokens in that wallet

### Data Flow (Optimized Wallet-Based Monitoring)

1. **Wait** until next scheduled time (1, 21, or 41 minutes past the hour, ±30s jitter)
2. **Retrieve** unique wallet addresses with `followers_count > 0` from `data/snapshots.csv`
3. **For each unique wallet:**
   - **Fetch** current positions from Hyperliquid API (returns ALL tokens in wallet)
   - **Load** previous snapshot for wallet from `data/snapshots.csv` (ALL tokens)
   - **Compare** ALL tokens: calculate percentage change using absolute values
   - **If changes detected:**
     - **Update** snapshot once for the wallet (all tokens, preserving followers_count)
     - **Get** all active followers (users) who monitor this wallet
     - **Send** personalized alert to each follower with ALL changed tokens in one message
   - **Sleep** 1 second between API calls to avoid rate limiting
4. **Repeat** from step 1

**Efficiency:** Each wallet is fetched once per cycle regardless of how many users follow it. Alerts are then fanned out to all followers.

### Key Functions (hypurrseeker.py)

**API Client:**
- `fetch_positions(address)` - Calls Hyperliquid API, returns `Dict[symbol -> (size, value_usd)]`
  - Fetches ALL tokens in wallet at once (both size and USD value)
  - Returns tuple: (position_size, position_value_usd) per token
  - Includes retry logic with exponential backoff (3 attempts)
  - Handles HTTP 429 rate limiting and 5xx server errors
  - Normalizes symbols to uppercase
  - Located at hypurrseeker.py:77

**Storage - Initialization:**
- `initialize_csv_files()` - Auto-creates CSV files with proper headers if they don't exist
  - Called on bot startup in main()
  - Creates subscribers.csv, wallets.csv, snapshots.csv with correct 6-field schema
  - Enables fresh setup without manual file creation
  - Located at hypurrseeker.py:150

**Storage - Snapshots:**
- `load_wallet_snapshot(address)` - Reads snapshot for a wallet (ALL tokens)
  - Filters by address only (wallet-centric, not user-specific)
  - Returns (positions dict mapping token -> (size, value_usd), timestamp) tuple
  - Requires proper 6-field CSV schema (no backward compatibility)
  - Located at hypurrseeker.py:180

- `update_wallet_snapshot(address, positions, timestamp)` - Updates snapshot for wallet
  - Replaces old snapshot with new one (all tokens with sizes and USD values)
  - Preserves followers_count for the wallet
  - Writes to CSV with fieldnames: address, followers_count, timestamp, token, amount, value_usd
  - Located at hypurrseeker.py:213

- `get_monitored_wallets()` - Returns unique wallet addresses with followers_count > 0
  - Used to determine which wallets to monitor
  - Includes wallets with placeholder entries (ensures new wallets are monitored immediately)
  - Located at hypurrseeker.py:660

- `get_active_wallet_followers(address)` - Returns user_ids who actively follow this wallet
  - Used to fan out alerts to all followers
  - Located at hypurrseeker.py:665

- `increment_wallet_followers(address)` - Increments followers_count for ALL tokens in wallet
  - Called when user subscribes or adds wallet
  - Uses 6-field CSV schema: address, followers_count, timestamp, token, amount, value_usd
  - For new wallets: creates placeholder entry with token="_PLACEHOLDER_" and followers_count=1
  - Placeholder is replaced with real data on first monitoring cycle
  - Located at hypurrseeker.py:572

- `decrement_wallet_followers(address)` - Decrements followers_count for ALL tokens in wallet
  - Called when user unsubscribes or removes wallet
  - Uses 6-field CSV schema (consistent with increment function)
  - Never goes below 0
  - Located at hypurrseeker.py:626

**Storage - Subscribers:**
- `load_subscribers()` - Returns list of active subscriber user IDs
  - Located at hypurrseeker.py:241

- `add_subscriber(user_id, username)` - Adds new subscriber or reactivates inactive subscriber
  - Returns True if newly subscribed or reactivated, False if already active
  - Located at hypurrseeker.py:282

- `remove_subscriber(user_id)` - Unsubscribes user by setting active to false
  - Preserves data for potential re-subscription
  - Located at hypurrseeker.py:343

**Storage - Wallets:**
- `validate_evm_address(address)` - Validates EVM address format (0x + 40 hex chars)
  - Located at hypurrseeker.py:381

- `get_user_wallets(user_id)` - Returns list of (address, added_at) tuples for a user
  - Only returns active wallets
  - Sorted chronologically by added_at
  - Located at hypurrseeker.py:402

- `add_wallet(user_id, address)` - Adds wallet for user, removes oldest if at max (5)
  - Returns (success, removed_address) tuple
  - Normalizes address to lowercase
  - Marks oldest as inactive if user has MAX_WALLETS_PER_USER
  - Calls increment_wallet_followers() after successful add
  - Located at hypurrseeker.py:427

- `remove_wallet(user_id, address)` - Removes wallet by setting it to inactive
  - Calls decrement_wallet_followers() after successful removal
  - Located at hypurrseeker.py:500

**Diff & Alert Logic:**
- `detect_changes(prev, curr, threshold_pct, compare_abs, min_value_usd)` - Identifies position changes for ALL tokens
  - Processes union of all tokens: `set(prev.keys()) | set(curr.keys())`
  - **$10k USD Filter**: Ignores positions where BOTH prev AND curr values < $10,000
  - **Edge case handling**: Alerts if EITHER prev >= $10k (closing) OR curr >= $10k (opening)
  - Uses absolute values by default (`COMPARE_ABS=true`)
  - Formula: `pct = (abs(curr) - abs(prev)) / abs(prev) * 100`
  - Returns list of `(token, prev_amount, curr_amount, prev_value_usd, curr_value_usd, pct_change)` tuples
  - Located at hypurrseeker.py:697

- `render_alert_message(address, changes, prev_timestamp, curr_timestamp)` - Formats alert for Telegram
  - Includes ALL changed tokens in one message
  - Shows position sizes AND USD values for each change
  - Shows elapsed time since previous snapshot
  - Includes abbreviated wallet address (first 6 + last 4 chars)
  - Located at hypurrseeker.py:758

**Telegram Bot:**
- `cmd_start(update, context)` - Handles `/start` command
  - Shows welcome message and instructions
  - Located at hypurrseeker.py:818

- `cmd_sub(update, context)` - Handles `/sub` command
  - Subscribes user to alerts (or reactivates inactive subscriber)
  - Auto-adds DEFAULT_WALLET_ADDRESS if new user has no wallets
  - Calls increment_wallet_followers() for all user's wallets (creates placeholders for new wallets)
  - Located at hypurrseeker.py:836

- `cmd_unsub(update, context)` - Handles `/unsub` command
  - Unsubscribes user (sets active=false)
  - Calls decrement_wallet_followers() for all user's wallets
  - Preserves wallet data for potential re-subscription
  - Located at hypurrseeker.py:1013

- `cmd_wallet_start(update, context)` - Handles `/wallet` command
  - Shows current wallets (numbered list)
  - Starts conversation to add or remove wallet
  - Located at hypurrseeker.py:894

- `cmd_wallet_address(update, context)` - Handles wallet address or number input
  - If input is number (1-5): removes that wallet
  - If input is address (0x...): adds wallet
  - Validates address format
  - Notifies if oldest was removed when at max
  - Manages followers_count updates (creates placeholder for new wallets)
  - Located at hypurrseeker.py:926

- `cmd_wallet_cancel(update, context)` - Handles `/cancel` command
  - Cancels wallet addition/removal conversation
  - Located at hypurrseeker.py:1007

- `send_alert(app, user_id, message)` - Sends alert to specific user
  - Located at hypurrseeker.py:1037

**Monitoring Loop:**
- `get_next_scheduled_time()` - Calculates next monitoring time
  - Returns next scheduled time (1, 21, or 41 minutes past the hour)
  - Handles hour rollover and midnight crossing
  - Located at hypurrseeker.py:1131

- `job_once(app)` - Single monitoring cycle
  - Gets unique monitored wallets (followers_count > 0, including placeholders)
  - For each wallet: fetch → compare ALL tokens → alert all followers → update snapshot once
  - Placeholder entries are replaced with real position data on first fetch
  - Optimized: each wallet fetched once regardless of follower count
  - Located at hypurrseeker.py:1056

- `monitoring_loop(app)` - Infinite async loop with scheduled runs
  - Calculates next scheduled time and sleeps until then
  - Runs at 1, 21, and 41 minutes past each hour
  - Adds ±30 second jitter to be "generous" with timing
  - Calls `job_once()` at each scheduled time
  - Located at hypurrseeker.py:1161

### CSV Storage

**data/subscribers.csv:**
- Columns: `user_id`, `username`, `subscribed_at`, `active`
- Track user subscription status
- Users can unsubscribe (active=false) and re-subscribe (active=true)

**data/wallets.csv:**
- Columns: `user_id`, `address`, `added_at`, `active`
- **One row per user-wallet pair** (NOT per token)
- Tracks which users follow which wallets
- When user reaches max wallets (5), oldest is marked `active=false`
- Address normalized to lowercase

**data/snapshots.csv:**
- Columns: `address`, `followers_count`, `timestamp`, `token`, `amount`, `value_usd`
- **One row per token per wallet** (wallet-centric, not user-specific)
- Example: If wallet has BTC and ETH, there are 2 rows with same address
- `value_usd` stores the position value in USD (from Hyperliquid API `positionValue` field)
- `followers_count` tracks how many active users follow this wallet
- Snapshot is replaced (not appended) on each update to keep only latest state
- When user subs/adds wallet: all tokens in wallet get `followers_count++`
- When user unsubs/removes wallet: all tokens in wallet get `followers_count--`
- Wallets with `followers_count > 0` are monitored
- **New wallet handling:** When a wallet is added for the first time, a placeholder entry is created with token="_PLACEHOLDER_", amount=0, value_usd=0, and followers_count=1. This ensures the wallet appears in `get_monitored_wallets()` and is fetched on the next monitoring cycle. The placeholder is replaced with actual position data after the first API fetch.

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
- `assetPositions[].position.positionValue` - Position value in USD (absolute value)

**Rate limiting:**
- Scheduled monitoring (1, 21, 41 minutes past hour) is well below API limits
- ~20 minutes between checks with ±30 second jitter
- 1-second delay between consecutive wallet API calls
- Retries use exponential backoff (1s → 2s → 4s)

## Configuration

Environment variables (see `.env.example`):

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | - | Bot token from @BotFather |
| `DEFAULT_WALLET_ADDRESS` | No | `0xb317d2bc2d3d2df5fa441b5bae0ab9d8b07283ae` | Auto-added for new subscribers |
| `CHANGE_THRESHOLD_PCT` | No | 5.0 | Alert threshold (%) |
| `MIN_POSITION_VALUE_USD` | No | 10000 | Minimum USD value to trigger alerts (filters small positions) |
| `API_BASE` | No | `https://api.hyperliquid.xyz/info` | API endpoint |
| `COMPARE_ABS` | No | true | Use absolute values for comparison |
| `MAX_WALLETS_PER_USER` | No | 5 | Maximum wallets per user |

**Note:** `POLL_INTERVAL_MIN` environment variable is no longer used. Monitoring runs at fixed times: 1, 21, and 41 minutes past each hour.

## Alert Logic

**Comparison mode:** By default, compares absolute position sizes (ignoring long/short direction).

**$10,000 USD Position Value Filter:**
- **Core Rule**: Positions are ignored if BOTH previous AND current values < $10,000 USD
- **Edge Cases Handled**:
  - ✓ New large position: `$0 → $15,000` - **ALERTS** (opening significant position)
  - ✓ Closing large position: `$15,000 → $0` - **ALERTS** (closing significant position)
  - ✓ Growing to large: `$5,000 → $15,000` - **ALERTS** (becoming significant)
  - ✓ Shrinking from large: `$15,000 → $5,000` - **ALERTS** (was significant)
  - ✗ Small fluctuation: `$5,000 → $6,000` - **NO ALERT** (never significant)
  - ✗ New small position: `$0 → $5,000` - **NO ALERT** (insignificant size)
- Prevents spam from dust positions and low-value token movements
- Configurable via `MIN_POSITION_VALUE_USD` environment variable

**Multi-Token Handling:**
- All tokens in a wallet are checked in a single monitoring cycle
- Alert includes ALL tokens that exceed threshold AND pass the $10k filter
- Example: If wallet has BTC ($50M, +6%) and ETH ($8k, +10%), only BTC alert is sent

**Trigger conditions:**
- Position value >= $10,000 (previous OR current)
- Percentage change exceeds threshold: `abs(pct_change) > CHANGE_THRESHOLD_PCT`
- New positions (0 → non-zero) treated as 100% change
- Closed positions (non-zero → 0) treated as -100% change
- Detection compares union of all tokens: `set(prev_tokens) | set(curr_tokens)`

**Alert format:**
```
[HypurrSeeker]
Wallet: 0xb317...3ae
Changed after 21m

BTC: 600.0 → 620.0 (+3.3%)
  Value: $60,955,800 → $63,000,000
ETH: 13000.0 → 12000.0 (-7.7%)
  Value: $42,681,600 → $39,420,000

Previous: 2025-11-05 09:00
Current:  2025-11-05 09:21
```

**Personalization:** Each user only receives alerts for their own wallets. Multiple users following the same wallet each receive the same alert independently.

## User Workflow

### Initial Setup
1. User sends `/start` to bot → receives welcome message with instructions
2. User sends `/sub` to bot → subscribed to alerts
3. If new user has no wallets, default wallet (`DEFAULT_WALLET_ADDRESS`) is automatically added
4. User sends `/wallet` → bot shows current wallets and prompts for action
5. User sends EVM address (0x...) → wallet added (all tokens in wallet will be monitored)
6. User repeats steps 4-5 for up to 5 wallets total
7. If user adds 6th wallet → oldest is automatically removed and user is notified

### Ongoing Monitoring
8. Bot monitors all unique wallets (that have followers_count > 0) at scheduled times (1, 21, 41 minutes past hour)
9. For each wallet, bot fetches ALL tokens and compares with previous snapshot
10. If any token changes exceed 5%, alert is sent to ALL users following that wallet
11. Alert includes ALL changed tokens in one message
12. Next monitoring happens at the next scheduled time (~20 minutes later)

### Removal and Unsubscribe
- User sends `/wallet` → replies with number (1-5) → removes that specific wallet
- User sends `/unsub` → unsubscribes but wallet data is preserved
- User can `/sub` again later to reactivate with same wallets

## Error Handling

- **Missing TELEGRAM_BOT_TOKEN:** Logs error and exits immediately in `main()`
- **API failures:** Exponential backoff (1s → 2s → 4s), max 3 retries, then skip that wallet
- **Invalid address format:** Bot prompts user to re-enter valid address
- **Schema drift:** Warns and skips individual malformed positions (via try/except in parsing)
- **Alert send failures:** Logs per-user errors but continues with other users
- **CSV errors:** Will crash (no error handling) - assumes single process, append-only writes
- **CSV schema consistency:** All CSV write operations use consistent 6-field schema for snapshots.csv to prevent data corruption (fixed in commit 084c6dd)

## Known Limitations

- **Snapshot persistence:** Only latest snapshot kept per wallet (no historical data)
- **Single process:** No concurrency protection on CSV files
- **No persistence layer:** Uses CSV instead of database
- **Manual deployment:** No systemd service file or deployment automation
- **No direction tracking:** Does not flag sign flips (long ↔ short) explicitly
- **System timezone:** Timestamps use local system time
- **CSV read-modify-write:** Pattern could fail under concurrent access
- **No per-token customization:** Cannot set different thresholds for different tokens
- **No historical charts:** Cannot view position history over time
- **Fixed $10k threshold:** Position value filter is global, not per-user or per-token customizable

## Recent Bug Fixes

### v2024-11-05b: Fresh Start Simplification (Commit ba712dc)
Removed backward compatibility code and added auto-initialization for cleaner codebase:

1. **Removed Backward Compatibility**
   - **Change:** Removed code that handled old CSV format without `value_usd` column
   - **Reason:** Backward compatibility code was causing data corruption and hard to maintain
   - **Impact:** Fresh start approach - requires production to start with clean CSV files

2. **Added Auto-Initialization**
   - **Feature:** Bot now auto-creates CSV files with proper headers on startup
   - **Implementation:** New `initialize_csv_files()` function called in `main()`
   - **Benefit:** No manual CSV file creation needed - bot handles everything

3. **Simplified `load_wallet_snapshot()`**
   - **Change:** Removed `.get("value_usd", "0")` fallback - now requires proper schema
   - **Benefit:** Cleaner code, easier to debug, predictable behavior

**For Production Deployment:**
- Stop bot, backup and delete existing CSV files
- Restart bot - it will create fresh CSV files with correct schema
- Users can start fresh with `/sub` and `/wallet` commands

### v2024-11-05a: CSV Schema Bugs (Commit 084c6dd)
Fixed three critical bugs that prevented `/sub` and `/wallet` commands from working:

1. **CSV Schema Mismatch in `increment_wallet_followers()`**
   - **Issue:** Function was using 5-field schema instead of 6 (missing `value_usd` field)
   - **Impact:** Entire `value_usd` column was dropped from CSV when users subscribed or added wallets, corrupting the data structure
   - **Fix:** Updated to use consistent 6-field schema: `[address, followers_count, timestamp, token, amount, value_usd]`

2. **CSV Schema Mismatch in `decrement_wallet_followers()`**
   - **Issue:** Same schema mismatch (missing `value_usd` field)
   - **Impact:** Would corrupt CSV when users unsubscribed or removed wallets
   - **Fix:** Updated to use consistent 6-field schema

3. **New Wallets Not Added to Snapshots**
   - **Issue:** When a wallet was added for the first time, only a log message was written but no CSV entry was created
   - **Impact:** New wallets would silently fail to be monitored because they didn't appear in `get_monitored_wallets()`
   - **Fix:** Now creates a placeholder entry with `token="_PLACEHOLDER_"`, `amount=0`, `value_usd=0`, and `followers_count=1`. The placeholder is replaced with real position data on the first monitoring cycle.

**Result:** Users can now properly subscribe and add multiple wallet addresses. All CSV operations use consistent schema to prevent data corruption.

## Future Extensions

The minimal CSV + alert architecture can be extended to:
1. ~~Add `/unsub` command to unsubscribe~~ ✓ Implemented
2. ~~Add wallet removal functionality~~ ✓ Implemented via `/wallet` command
3. ~~Add USD value tracking and filtering~~ ✓ Implemented ($10k threshold)
4. ~~Fix CSV schema consistency bugs~~ ✓ Fixed (commit 084c6dd)
5. ~~Add auto-initialization of CSV files~~ ✓ Implemented (commit ba712dc)
6. Implement database (SQLite/PostgreSQL) for better concurrent access
7. Add per-wallet or per-token custom thresholds (% and $ thresholds)
8. Add per-user customization of `MIN_POSITION_VALUE_USD` threshold
9. Monitor other data sources by adding new fetch functions
10. Add historical position charts (currently only latest snapshot is kept)
11. Implement webhook-based monitoring instead of polling
12. Add direction flip alerts (long ↔ short transitions)
13. Add `/wallets` command with full addresses and status details
14. Export position history to CSV or charts
