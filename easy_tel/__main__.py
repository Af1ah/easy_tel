import argparse
import asyncio
import logging
import os
import shutil
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from .store import Store
from .transfer import rclone, safe_name, upload_verified

log = logging.getLogger("easy_tel")


def credentials():
    for name in ("TEL_APPID", "TEL_APP_HASH", "TEL_BOT_TOKEN"):
        if not os.environ.get(name):
            raise ValueError(f"Missing {name}")
    return int(os.environ["TEL_APPID"]), os.environ["TEL_APP_HASH"], os.environ["TEL_BOT_TOKEN"]


async def authenticate(client, token):
    await asyncio.wait_for(client.start(bot_token=token), 30)
    me = await client.get_me()
    expected = os.environ.get("TEL_EXPECTED_BOT_USERNAME", "easy_tel2_bot").lstrip("@").lower()
    if not me.bot or (me.username or "").lower() != expected:
        raise ValueError("Authenticated bot does not match TEL_EXPECTED_BOT_USERNAME")
    print(f"Authenticated @{me.username}", flush=True)


async def main(check=False):
    api_id, api_hash, token = credentials()
    state = Path(os.environ.get("TEL_STATE_DIR", "./state")).resolve()
    if not check:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = TelegramClient(MemorySession() if check else str(state / "bot"),
                            api_id, api_hash, connection_retries=0, timeout=10)
    try:
        await authenticate(client, token)
        if check:
            return
        allowed = {int(v.strip()) for v in os.environ.get("TEL_ALLOWED_USER_IDS", "").split(",") if v.strip()}
        destination = os.environ.get("TEL_DRIVE_DEST", "gdrive:easy_tel").rstrip("/")
        if not destination.startswith("gdrive:"):
            raise ValueError("TEL_DRIVE_DEST must use the gdrive: remote")
        # Read-only access check before accepting transfer jobs.
        await rclone("lsd", "gdrive:")
        store = Store(state / "jobs.sqlite3")
        maximum = int(os.environ.get("TEL_MAX_FILE_BYTES", "4294967296"))
        reserve = int(os.environ.get("TEL_DISK_RESERVE_BYTES", "1073741824"))
        queue_limit = int(os.environ.get("TEL_MAX_QUEUE", "20"))
        wake = asyncio.Event()

        async def notify(chat, text):
            try:
                await client.send_message(chat, text)
            except Exception as exc:
                log.warning("Notification failed: %s", type(exc).__name__)

        @client.on(events.NewMessage(incoming=True))
        async def receive(event):
            if not event.is_private:
                return
            text = event.raw_text.strip().split("@", 1)[0]
            if text == "/id":
                await event.reply(f"Your Telegram user ID: {event.sender_id}")
                return
            if event.sender_id not in allowed:
                await event.reply("Transfers are restricted. Use /id and ask the operator to allow your ID.")
                return
            if text in ("/start", "/help"):
                await event.reply("Send or forward a file to upload it to Drive. /status shows recent jobs. Failed local files are retained for the operator.")
            elif text == "/status":
                rows = store.recent(event.chat_id)
                await event.reply("\n".join(f"Job {r['id']}: {r['status']}" for r in rows) or "No jobs yet.")
            elif event.file:
                size = event.file.size
                if not size or size > maximum:
                    await event.reply("File is empty or exceeds the configured size limit.")
                    return
                if store.pending_count() >= queue_limit:
                    await event.reply("Queue is full. Please try later.")
                    return
                row = store.add(event.chat_id, event.id, safe_name(event.file.name or f"file{event.file.ext or '.bin'}"), size)
                wake.set()
                await event.reply(f"Job {row['id']}: {row['status']}")

        async def worker():
            while True:
                wake.clear()
                job = store.next()
                if job is None:
                    await wake.wait()
                    continue
                store.status(job["id"], "running")
                folder = state / "downloads" / str(job["id"])
                folder.mkdir(parents=True, exist_ok=True, mode=0o700)
                path = folder / job["name"]
                try:
                    if not path.is_file() or path.stat().st_size != job["size"]:
                        if shutil.disk_usage(state).free < job["size"] + reserve:
                            raise RuntimeError("Insufficient disk space")
                        message = await client.get_messages(job["chat"], ids=job["message"])
                        if not message or not message.file:
                            raise RuntimeError("Source message no longer available")
                        await notify(job["chat"], f"Job {job['id']}: downloading")
                        await client.download_media(message, file=str(path))
                    if not path.is_file() or path.stat().st_size != job["size"]:
                        raise RuntimeError("Downloaded size mismatch")
                    await notify(job["chat"], f"Job {job['id']}: uploading to Drive")
                    target = f"{destination}/{job['id']}-{job['name']}"
                    await upload_verified(path, target)
                    store.status(job["id"], "done")
                    await notify(job["chat"], f"Job {job['id']}: uploaded and verified")
                except Exception as exc:
                    store.status(job["id"], "failed")
                    log.error("Job %s failed: %s", job["id"], type(exc).__name__)
                    await notify(job["chat"], f"Job {job['id']}: failed; contact the operator. Any downloaded file was retained.")

        print("Service ready; transfers " + ("enabled" if allowed else "disabled until IDs are allowed"), flush=True)
        task = asyncio.create_task(worker())
        try:
            await client.run_until_disconnected()
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            store.db.close()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-telegram", action="store_true", help="Authenticate bot without starting transfers")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    try:
        asyncio.run(main(args.check_telegram))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Tracebacks from clients may include request data; show only the type.
        print(f"Startup failed: {type(exc).__name__}. Check credentials, Telegram networking, and rclone access.", flush=True)
        raise SystemExit(1)
