# HypurrSeeker

Minimal Telegram bot for monitoring Hyperliquid Perps positions. Users can subscribe and monitor up to 5 wallet addresses each.

## Quick Start

1. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

2. **Configure:**
   ```bash
   cp .env.example .env
   ```
   Edit `.env` and add your `TELEGRAM_BOT_TOKEN` (from @BotFather)

3. **Run:**
   ```bash
   python hypurrseeker.py
   ```

4. **Use the bot:**
   - Send `/sub` to subscribe to alerts
   - Send `/wallet` and provide an EVM address to monitor
   - Add up to 5 wallets per user

## How It Works

- Users subscribe via `/sub` - a default wallet is automatically added
- Users can add their own wallet addresses via `/wallet`
- Bot polls Hyperliquid API every 20 minutes for all user wallets
- Compares positions with previous snapshots
- Sends personalized alerts when any token changes by >5%
- Stores data in CSV files (`data/` directory)

## Bot Commands

- `/start` - Show welcome message and help
- `/sub` - Subscribe to alerts
- `/wallet` - Add a wallet address to monitor (max 5)
- `/cancel` - Cancel wallet addition

## Configuration

See `.env.example` for all available options. Key settings:

- `TELEGRAM_BOT_TOKEN` - Required: Your bot token
- `DEFAULT_WALLET_ADDRESS` - Auto-added for new subscribers (default: 0xb317...)
- `POLL_INTERVAL_MIN` - Minutes between checks (default: 20)
- `CHANGE_THRESHOLD_PCT` - Alert threshold % (default: 5.0)
- `MAX_WALLETS_PER_USER` - Max wallets per user (default: 5)
- `COMPARE_ABS` - Use absolute values (default: true)

## Project Structure

```
hypurrseeker.py      # Single-file bot (all logic)
.env                 # Environment variables
data/
  snapshots.csv      # Position history (per user/wallet)
  subscribers.csv    # Telegram subscribers
  wallets.csv        # User wallet addresses
```

## Features

- Default wallet auto-added on subscription - users start monitoring immediately
- Multi-user support - each user monitors their own wallets
- Automatic wallet limit - oldest wallet removed when adding 6th
- Personalized alerts - users only get alerts for their wallets
- EVM address validation
- Conversation-based wallet addition

## Development

See `CLAUDE.md` for detailed architecture and development guide.
