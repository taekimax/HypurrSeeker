#!/usr/bin/env python3
"""
Migration script to convert old snapshots.csv format to new format.

Old format: timestamp, user_id, address, token, amount
New format: address, followers_count, timestamp, token, amount
"""

import csv
from pathlib import Path
from collections import defaultdict

DATA_DIR = Path("data")
OLD_FILE = DATA_DIR / "snapshots.csv.backup"
NEW_FILE = DATA_DIR / "snapshots.csv"
WALLETS_FILE = DATA_DIR / "wallets.csv"
SUBSCRIBERS_FILE = DATA_DIR / "subscribers.csv"

def get_active_subscribers():
    """Get set of active subscriber user IDs."""
    if not SUBSCRIBERS_FILE.exists():
        return set()

    active = set()
    with open(SUBSCRIBERS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row["active"].lower() == "true":
                active.add(int(row["user_id"]))
    return active

def count_wallet_followers(address, active_subscribers):
    """Count how many active users follow this wallet."""
    if not WALLETS_FILE.exists():
        return 0

    count = 0
    address = address.lower()
    with open(WALLETS_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if (row["address"].lower() == address and
                row["active"].lower() == "true" and
                int(row["user_id"]) in active_subscribers):
                count += 1

    return count

def migrate():
    """Migrate old snapshots to new format."""
    if not OLD_FILE.exists():
        print(f"No backup file found at {OLD_FILE}")
        return

    # Get active subscribers
    active_subscribers = get_active_subscribers()
    print(f"Found {len(active_subscribers)} active subscribers")

    # Read old snapshots and group by address
    wallet_data = defaultdict(lambda: {"positions": {}, "timestamp": None})

    with open(OLD_FILE, "r", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            address = row["address"].lower()
            token = row["token"]
            amount = row["amount"]
            timestamp = row["timestamp"]

            # Keep only latest data for each wallet
            if wallet_data[address]["timestamp"] is None or timestamp > wallet_data[address]["timestamp"]:
                wallet_data[address]["timestamp"] = timestamp
                wallet_data[address]["positions"] = {token: amount}
            elif timestamp == wallet_data[address]["timestamp"]:
                wallet_data[address]["positions"][token] = amount

    print(f"Found {len(wallet_data)} unique wallet addresses")

    # Convert to new format
    new_rows = []
    for address, data in wallet_data.items():
        # Count active followers for this wallet
        followers_count = count_wallet_followers(address, active_subscribers)

        print(f"Wallet {address[:10]}... has {followers_count} active followers")

        # Add row for each token
        for token, amount in data["positions"].items():
            new_rows.append({
                "address": address,
                "followers_count": str(followers_count),
                "timestamp": data["timestamp"],
                "token": token,
                "amount": amount
            })

    # Write new format
    with open(NEW_FILE, "w", newline="") as f:
        fieldnames = ["address", "followers_count", "timestamp", "token", "amount"]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(new_rows)

    print(f"✓ Migration complete! Wrote {len(new_rows)} rows to {NEW_FILE}")
    print(f"  Old format backed up at {OLD_FILE}")

if __name__ == "__main__":
    migrate()
