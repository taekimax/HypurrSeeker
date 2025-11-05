#!/usr/bin/env python3
"""
HypurrSeeker - Telegram bot for monitoring Hyperliquid Perps positions

Users can subscribe and add up to 5 wallet addresses to monitor. The bot polls
the Hyperliquid Info API every 20 minutes, compares results with saved CSV
snapshots, and sends personalized alerts when any token's absolute position
changes by more than 5%.
"""

import asyncio
import csv
import logging
import os
import random
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import httpx
from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

# Load environment variables
load_dotenv()

# Configuration
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
DEFAULT_WALLET_ADDRESS = os.getenv("DEFAULT_WALLET_ADDRESS", "0xb317d2bc2d3d2df5fa441b5bae0ab9d8b07283ae")
POLL_INTERVAL_MIN = int(os.getenv("POLL_INTERVAL_MIN", "20"))
CHANGE_THRESHOLD_PCT = float(os.getenv("CHANGE_THRESHOLD_PCT", "5.0"))
API_BASE = os.getenv("API_BASE", "https://api.hyperliquid.xyz/info")
COMPARE_ABS = os.getenv("COMPARE_ABS", "true").lower() == "true"
MAX_WALLETS_PER_USER = int(os.getenv("MAX_WALLETS_PER_USER", "5"))

# File paths
DATA_DIR = Path("data")
SNAPSHOTS_FILE = DATA_DIR / "snapshots.csv"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.csv"
WALLETS_FILE = DATA_DIR / "wallets.csv"

# Conversation states
WAITING_FOR_ADDRESS = 1

# Ensure data directory exists
DATA_DIR.mkdir(exist_ok=True)

# Logging setup
logging.basicConfig(
    level=logging.WARNING,  # Set default to WARNING
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s"
)

# Set our app to INFO level
logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

# Mute noisy libraries
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.WARNING)
logging.getLogger("telegram.ext").setLevel(logging.WARNING)


# ============================================================================
# API Client
# ============================================================================

async def fetch_positions(address: str) -> Dict[str, Decimal]:
    """
    Fetch Perps positions from Hyperliquid Info API.

    Args:
        address: EVM address to query

    Returns:
        Dictionary mapping token symbol to position size (signed decimal)
    """
    payload = {
        "type": "clearinghouseState",
        "user": address,
        "dex": ""
    }

    headers = {"Content-Type": "application/json"}

    # Retry logic with exponential backoff
    max_retries = 3
    for attempt in range(max_retries):
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(API_BASE, json=payload, headers=headers)

                if response.status_code == 429:
                    wait_time = 2 ** attempt
                    logger.warning(f"Rate limited, waiting {wait_time}s before retry")
                    await asyncio.sleep(wait_time)
                    continue

                response.raise_for_status()
                data = response.json()

                # Parse positions
                positions = {}
                asset_positions = data.get("assetPositions", [])

                for asset in asset_positions:
                    try:
                        coin = asset["position"]["coin"].upper()
                        szi = asset["position"]["szi"]
                        positions[coin] = Decimal(str(szi))
                    except (KeyError, TypeError, ValueError) as e:
                        logger.warning(f"Failed to parse position: {e}")
                        continue

                logger.info(f"Fetched {len(positions)} positions for {address}")
                return positions

        except httpx.HTTPStatusError as e:
            if e.response.status_code >= 500:
                wait_time = 2 ** attempt
                logger.warning(f"Server error {e.response.status_code}, waiting {wait_time}s")
                await asyncio.sleep(wait_time)
                continue
            logger.error(f"HTTP error: {e}")
            raise
        except Exception as e:
            logger.error(f"Failed to fetch positions: {e}")
            if attempt == max_retries - 1:
                raise
            await asyncio.sleep(2 ** attempt)

    return {}


# ============================================================================
# Storage - CSV Operations
# ============================================================================

def load_latest_snapshot(user_id: int, address: str) -> Dict[str, Decimal]:
    """
    Load the most recent snapshot from CSV for a specific user and address.

    Args:
        user_id: Telegram user ID
        address: Wallet address

    Returns:
        Dictionary mapping token symbol to position size
    """
    if not SNAPSHOTS_FILE.exists():
        logger.info("No snapshot file found, returning empty snapshot")
        return {}

    address = address.lower()
    positions = {}
    latest_timestamp = None

    with open(SNAPSHOTS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (int(row["user_id"]) == user_id and
                row["address"].lower() == address):
                row_timestamp = row["timestamp"]
                # Only keep positions from the latest timestamp
                if latest_timestamp is None or row_timestamp > latest_timestamp:
                    latest_timestamp = row_timestamp
                    positions = {row["token"]: Decimal(row["amount"])}
                elif row_timestamp == latest_timestamp:
                    positions[row["token"]] = Decimal(row["amount"])

    logger.info(f"Loaded {len(positions)} positions from latest snapshot for {address}")
    return positions


def append_snapshot(
    user_id: int,
    address: str,
    positions: Dict[str, Decimal],
    timestamp: datetime
):
    """
    Append current positions to snapshot CSV.

    Args:
        user_id: Telegram user ID
        address: Wallet address
        positions: Dictionary of token -> amount
        timestamp: Current timestamp
    """
    file_exists = SNAPSHOTS_FILE.exists()

    with open(SNAPSHOTS_FILE, "a", newline="") as f:
        fieldnames = ["timestamp", "user_id", "address", "token", "amount"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        for token, amount in positions.items():
            writer.writerow({
                "timestamp": timestamp.isoformat(),
                "user_id": user_id,
                "address": address.lower(),
                "token": token,
                "amount": str(amount)
            })

    logger.info(f"Appended snapshot with {len(positions)} positions for {address}")


def load_subscribers() -> List[int]:
    """
    Load active subscriber user IDs.

    Returns:
        List of Telegram user IDs
    """
    if not SUBSCRIBERS_FILE.exists():
        return []

    subscribers = []
    with open(SUBSCRIBERS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["active"].lower() == "true":
                subscribers.append(int(row["user_id"]))

    return subscribers


def add_subscriber(user_id: int, username: str):
    """
    Add a new subscriber to CSV.

    Args:
        user_id: Telegram user ID
        username: Telegram username
    """
    file_exists = SUBSCRIBERS_FILE.exists()

    # Check if already subscribed
    if file_exists:
        with open(SUBSCRIBERS_FILE, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row["user_id"]) == user_id and row["active"].lower() == "true":
                    logger.info(f"User {user_id} already subscribed")
                    return False

    with open(SUBSCRIBERS_FILE, "a", newline="") as f:
        fieldnames = ["user_id", "username", "subscribed_at", "active"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerow({
            "user_id": user_id,
            "username": username or "",
            "subscribed_at": datetime.now().isoformat(),
            "active": "true"
        })

    logger.info(f"Added subscriber {user_id} ({username})")
    return True


def validate_evm_address(address: str) -> bool:
    """
    Validate EVM address format.

    Args:
        address: Address string to validate

    Returns:
        True if valid, False otherwise
    """
    if not address.startswith("0x"):
        return False
    if len(address) != 42:  # 0x + 40 hex chars
        return False
    try:
        int(address[2:], 16)  # Check if hex
        return True
    except ValueError:
        return False


def get_user_wallets(user_id: int) -> List[Tuple[str, str]]:
    """
    Get all active wallets for a user.

    Args:
        user_id: Telegram user ID

    Returns:
        List of (address, added_at) tuples, sorted by added_at
    """
    if not WALLETS_FILE.exists():
        return []

    wallets = []
    with open(WALLETS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["user_id"]) == user_id and row["active"].lower() == "true":
                wallets.append((row["address"], row["added_at"]))

    # Sort by added_at to get chronological order
    wallets.sort(key=lambda x: x[1])
    return wallets


def add_wallet(user_id: int, address: str) -> Tuple[bool, Optional[str]]:
    """
    Add a wallet for a user. If user has MAX_WALLETS_PER_USER, remove oldest.

    Args:
        user_id: Telegram user ID
        address: EVM address to add

    Returns:
        (success, removed_address) - removed_address is set if an old wallet was removed
    """
    address = address.lower()  # Normalize to lowercase

    # Validate address format
    if not validate_evm_address(address):
        return False, None

    # Get current wallets
    current_wallets = get_user_wallets(user_id)

    # Check if already exists
    for wallet_addr, _ in current_wallets:
        if wallet_addr.lower() == address:
            logger.info(f"Wallet {address} already exists for user {user_id}")
            return False, None

    removed_address = None

    # If at max capacity, mark oldest as inactive
    if len(current_wallets) >= MAX_WALLETS_PER_USER:
        oldest_address = current_wallets[0][0]
        removed_address = oldest_address

        # Read all rows and mark the oldest as inactive
        if WALLETS_FILE.exists():
            rows = []
            with open(WALLETS_FILE, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    if (int(row["user_id"]) == user_id and
                        row["address"].lower() == oldest_address.lower()):
                        row["active"] = "false"
                    rows.append(row)

            # Write back
            with open(WALLETS_FILE, "w", newline="") as f:
                fieldnames = ["user_id", "address", "added_at", "active"]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)

            logger.info(f"Removed oldest wallet {oldest_address} for user {user_id}")

    # Add new wallet
    file_exists = WALLETS_FILE.exists()
    with open(WALLETS_FILE, "a", newline="") as f:
        fieldnames = ["user_id", "address", "added_at", "active"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)

        if not file_exists:
            writer.writeheader()

        writer.writerow({
            "user_id": user_id,
            "address": address,
            "added_at": datetime.now().isoformat(),
            "active": "true"
        })

    logger.info(f"Added wallet {address} for user {user_id}")
    return True, removed_address


def get_all_user_wallet_pairs() -> List[Tuple[int, str]]:
    """
    Get all (user_id, address) pairs for active users and wallets.

    Returns:
        List of (user_id, address) tuples
    """
    if not WALLETS_FILE.exists():
        return []

    # Get active subscribers
    active_subscribers = set(load_subscribers())

    pairs = []
    with open(WALLETS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            user_id = int(row["user_id"])
            if user_id in active_subscribers and row["active"].lower() == "true":
                pairs.append((user_id, row["address"]))

    return pairs


# ============================================================================
# Diff & Alert Logic
# ============================================================================

def detect_changes(
    prev: Dict[str, Decimal],
    curr: Dict[str, Decimal],
    threshold_pct: float,
    compare_abs: bool = True
) -> List[Tuple[str, Decimal, Decimal, float]]:
    """
    Detect position changes above threshold.

    Args:
        prev: Previous positions
        curr: Current positions
        threshold_pct: Alert threshold percentage
        compare_abs: Whether to compare absolute values

    Returns:
        List of (token, prev_amount, curr_amount, pct_change)
    """
    changes = []
    all_tokens = set(prev.keys()) | set(curr.keys())

    for token in all_tokens:
        prev_amount = prev.get(token, Decimal(0))
        curr_amount = curr.get(token, Decimal(0))

        if prev_amount == curr_amount:
            continue

        # Use absolute values if configured
        prev_val = abs(prev_amount) if compare_abs else prev_amount
        curr_val = abs(curr_amount) if compare_abs else curr_amount

        # Calculate percentage change
        if prev_val == 0:
            if curr_val != 0:
                pct_change = 100.0  # New position
            else:
                continue
        else:
            pct_change = float((curr_val - prev_val) / prev_val * 100)

        # Check threshold
        if abs(pct_change) > threshold_pct:
            changes.append((token, prev_amount, curr_amount, pct_change))

    return changes


def render_alert_message(
    address: str,
    changes: List[Tuple[str, Decimal, Decimal, float]],
    timestamp: datetime
) -> str:
    """
    Render alert message for Telegram.

    Args:
        address: Wallet address
        changes: List of detected changes
        timestamp: Current timestamp

    Returns:
        Formatted message string
    """
    lines = ["[HypurrSeeker]", f"Wallet: {address[:6]}...{address[-4:]}", ""]

    for token, prev, curr, pct in changes:
        sign = "+" if pct > 0 else ""
        lines.append(f"{token}: {prev} → {curr} ({sign}{pct:.1f}%)")

    lines.append("")
    lines.append(f"({timestamp.strftime('%Y-%m-%d %H:%M')} KST)")

    return "\n".join(lines)


# ============================================================================
# Telegram Bot
# ============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show welcome message."""
    await update.message.reply_text(
        "🐱 Welcome to HypurrSeeker!\n\n"
        "Monitor Hyperliquid Perps positions and receive instant alerts when any token position changes by more than 5%.\n\n"
        "📋 Commands:\n"
        "/sub - Subscribe to alerts\n"
        "/wallet - Add a wallet to monitor (max 5)\n"
        "/cancel - Cancel wallet addition\n\n"
        "🚀 Quick Start:\n"
        "1. Send /sub to subscribe\n"
        "2. A default wallet will be added automatically\n"
        "3. Add your own wallets with /wallet\n"
        "4. Receive alerts every 20 minutes!\n\n"
        "💡 Features:\n"
        "• Monitor up to 5 wallets per user\n"
        "• Real-time position change alerts\n"
        "• Automatic oldest wallet removal when adding 6th\n"
        "• Personalized alerts for your wallets only\n\n"
        "Need help? Just send /sub to get started!"
    )


async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /sub command to subscribe to alerts."""
    user_id = update.effective_user.id
    username = update.effective_user.username

    is_new_subscriber = add_subscriber(user_id, username)

    # If new subscriber and no wallets, add default wallet
    if is_new_subscriber:
        existing_wallets = get_user_wallets(user_id)
        if not existing_wallets and DEFAULT_WALLET_ADDRESS:
            success, _ = add_wallet(user_id, DEFAULT_WALLET_ADDRESS)
            if success:
                await update.message.reply_text(
                    "✓ You've been subscribed to HypurrSeeker alerts!\n\n"
                    f"Default wallet added: {DEFAULT_WALLET_ADDRESS[:6]}...{DEFAULT_WALLET_ADDRESS[-4:]}\n\n"
                    "Add more wallet addresses using /wallet"
                )
            else:
                await update.message.reply_text(
                    "✓ You've been subscribed to HypurrSeeker alerts!\n\n"
                    "Add wallet addresses to monitor using /wallet"
                )
        else:
            await update.message.reply_text(
                "✓ You've been subscribed to HypurrSeeker alerts!\n\n"
                "Add wallet addresses to monitor using /wallet"
            )
    else:
        wallets = get_user_wallets(user_id)
        if wallets:
            await update.message.reply_text(
                f"You're already subscribed with {len(wallets)} wallet(s)!\n\n"
                "Add more wallets using /wallet"
            )
        else:
            await update.message.reply_text(
                "You're already subscribed!\n\n"
                "Add wallet addresses to monitor using /wallet"
            )


async def cmd_wallet_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /wallet command - start wallet address input."""
    user_id = update.effective_user.id

    # Check if subscribed
    subscribers = load_subscribers()
    if user_id not in subscribers:
        await update.message.reply_text(
            "Please subscribe first using /sub before adding wallets."
        )
        return ConversationHandler.END

    # Show current wallets
    wallets = get_user_wallets(user_id)
    if wallets:
        wallet_list = "\n".join([f"{i+1}. {addr[:6]}...{addr[-4:]}" for i, (addr, _) in enumerate(wallets)])
        await update.message.reply_text(
            f"Your current wallets ({len(wallets)}/{MAX_WALLETS_PER_USER}):\n{wallet_list}\n\n"
            f"Send me an EVM wallet address to add (0x...):"
        )
    else:
        await update.message.reply_text(
            f"You have no wallets yet.\n\n"
            f"Send me an EVM wallet address to add (0x...):"
        )

    return WAITING_FOR_ADDRESS


async def cmd_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle wallet address input."""
    user_id = update.effective_user.id
    address = update.message.text.strip()

    # Validate and add
    success, removed_address = add_wallet(user_id, address)

    if not success:
        if not validate_evm_address(address):
            await update.message.reply_text(
                "Invalid EVM address format. Please send a valid address (0x... with 42 characters).\n\n"
                "Or send /cancel to abort."
            )
            return WAITING_FOR_ADDRESS
        else:
            # Already exists
            await update.message.reply_text(
                "This wallet is already in your list!"
            )
            return ConversationHandler.END

    # Success
    if removed_address:
        await update.message.reply_text(
            f"✓ Wallet added: {address[:6]}...{address[-4:]}\n\n"
            f"⚠️ You had {MAX_WALLETS_PER_USER} wallets. Removed oldest:\n"
            f"{removed_address[:6]}...{removed_address[-4:]}"
        )
    else:
        wallets = get_user_wallets(user_id)
        await update.message.reply_text(
            f"✓ Wallet added: {address[:6]}...{address[-4:]}\n\n"
            f"You now have {len(wallets)}/{MAX_WALLETS_PER_USER} wallets."
        )

    return ConversationHandler.END


async def cmd_wallet_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Cancel wallet addition."""
    await update.message.reply_text("Wallet addition cancelled.")
    return ConversationHandler.END


async def send_alert(app: Application, user_id: int, message: str):
    """
    Send alert message to a specific user.

    Args:
        app: Telegram application instance
        user_id: Telegram user ID
        message: Message to send
    """
    try:
        await app.bot.send_message(chat_id=user_id, text=message)
    except Exception as e:
        logger.error(f"Failed to send message to {user_id}: {e}")


# ============================================================================
# Main Monitoring Loop
# ============================================================================

async def job_once(app: Application):
    """
    Execute one monitoring cycle.

    Args:
        app: Telegram application instance
    """
    try:
        # Get all user-wallet pairs
        user_wallet_pairs = get_all_user_wallet_pairs()

        if not user_wallet_pairs:
            logger.info("No user-wallet pairs to monitor")
            return

        logger.info(f"Monitoring {len(user_wallet_pairs)} user-wallet pairs")

        # Monitor each wallet
        for user_id, address in user_wallet_pairs:
            try:
                # Fetch current positions
                curr = await fetch_positions(address)

                # Load previous snapshot
                prev = load_latest_snapshot(user_id, address)

                # Detect changes
                changes = detect_changes(prev, curr, CHANGE_THRESHOLD_PCT, COMPARE_ABS)

                # Send alert if changes detected
                if changes:
                    timestamp = datetime.now()
                    message = render_alert_message(address, changes, timestamp)
                    await send_alert(app, user_id, message)
                    logger.info(f"Alert sent to user {user_id} for wallet {address} ({len(changes)} changes)")

                # Save current snapshot
                append_snapshot(user_id, address, curr, datetime.now())

                # Small delay between API calls to avoid rate limiting
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error monitoring wallet {address} for user {user_id}: {e}", exc_info=True)
                continue

    except Exception as e:
        logger.error(f"Error in monitoring cycle: {e}", exc_info=True)


async def monitoring_loop(app: Application):
    """
    Main monitoring loop that runs periodically.

    Args:
        app: Telegram application instance
    """
    logger.info(f"Starting monitoring loop (interval: {POLL_INTERVAL_MIN} min)")

    while True:
        await job_once(app)

        # Sleep with random jitter
        jitter = random.uniform(0, 60)  # 0-60 seconds
        sleep_time = POLL_INTERVAL_MIN * 60 + jitter
        logger.info(f"Sleeping for {sleep_time:.0f}s")
        await asyncio.sleep(sleep_time)


# ============================================================================
# Main Entry Point
# ============================================================================

async def main():
    """Main entry point."""
    if not TELEGRAM_BOT_TOKEN:
        logger.error("TELEGRAM_BOT_TOKEN not set")
        return

    logger.info("Starting HypurrSeeker")
    logger.info(f"Default wallet: {DEFAULT_WALLET_ADDRESS}")
    logger.info(f"Poll interval: {POLL_INTERVAL_MIN} min")
    logger.info(f"Change threshold: {CHANGE_THRESHOLD_PCT}%")
    logger.info(f"Max wallets per user: {MAX_WALLETS_PER_USER}")

    # Initialize Telegram bot
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sub", cmd_sub))

    # Add conversation handler for /wallet
    wallet_conv_handler = ConversationHandler(
        entry_points=[CommandHandler("wallet", cmd_wallet_start)],
        states={
            WAITING_FOR_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_wallet_address)
            ],
        },
        fallbacks=[CommandHandler("cancel", cmd_wallet_cancel)],
    )
    app.add_handler(wallet_conv_handler)

    # Start bot
    await app.initialize()
    await app.start()
    await app.updater.start_polling()

    logger.info("Bot started, beginning monitoring loop")

    # Start monitoring loop
    try:
        await monitoring_loop(app)
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await app.updater.stop()
        await app.stop()
        await app.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
