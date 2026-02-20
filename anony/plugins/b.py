import json
import os
from datetime import datetime
from pyrogram import filters
from pyrogram.types import Message
from anony import app, config
from html import escape

COLLECTIONS_MAP = {
    "authdb": "adminauth",
    "authuserdb": "authuser",
    "autoenddb": "autoend",
    "assdb": "assistants",
    "blacklist_chatdb": "blacklistChat",
    "blockeddb": "blockedusers",
    "chatsdb": "chats",
    "channeldb": "cplaymode",
    "countdb": "upcount",
    "gbansdb": "gban",
    "langdb": "language",
    "onoffdb": "onoffper",
    "playmodedb": "playmode",
    "playtypedb": "playtypedb",
    "skipdb": "skipmode",
    "sudoersdb": "sudoers",
    "usersdb": "tgusersdb",
}


@app.on_message(filters.command("backup") & filters.user(config.OWNER_ID))
async def backup_database(client, message: Message):
    status = await message.reply_text("Creating database backup...")

    try:
        from pymongo import AsyncMongoClient

        mongo_client = AsyncMongoClient(config.MONGO_URL)
        db = mongo_client.mongodb

        backup_data = {
            "timestamp": datetime.now().isoformat(),
            "version": "1.0",
            "collections": {}
        }

        for key, collection_name in COLLECTIONS_MAP.items():
            backup_data["collections"][key] = []
            async for doc in db[collection_name].find():
                backup_data["collections"][key].append(doc)

        await status.edit_text(
            f"Backup created! Uploading...\n"
            f"Collections: {len(COLLECTIONS_MAP)}"
        )

        filename = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
        filepath = f"/tmp/{filename}"

        with open(filepath, "w") as f:
            json.dump(backup_data, f, indent=2, default=str)

        file_size = os.path.getsize(filepath) / 1024

        total_docs = sum(len(v) for v in backup_data["collections"].values())

        await message.reply_document(
            document=filepath,
            caption=(
                f"**Database Backup**\n\n"
                f"**Date:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"**Collections:** {len(COLLECTIONS_MAP)}\n"
                f"**Total Documents:** {total_docs}\n"
                f"**Size:** {file_size:.2f} KB\n\n"
                f"**Collections Backed Up:**\n"
                + "\n".join(
                    f"• `{k}` → `{v}` ({len(backup_data['collections'][k])} docs)"
                    for k, v in COLLECTIONS_MAP.items()
                )
            )
        )

        if os.path.exists(filepath):
            os.remove(filepath)
        await status.delete()
        await mongo_client.close()

    except Exception as e:
        await status.edit_text(f"❌ Backup failed: {str(e)}")


@app.on_message(filters.command("restore") & filters.user(config.OWNER_ID))
async def restore_database(client, message: Message):
    if not message.reply_to_message or not message.reply_to_message.document:
        return await message.reply_text(
            "Please reply to a backup file with /restore command."
        )

    if not message.reply_to_message.document.file_name.endswith(".json"):
        return await message.reply_text("❌ Invalid backup file. Must be a .json file.")

    status = await message.reply_text("⬇️ Downloading backup file...")
    filepath = None

    try:
        from pymongo import AsyncMongoClient

        filepath = await message.reply_to_message.download()

        await status.edit_text("📂 Loading backup data...")

        with open(filepath, "r") as f:
            backup_data = json.load(f)

        if "collections" not in backup_data:
            return await status.edit_text("❌ Invalid backup file format.")

        await status.edit_text("♻️ Restoring database...")

        mongo_client = AsyncMongoClient(config.MONGO_URL)
        db = mongo_client.mongodb

        restored_count = 0
        restored_collections = []
        skipped_collections = []

        for key, collection_name in COLLECTIONS_MAP.items():
            documents = backup_data["collections"].get(key, [])

            if not documents:
                skipped_collections.append(key)
                continue

            collection = db[collection_name]
            await collection.delete_many({})
            await collection.insert_many(documents)
            restored_count += len(documents)
            restored_collections.append(f"• `{key}` → {len(documents)} docs")

        await mongo_client.close()

        skipped_text = (
            f"\n**Skipped (empty):** {', '.join(skipped_collections)}"
            if skipped_collections else ""
        )

        await status.edit_text(
            f"✅ **Database Restored Successfully**\n\n"
            f"**Backup Date:** {backup_data.get('timestamp', 'Unknown')}\n"
            f"**Documents Restored:** {restored_count}\n"
            f"**Collections Restored:** {len(restored_collections)}\n"
            f"{skipped_text}\n\n"
            f"**Details:**\n" + "\n".join(restored_collections)
        )

    except Exception as e:
        await status.edit_text(f"❌ Restore failed: {str(e)}")

    finally:
        if filepath and os.path.exists(filepath):
            os.remove(filepath)
