"""Coalesce transfer callbacks; never put Telegram I/O in the data path."""
import asyncio
import time
from telethon import errors


def size_text(value):
    value = max(0, value)
    for unit in ('B', 'KiB', 'MiB', 'GiB', 'TiB'):
        if value < 1024 or unit == 'TiB':
            return f'{value:.1f} {unit}'
        value /= 1024


def duration(seconds):
    if seconds is None or seconds < 0:
        return 'calculating'
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f'{hours}h {minutes}m' if hours else f'{minutes}m {seconds}s'


class Progress:
    def __init__(self, client, chat, job_id, name, total, interval=1,
                 message_id=None, save_message=None, clock=time.monotonic):
        self.client, self.chat, self.job_id = client, chat, job_id
        self.name, self.total = name, total
        self.interval = max(1, interval)
        self.message_id, self.save_message = message_id, save_message
        self.clock = clock
        self.phase = 'queued'
        self.done = self.speed = 0
        self.eta = None
        self.started = clock()
        self.last_at = float('-inf')
        self.blocked_until = 0
        self.last_text = None
        self.buttons = None
        self.last_buttons = None
        self.detail = ''
        self.task = None

    def phase_update(self, phase, detail=''):
        self.phase, self.detail = phase, detail
        self.done = self.speed = 0
        self.eta = None
        self.started = self.clock()

    def update(self, done, total=None, speed=None, eta=None):
        self.done = min(self.total, max(0, done))
        elapsed = self.clock() - self.started
        self.speed = speed if speed is not None else self.done / elapsed if elapsed > 0 else 0
        self.eta = eta if eta is not None else (self.total-self.done)/self.speed if self.speed > 0 else None

    def text(self):
        text = f'Job {self.job_id}: {self.phase}\n{self.name}\nSize: {size_text(self.total)}'
        if self.phase in ('downloading', 'uploading to Drive'):
            percent = min(100, self.done / self.total * 100) if self.total else 0
            text += f'\n{percent:.1f}% — {size_text(self.done)} / {size_text(self.total)}'
            text += f'\n{size_text(self.speed)}/s · ETA: {duration(self.eta)} (this stage)'
        if self.detail:
            text += '\n' + self.detail
        return text

    async def emit(self, force=False):
        now, text = self.clock(), self.text()
        buttons = self.buttons
        if now < self.blocked_until or (text == self.last_text and buttons == self.last_buttons):
            return
        if not force and now - self.last_at < self.interval:
            return
        self.last_at = now
        try:
            if self.message_id:
                try:
                    await asyncio.wait_for(self.client.edit_message(
                        self.chat, self.message_id, text, parse_mode=None, buttons=buttons), 15)
                except (errors.MessageIdInvalidError, errors.MessageAuthorRequiredError,
                        errors.MessageEditTimeExpiredError):
                    self.message_id = None
            if not self.message_id:
                message = await asyncio.wait_for(self.client.send_message(
                    self.chat, text, parse_mode=None, buttons=buttons), 15)
                self.message_id = message.id
                if self.save_message:
                    self.save_message(message.id)
            self.last_text = text
            self.last_buttons = buttons
        except errors.MessageNotModifiedError:
            self.last_text = text
            self.last_buttons = buttons
        except (errors.FloodWaitError, errors.FloodPremiumWaitError) as exc:
            self.blocked_until = self.clock() + exc.seconds
        except Exception:
            # Retry on a later tick; notification failure must not fail a transfer.
            pass

    async def start(self):
        async def pump():
            while True:
                await self.emit()
                await asyncio.sleep(self.interval)
        self.task = asyncio.create_task(pump())

    async def close(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        await self.emit(force=True)
