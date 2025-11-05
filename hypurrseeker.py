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
MIN_POSITION_VALUE_USD = float(os.getenv("MIN_POSITION_VALUE_USD", "10000"))

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

async def fetch_positions(address: str) -> Dict[str, Tuple[Decimal, Decimal]]:
    """
    Fetch Perps positions from Hyperliquid Info API.

    Args:
        address: EVM address to query

    Returns:
        Dictionary mapping token symbol to (position_size, position_value_usd) tuple
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
                        position_value = asset["position"]["positionValue"]
                        positions[coin] = (Decimal(str(szi)), Decimal(str(position_value)))
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

def load_wallet_snapshot(address: str) -> Tuple[Dict[str, Tuple[Decimal, Decimal]], Optional[datetime]]:
    """
    Load the snapshot from CSV for a specific wallet address.

    Args:
        address: Wallet address

    Returns:
        Tuple of (positions dict mapping token to (size, value_usd), timestamp of snapshot)
    """
    if not SNAPSHOTS_FILE.exists():
        logger.info("No snapshot file found, returning empty snapshot")
        return {}, None

    address = address.lower()
    positions = {}
    timestamp = None

    with open(SNAPSHOTS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["address"].lower() == address:
                amount = Decimal(row["amount"])
                # Handle backward compatibility: value column may not exist in old snapshots
                value_usd = Decimal(row.get("value_usd", "0"))
                positions[row["token"]] = (amount, value_usd)
                if timestamp is None and "timestamp" in row:
                    timestamp = datetime.fromisoformat(row["timestamp"])

    logger.info(f"Loaded {len(positions)} positions from snapshot for {address}")
    return positions, timestamp


def update_wallet_snapshot(
    address: str,
    positions: Dict[str, Tuple[Decimal, Decimal]],
    timestamp: datetime
):
    """
    Update snapshot CSV with current positions for a wallet.
    Preserves followers_count.

    Args:
        address: Wallet address
        positions: Dictionary of token -> (amount, value_usd) tuple
        timestamp: Current timestamp
    """
    address = address.lower()
    file_exists = SNAPSHOTS_FILE.exists()

    # Read existing snapshots and preserve followers_count
    existing_rows = []
    followers_count = 1  # Default if not found

    if file_exists:
        with open(SNAPSHOTS_FILE, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["address"].lower() == address:
                    # Save followers_count for this wallet
                    followers_count = int(row["followers_count"])
                else:
                    # Keep rows for other wallets
                    existing_rows.append(row)

    # Add new/updated positions for this wallet
    new_rows = []
    for token, (amount, value_usd) in positions.items():
        new_rows.append({
            "address": address,
            "followers_count": str(followers_count),
            "timestamp": timestamp.isoformat(),
            "token": token,
            "amount": str(amount),
            "value_usd": str(value_usd)
        })

    # Write back all rows
    with open(SNAPSHOTS_FILE, "w", newline="") as f:
        fieldnames = ["address", "followers_count", "timestamp", "token", "amount", "value_usd"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(existing_rows + new_rows)

    logger.info(f"Updated snapshot with {len(positions)} positions for {address}")


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
    Add a new subscriber to CSV or reactivate existing inactive subscriber.

    Args:
        user_id: Telegram user ID
        username: Telegram username

    Returns:
        True if newly subscribed or reactivated, False if already active
    """
    file_exists = SUBSCRIBERS_FILE.exists()

    # Check if user exists and their status
    user_exists = False
    is_active = False
    if file_exists:
        rows = []
        with open(SUBSCRIBERS_FILE, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if int(row["user_id"]) == user_id:
                    user_exists = True
                    if row["active"].lower() == "true":
                        is_active = True
                        logger.info(f"User {user_id} already subscribed")
                        return False
                    else:
                        # Reactivate the user
                        row["active"] = "true"
                rows.append(row)

        # If user exists but was inactive, update and return
        if user_exists and not is_active:
            with open(SUBSCRIBERS_FILE, "w", newline="") as f:
                fieldnames = ["user_id", "username", "subscribed_at", "active"]
                writer = csv.DictWriter(f, fieldnames=fieldnames)
                writer.writeheader()
                writer.writerows(rows)
            logger.info(f"Re-subscribed user {user_id} ({username})")
            return True

    # Add new subscriber if doesn't exist
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


def remove_subscriber(user_id: int) -> bool:
    """
    Unsubscribe a user by setting active to false.

    Args:
        user_id: Telegram user ID

    Returns:
        True if user was unsubscribed, False if not found or already inactive
    """
    if not SUBSCRIBERS_FILE.exists():
        return False

    # Read all rows and mark user as inactive
    rows = []
    found = False
    with open(SUBSCRIBERS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["user_id"]) == user_id and row["active"].lower() == "true":
                row["active"] = "false"
                found = True
            rows.append(row)

    if not found:
        return False

    # Write back
    with open(SUBSCRIBERS_FILE, "w", newline="") as f:
        fieldnames = ["user_id", "username", "subscribed_at", "active"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Unsubscribed user {user_id}")
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


def remove_wallet(user_id: int, address: str) -> bool:
    """
    Remove a wallet for a user by setting it to inactive.

    Args:
        user_id: Telegram user ID
        address: Wallet address to remove

    Returns:
        True if wallet was removed, False if not found
    """
    if not WALLETS_FILE.exists():
        return False

    address = address.lower()
    rows = []
    found = False

    with open(WALLETS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (int(row["user_id"]) == user_id and
                row["address"].lower() == address and
                row["active"].lower() == "true"):
                row["active"] = "false"
                found = True
            rows.append(row)

    if not found:
        return False

    # Write back
    with open(WALLETS_FILE, "w", newline="") as f:
        fieldnames = ["user_id", "address", "added_at", "active"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info(f"Removed wallet {address} for user {user_id}")
    return True


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


def increment_wallet_followers(address: str):
    """
    Increment followers_count for a wallet in snapshots.csv.
    If wallet doesn't exist, initialize with followers_count=1 and fetch initial snapshot.

    Args:
        address: Wallet address
    """
    address = address.lower()
    file_exists = SNAPSHOTS_FILE.exists()

    # Check if wallet exists in snapshots
    wallet_exists = False
    rows = []

    if file_exists:
        with open(SNAPSHOTS_FILE, "r", newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row["address"].lower() == address:
                    wallet_exists = True
                    row["followers_count"] = str(int(row["followers_count"]) + 1)
                rows.append(row)

    if wallet_exists:
        # Write back with incremented count
        with open(SNAPSHOTS_FILE, "w", newline="") as f:
            fieldnames = ["address", "followers_count", "timestamp", "token", "amount"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"Incremented followers_count for wallet {address}")
    else:
        # New wallet - initialize with followers_count=1 (no initial snapshot, will be fetched on first monitoring)
        logger.info(f"New wallet {address} added to snapshots with followers_count=1")
        # Note: We don't fetch initial snapshot here to keep command handlers fast
        # Initial snapshot will be created on first monitoring cycle


def decrement_wallet_followers(address: str):
    """
    Decrement followers_count for a wallet in snapshots.csv.
    Never goes below 0.

    Args:
        address: Wallet address
    """
    if not SNAPSHOTS_FILE.exists():
        return

    address = address.lower()
    rows = []
    found = False

    with open(SNAPSHOTS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["address"].lower() == address:
                found = True
                current_count = int(row["followers_count"])
                row["followers_count"] = str(max(0, current_count - 1))
            rows.append(row)

    if found:
        # Write back with decremented count
        with open(SNAPSHOTS_FILE, "w", newline="") as f:
            fieldnames = ["address", "followers_count", "timestamp", "token", "amount"]
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
        logger.info(f"Decremented followers_count for wallet {address}")


def get_monitored_wallets() -> List[str]:
    """
    Get list of unique wallet addresses with followers_count > 0.

    Returns:
        List of wallet addresses that should be monitored
    """
    if not SNAPSHOTS_FILE.exists():
        return []

    monitored = set()
    with open(SNAPSHOTS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if int(row["followers_count"]) > 0:
                monitored.add(row["address"].lower())

    return list(monitored)


def get_active_wallet_followers(address: str) -> List[int]:
    """
    Get all active subscriber user IDs who follow this wallet.

    Args:
        address: Wallet address

    Returns:
        List of user_ids where user is active subscriber and wallet is active for that user
    """
    if not WALLETS_FILE.exists():
        return []

    address = address.lower()
    active_subscribers = set(load_subscribers())
    followers = []

    with open(WALLETS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row["address"].lower() == address and
                row["active"].lower() == "true"):
                user_id = int(row["user_id"])
                if user_id in active_subscribers:
                    followers.append(user_id)

    return followers


# ============================================================================
# Diff & Alert Logic
# ============================================================================

def detect_changes(
    prev: Dict[str, Tuple[Decimal, Decimal]],
    curr: Dict[str, Tuple[Decimal, Decimal]],
    threshold_pct: float,
    compare_abs: bool = True,
    min_value_usd: Decimal = Decimal("10000")
) -> List[Tuple[str, Decimal, Decimal, Decimal, Decimal, float]]:
    """
    Detect position changes above threshold.
    Filters out small positions (< $10k) UNLESS position was/is >= $10k (handles opens/closes).

    Args:
        prev: Previous positions mapping token -> (size, value_usd)
        curr: Current positions mapping token -> (size, value_usd)
        threshold_pct: Alert threshold percentage
        compare_abs: Whether to compare absolute values
        min_value_usd: Minimum USD value threshold (default $10,000)

    Returns:
        List of (token, prev_amount, curr_amount, prev_value_usd, curr_value_usd, pct_change)
    """
    changes = []
    all_tokens = set(prev.keys()) | set(curr.keys())

    for token in all_tokens:
        prev_amount, prev_value_usd = prev.get(token, (Decimal(0), Decimal(0)))
        curr_amount, curr_value_usd = curr.get(token, (Decimal(0), Decimal(0)))

        if prev_amount == curr_amount:
            continue

        # Apply $10k filter: ignore if BOTH prev and curr are below threshold
        # Alert if either prev >= $10k (position closing) OR curr >= $10k (position opening/growing)
        prev_value_abs = abs(prev_value_usd)
        curr_value_abs = abs(curr_value_usd)

        if prev_value_abs < min_value_usd and curr_value_abs < min_value_usd:
            logger.debug(f"Skipping {token}: both prev (${prev_value_abs}) and curr (${curr_value_abs}) below ${min_value_usd}")
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
            changes.append((token, prev_amount, curr_amount, prev_value_usd, curr_value_usd, pct_change))

    return changes


def render_alert_message(
    address: str,
    changes: List[Tuple[str, Decimal, Decimal, Decimal, Decimal, float]],
    prev_timestamp: Optional[datetime],
    curr_timestamp: datetime
) -> str:
    """
    Render alert message for Telegram.

    Args:
        address: Wallet address
        changes: List of (token, prev_amount, curr_amount, prev_value_usd, curr_value_usd, pct_change)
        prev_timestamp: Previous snapshot timestamp (None if first time)
        curr_timestamp: Current timestamp

    Returns:
        Formatted message string
    """
    lines = ["[HypurrSeeker]", f"Wallet: {address[:6]}...{address[-4:]}"]

    # Calculate and show time elapsed
    if prev_timestamp:
        elapsed = curr_timestamp - prev_timestamp
        hours, remainder = divmod(int(elapsed.total_seconds()), 3600)
        minutes, _ = divmod(remainder, 60)

        if hours > 0:
            elapsed_str = f"{hours}h {minutes}m"
        else:
            elapsed_str = f"{minutes}m"

        lines.append(f"Changed after {elapsed_str}")
    else:
        lines.append("First snapshot")

    lines.append("")

    # Token changes
    for token, prev, curr, prev_value, curr_value, pct in changes:
        sign = "+" if pct > 0 else ""
        # Format USD values with thousand separators
        prev_usd_str = f"${abs(prev_value):,.0f}"
        curr_usd_str = f"${abs(curr_value):,.0f}"
        lines.append(f"{token}: {prev} → {curr} ({sign}{pct:.1f}%)")
        lines.append(f"  Value: {prev_usd_str} → {curr_usd_str}")

    lines.append("")

    # Timestamps
    if prev_timestamp:
        lines.append(f"Previous: {prev_timestamp.strftime('%Y-%m-%d %H:%M')}")
    lines.append(f"Current:  {curr_timestamp.strftime('%Y-%m-%d %H:%M')}")

    return "\n".join(lines)


# ============================================================================
# Telegram Bot
# ============================================================================

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command - show welcome message."""
    await update.message.reply_text(
        "🐱 Welcome to HypurrSeeker!\n\n"
        "Get instant alerts when Hyperliquid wallet positions change by more than 5%.\n\n"
        "🚀 Getting Started:\n"
        "1. /sub - Subscribe to alerts\n"
        "2. /wallet - Add or remove wallets (max 5)\n"
        "   • Type an address (0x...) to ADD\n"
        "   • Type a number (1-5) to REMOVE\n"
        "3. Get alerts every 20 minutes!\n\n"
        "📋 Other Commands:\n"
        "/unsub - Unsubscribe from alerts\n"
        "/cancel - Cancel current action\n\n"
        "Ready? Send /sub to begin!"
    )


async def cmd_sub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /sub command to subscribe to alerts."""
    user_id = update.effective_user.id
    username = update.effective_user.username

    is_new_subscriber = add_subscriber(user_id, username)

    # If new subscriber or re-subscribed
    if is_new_subscriber:
        existing_wallets = get_user_wallets(user_id)

        # Check if this is a re-subscription (user has wallets but was inactive)
        if existing_wallets:
            # Re-subscribing: increment followers_count for all user's wallets
            for address, _ in existing_wallets:
                increment_wallet_followers(address)

            await update.message.reply_text(
                "✓ Welcome back! You've been re-subscribed to HypurrSeeker alerts!\n\n"
                f"Your {len(existing_wallets)} wallet(s) are still being monitored.\n\n"
                "Use /wallet to add or remove wallets"
            )
        # New subscriber with no wallets - add default
        elif DEFAULT_WALLET_ADDRESS:
            success, _ = add_wallet(user_id, DEFAULT_WALLET_ADDRESS)
            if success:
                # Increment followers for default wallet
                increment_wallet_followers(DEFAULT_WALLET_ADDRESS)

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
            f"Reply with:\n"
            f"• An address (0x...) to ADD a wallet\n"
            f"• A number (1-{len(wallets)}) to REMOVE that wallet\n"
            f"• /cancel to exit"
        )
    else:
        await update.message.reply_text(
            f"You have no wallets yet.\n\n"
            f"Send me an EVM wallet address to add (0x...):"
        )

    return WAITING_FOR_ADDRESS


async def cmd_wallet_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle wallet address or number input."""
    user_id = update.effective_user.id
    user_input = update.message.text.strip()

    # Check if input is a number (for removal)
    if user_input.isdigit():
        wallet_number = int(user_input)
        wallets = get_user_wallets(user_id)

        # Validate number range
        if wallet_number < 1 or wallet_number > len(wallets):
            await update.message.reply_text(
                f"Invalid number. Please enter 1-{len(wallets)} to remove, or an address (0x...) to add.\n\n"
                "Or send /cancel to abort."
            )
            return WAITING_FOR_ADDRESS

        # Remove the wallet
        address_to_remove = wallets[wallet_number - 1][0]
        success = remove_wallet(user_id, address_to_remove)

        if success:
            # Decrement followers_count for removed wallet
            decrement_wallet_followers(address_to_remove)

            await update.message.reply_text(
                f"✓ Wallet removed: {address_to_remove[:6]}...{address_to_remove[-4:]}\n\n"
                f"You now have {len(wallets) - 1}/{MAX_WALLETS_PER_USER} wallets."
            )
        else:
            await update.message.reply_text("Failed to remove wallet. Please try again.")

        return ConversationHandler.END

    # Otherwise, treat as address (for addition)
    address = user_input

    # Validate and add
    success, removed_address = add_wallet(user_id, address)

    if not success:
        if not validate_evm_address(address):
            await update.message.reply_text(
                "Invalid input. Please send:\n"
                "• A valid address (0x... with 42 characters) to add\n"
                "• A number to remove a wallet\n"
                "• /cancel to abort"
            )
            return WAITING_FOR_ADDRESS
        else:
            # Already exists
            await update.message.reply_text(
                "This wallet is already in your list!"
            )
            return ConversationHandler.END

    # Success - wallet was added
    # Increment followers_count for new wallet
    increment_wallet_followers(address)

    # If at MAX_WALLETS, oldest was auto-removed
    if removed_address:
        # Decrement followers_count for removed wallet
        decrement_wallet_followers(removed_address)

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


async def cmd_unsub(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /unsub command to unsubscribe from alerts."""
    user_id = update.effective_user.id

    success = remove_subscriber(user_id)

    if success:
        # Decrement followers_count for all user's wallets
        user_wallets = get_user_wallets(user_id)
        for address, _ in user_wallets:
            decrement_wallet_followers(address)

        await update.message.reply_text(
            "✓ You've been unsubscribed from HypurrSeeker alerts.\n\n"
            "Your wallet data has been preserved. You can re-subscribe anytime with /sub"
        )
    else:
        await update.message.reply_text(
            "You're not currently subscribed.\n\n"
            "Use /sub to subscribe to alerts."
        )



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
        # Get unique wallets with followers_count > 0
        monitored_wallets = get_monitored_wallets()

        if not monitored_wallets:
            logger.info("No wallets to monitor")
            return

        logger.info(f"Monitoring {len(monitored_wallets)} unique wallets")

        # Monitor each unique wallet
        for address in monitored_wallets:
            try:
                # Fetch current positions from API (once per wallet)
                curr = await fetch_positions(address)

                # Load previous snapshot for this wallet (returns positions and timestamp)
                prev, prev_timestamp = load_wallet_snapshot(address)

                # Detect changes (with $10k minimum value filter)
                changes = detect_changes(prev, curr, CHANGE_THRESHOLD_PCT, COMPARE_ABS, Decimal(str(MIN_POSITION_VALUE_USD)))

                # Only update snapshot and send alerts if changes detected
                if changes:
                    curr_timestamp = datetime.now()

                    # Update snapshot once for this wallet
                    update_wallet_snapshot(address, curr, curr_timestamp)

                    # Get all active followers of this wallet
                    followers = get_active_wallet_followers(address)

                    if followers:
                        # Send personalized alert to each follower
                        message = render_alert_message(address, changes, prev_timestamp, curr_timestamp)
                        for user_id in followers:
                            await send_alert(app, user_id, message)

                        logger.info(f"Alert sent to {len(followers)} user(s) for wallet {address} ({len(changes)} changes)")
                    else:
                        logger.info(f"Changes detected for wallet {address} but no active followers")
                else:
                    logger.info(f"No changes detected for wallet {address}, keeping old snapshot")

                # Small delay between API calls to avoid rate limiting
                await asyncio.sleep(1)

            except Exception as e:
                logger.error(f"Error monitoring wallet {address}: {e}", exc_info=True)
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
    logger.info(f"Min position value: ${MIN_POSITION_VALUE_USD:,.0f}")
    logger.info(f"Max wallets per user: {MAX_WALLETS_PER_USER}")

    # Initialize Telegram bot
    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    # Add command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("sub", cmd_sub))
    app.add_handler(CommandHandler("unsub", cmd_unsub))

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
