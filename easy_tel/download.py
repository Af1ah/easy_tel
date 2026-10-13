"""Bounded, concurrent document download through Telethon's public API."""
import asyncio
import logging
import os
from telethon import errors

log = logging.getLogger('easy_tel.download')
CHUNK_SIZE = 512 * 1024  # Telethon's supported maximum getFile request size.


async def parallel_download(client, media, path, total, progress, workers):
    chunks = (total + CHUNK_SIZE - 1) // CHUNK_SIZE
    workers = min(workers, chunks)
    received = 0
    with path.open('w+b') as output:
        output.truncate(total)  # Sparse partial file; size alone never proves completion.
        fd = output.fileno()

        async def fetch(index):
            nonlocal received
            offset = index * CHUNK_SIZE
            count = (chunks - index + workers - 1) // workers
            stream = client.iter_download(
                media, offset=offset, stride=workers * CHUNK_SIZE, limit=count,
                chunk_size=CHUNK_SIZE, request_size=CHUNK_SIZE, file_size=total,
            )
            completed = 0
            try:
                async for data in stream:
                    expected = min(CHUNK_SIZE, total - offset)
                    if completed >= count or expected <= 0 or len(data) != expected:
                        raise OSError('Unexpected Telegram chunk size')
                    # Each worker owns disjoint offsets. No seek races, no full-file buffer.
                    view = memoryview(data)
                    written = 0
                    while written < len(view):
                        n = os.pwrite(fd, view[written:], offset + written)
                        if n <= 0:
                            raise OSError('Incomplete disk write')
                        written += n
                    received += len(data)
                    progress(received, total)
                    completed += 1
                    offset += workers * CHUNK_SIZE
                if completed != count:
                    raise OSError('Telegram stream ended before all chunks arrived')
            finally:
                # Preserve original errors (especially FloodWait) if library cleanup fails.
                try:
                    await stream.close()
                except Exception as exc:
                    log.warning('Download stream cleanup failed: %s', type(exc).__name__)

        tasks = [asyncio.create_task(fetch(i)) for i in range(workers)]
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise
        if received != total:
            raise OSError('Incomplete parallel download')


async def download_media(client, message, path, total, progress, workers=4,
                         threshold=32 * 1024 * 1024):
    workers = max(1, min(8, workers))
    if workers > 1 and total >= threshold and getattr(message, 'document', None) and hasattr(os, 'pwrite'):
        try:
            await parallel_download(client, message.media, path, total, progress, workers)
            return f'parallel ({workers} requests)'
        except Exception as exc:
            # Unsupported iterators/CDN redirects and stale refs use Telethon's
            # normal downloader. Never evade flood waits or retry disk/network failures here.
            compatible_fallback = isinstance(exc, (AttributeError, TypeError, ValueError,
                                                   errors.FileReferenceExpiredError,
                                                   errors.FilerefUpgradeNeededError))
            if not compatible_fallback and type(exc).__name__ != '_CdnRedirect':
                raise
            log.warning('Parallel download unavailable (%s); using standard downloader', type(exc).__name__)
            path.unlink(missing_ok=True)
            progress(0, total)
    await client.download_media(message, file=str(path), progress_callback=progress)
    return 'standard'
