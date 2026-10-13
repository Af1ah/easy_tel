import asyncio
import hashlib
import json
import math
import re
from pathlib import Path
from .retry import PermanentError, RcloneError


def safe_name(name: str) -> str:
    name = name.replace('\\', '/').rsplit('/', 1)[-1]
    name = re.sub(r'[^\w.() -]', '_', name).strip(' .')
    return name[:180] or 'file.bin'


async def md5(path: Path) -> str:
    def digest():
        h = hashlib.md5()
        with path.open('rb') as stream:
            for block in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                h.update(block)
        return h.hexdigest()
    return await asyncio.to_thread(digest)


def parse_stats(line):
    """Prefer per-file JSON stats; support text stats from older rclone versions."""
    try:
        record = json.loads(line)
    except (ValueError, TypeError):
        record = {'msg': line}
    if not isinstance(record, dict):
        return None
    stats = record.get('stats')
    if isinstance(stats, dict):
        active = stats.get('transferring') or []
        item = active[0] if active else stats
        try:
            done = float(item.get('bytes', 0))
            total = float(item.get('size', stats.get('totalBytes', 0)))
            speed = float(item.get('speedAvg', item.get('speed', stats.get('speed', 0))))
            eta = item.get('eta', stats.get('eta'))
            eta = float(eta) if eta is not None else None
            if total > 0 and all(math.isfinite(v) and v >= 0 for v in (done, total, speed)):
                return min(done, total), total, speed, eta
        except (ValueError, TypeError, AttributeError):
            pass
    # --stats-one-line, without dates. No file names or arbitrary log lines matched.
    match = re.search(r'(?:Transferred:\s*)?([\d.]+)\s*(\w*)\s*/\s*([\d.]+)\s*(\w*),\s*[\d.]+%,\s*([\d.]+)\s*(\w*)/s,\s*ETA\s+([^\s,]+)', str(record.get('msg', '')))
    if not match:
        return None
    units = {'':1, 'B':1, 'Bytes':1, 'KiB':1024, 'MiB':1024**2, 'GiB':1024**3,
             'TiB':1024**4, 'kBytes':1000, 'MBytes':1000**2, 'GBytes':1000**3}
    try:
        done = float(match[1]) * units[match[2]]
        total = float(match[3]) * units[match[4]]
        speed = float(match[5]) * units[match[6]]
        return min(done, total), total, speed, (total-done)/speed if speed > 0 else None
    except (KeyError, ValueError):
        return None


async def rclone(*args: str, progress=None) -> str:
    process = await asyncio.create_subprocess_exec(
        'rclone', *args, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_task = asyncio.create_task(process.stdout.read())
    try:
        while line := await process.stderr.readline():
            if progress:
                stats = parse_stats(line.decode(errors='replace'))
                if stats:
                    progress(*stats)
        stdout = await stdout_task
        await process.wait()
    finally:
        if process.returncode is None:
            try:
                process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(process.wait(), 5)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        stdout_task.cancel()
        await asyncio.gather(stdout_task, return_exceptions=True)
    if process.returncode:
        # Diagnostics can contain sensitive paths; retain only command and exit code.
        raise RcloneError(args[0], process.returncode)
    return stdout.decode()


async def upload_verified(path: Path, target: str, progress=None, verifying=None) -> None:
    await rclone('copyto', str(path), target, '--checksum', '--immutable',
                 '--transfers', '1', '--drive-chunk-size', '64M',
                 '--use-json-log', '--stats', '2s', '--stats-one-line',
                 '--stats-log-level', 'NOTICE', '--retries', '1',
                 '--low-level-retries', '3', '--contimeout', '15s', '--timeout', '2m',
                 progress=progress)
    if verifying:
        verifying()
    result = json.loads(await rclone('lsjson', target, '--stat', '--hash',
                                    '--contimeout', '15s', '--timeout', '2m'))
    hashes = {key.lower(): value for key, value in result.get('Hashes', {}).items()}
    remote_hash = hashes.get('md5', '')
    if result.get('Size') != path.stat().st_size or remote_hash.lower() != await md5(path):
        raise PermanentError('Drive size/checksum verification failed; local file retained')
    path.unlink()
