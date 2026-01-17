"""
Backfill script to generate BlockTrace IDs for existing users.

Run this script once to:
1. Generate unique blocktrace_id for all users without one
2. Create a unique index on the blocktrace_id field

Usage:
    python scripts/backfill_blocktrace_ids.py
"""

import asyncio
import secrets
import string
import os
from motor.motor_asyncio import AsyncIOMotorClient
from dotenv import load_dotenv

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI")
DB_NAME = os.getenv("DB_NAME", "BlockTrace")


def generate_blocktrace_id_sync() -> str:
    """Generate a BlockTrace ID in format BT-XXXXXXXX"""
    chars = string.ascii_uppercase + string.digits
    random_part = ''.join(secrets.choice(chars) for _ in range(8))
    return f"BT-{random_part}"


async def generate_unique_blocktrace_id(db) -> str:
    """Generate a unique BlockTrace ID, checking for collisions"""
    while True:
        blocktrace_id = generate_blocktrace_id_sync()
        existing = await db["users"].find_one({"blocktrace_id": blocktrace_id})
        if not existing:
            return blocktrace_id


async def backfill():
    if not MONGO_URI:
        print("Error: MONGO_URI environment variable not set")
        return

    print(f"Connecting to MongoDB...")
    client = AsyncIOMotorClient(MONGO_URI)
    db = client[DB_NAME]

    # Find all users without blocktrace_id
    users_without_id = await db["users"].count_documents({"blocktrace_id": {"$exists": False}})
    print(f"Found {users_without_id} users without blocktrace_id")

    if users_without_id == 0:
        print("No users need backfilling.")
    else:
        cursor = db["users"].find({"blocktrace_id": {"$exists": False}})
        updated_count = 0

        async for user in cursor:
            blocktrace_id = await generate_unique_blocktrace_id(db)
            await db["users"].update_one(
                {"_id": user["_id"]},
                {"$set": {"blocktrace_id": blocktrace_id}}
            )
            updated_count += 1
            print(f"  Updated user {user.get('email', user['_id'])} -> {blocktrace_id}")

        print(f"Backfilled {updated_count} users with BlockTrace IDs")

    # Create unique index on blocktrace_id
    print("Creating unique index on blocktrace_id...")
    await db["users"].create_index("blocktrace_id", unique=True, sparse=True)
    print("Index created successfully")

    client.close()
    print("Done!")


if __name__ == "__main__":
    asyncio.run(backfill())
