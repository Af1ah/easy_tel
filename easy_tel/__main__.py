import argparse
import asyncio
import logging
import os
from pathlib import Path

from telethon import TelegramClient, events
from telethon.sessions import MemorySession

from .store import Store
from .transfer import rclone, safe_name
from .progress import Progress, duration
from .retry import retry_delay, error_summary
from .worker import transfer_job

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


def diagnose():
    import sys
    import telethon
    from telethon.crypto import aes
    print('Python:', sys.version.split()[0])
    print('Telethon:', telethon.__version__)
    print('Native AES acceleration:', 'active' if aes.cryptg is not None else 'MISSING — install requirements.txt')
    print('Configured download workers:', os.environ.get('TEL_DOWNLOAD_WORKERS', '4'))
    print('Parallel threshold (bytes):', os.environ.get('TEL_PARALLEL_MIN_BYTES', str(32*1024*1024)))
    print('Parallel disk writes:', 'supported' if hasattr(os, 'pwrite') else 'standard downloader fallback')


async def main(check=False):
    api_id, api_hash, token = credentials()
    state = Path(os.environ.get("TEL_STATE_DIR", "./state")).resolve()
    if not check:
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
    client = TelegramClient(MemorySession() if check else str(state / "bot"),
                            api_id, api_hash, connection_retries=5, timeout=10, flood_sleep_threshold=0)
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
        max_attempts = max(1, int(os.environ.get("TEL_MAX_ATTEMPTS", "4")))
        progress_interval = max(3, float(os.environ.get("TEL_PROGRESS_INTERVAL", "5")))
        retry_base = max(1, float(os.environ.get("TEL_RETRY_BASE_SECONDS", "15")))

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
                await event.reply("Send or forward a file to upload it to Drive. /status shows recent jobs. Use /retry JOB_ID to retry a failed job. Failed local files are retained.")
            elif text == "/status":
                rows = store.recent(event.chat_id)
                await event.reply("\n".join(
                    f"Job {r['id']}: {r['status']} (attempts: {r['attempts']})"
                    for r in rows) or "No jobs yet.")
            elif text.startswith("/retry "):
                try:
                    job_id = int(text.split()[1])
                except (ValueError, IndexError):
                    await event.reply("Usage: /retry JOB_ID")
                    return
                if store.pending_count() >= queue_limit:
                    await event.reply("Queue is full. Please try later.")
                elif store.retry(job_id, event.chat_id):
                    wake.set()
                    await event.reply(f"Job {job_id}: queued again; same remote filename")
                else:
                    await event.reply("Only your failed jobs can be retried.")
            elif event.file:
                size = event.file.size
                if not size or size > maximum:
                    await event.reply("File is empty or exceeds the configured size limit.")
                    return
                if store.pending_count() >= queue_limit:
                    await event.reply("Queue is full. Please try later.")
                    return
                row = store.add(event.chat_id, event.id, safe_name(event.file.name or f"file{event.file.ext or '.bin'}"), size)
                try:
                    reply = await event.reply(f"Job {row['id']}: {row['status']}", parse_mode=None)
                    if not row["progress_message"]:
                        store.save_progress(row["id"], reply.id)
                finally:
                    wake.set()

        async def worker():
            while True:
                wake.clear()
                job = store.next()
                if job is None:
                    try:
                        await asyncio.wait_for(wake.wait(), min(60, store.wait_seconds()))
                    except asyncio.TimeoutError:
                        pass
                    continue
                if job["attempts"] >= max_attempts:
                    store.status(job["id"], "failed")
                    continue
                attempt = job["attempts"] + 1
                store.begin(job["id"])
                progress = Progress(client, job["chat"], job["id"], job["name"], job["size"],
                                    interval=progress_interval, message_id=job["progress_message"],
                                    save_message=lambda mid: store.save_progress(job["id"], mid))
                await progress.start()
                try:
                    await transfer_job(client, job, state, reserve, destination, progress)
                    store.status(job["id"], "done")
                    progress.phase_update("uploaded and verified")
                except Exception as exc:
                    delay = retry_delay(exc, attempt, retry_base)
                    reason = error_summary(exc)
                    log.error("Job %s attempt %s failed: %s", job["id"], attempt, reason)
                    if delay is not None and attempt < max_attempts:
                        store.defer(job["id"], delay, reason)
                        progress.phase_update("waiting to retry",
                            f"Attempt {attempt}/{max_attempts}; retry in {duration(delay)} ({reason}). Local file retained.")
                    else:
                        store.status(job["id"], "failed")
                        progress.phase_update("failed",
                            f"Attempt {attempt}/{max_attempts}: {reason}. Local files retained. Fix the cause, then /retry {job['id']}.")
                finally:
                    await progress.close()

        print("Service ready; transfers " + ("enabled" if allowed else "disabled until IDs are allowed"), flush=True)
        task = asyncio.create_task(worker())
        disconnected = asyncio.create_task(client.run_until_disconnected())
        try:
            completed, _ = await asyncio.wait((task, disconnected), return_when=asyncio.FIRST_COMPLETED)
            for finished in completed:
                await finished  # Surface a worker crash so systemd can restart the service.
        finally:
            task.cancel()
            disconnected.cancel()
            await asyncio.gather(task, disconnected, return_exceptions=True)
            store.db.close()
    finally:
        await client.disconnect()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-telegram", action="store_true", help="Authenticate bot without starting transfers")
    parser.add_argument("--diagnose", action="store_true", help="Show local download setup without credentials or network access")
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    log.setLevel(logging.INFO)
    if args.diagnose:
        diagnose()
        raise SystemExit(0)
    try:
        asyncio.run(main(args.check_telegram))
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        # Tracebacks from clients may include request data; show only the type.
        print(f"Startup failed: {type(exc).__name__}. Check credentials, Telegram networking, and rclone access.", flush=True)
        raise SystemExit(1)
