import shutil
import logging
import os
import time
from .download import download_media
from .retry import PermanentError
from .transfer import upload_verified


log = logging.getLogger('easy_tel.worker')


async def transfer_job(client, job, state, reserve, destination, progress):
    folder = state / 'downloads' / str(job['id'])
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = folder / job['name']
    partial = folder / (job['name'] + '.part')
    if not path.is_file() or path.stat().st_size != job['size']:
        partial.unlink(missing_ok=True)
        if shutil.disk_usage(state).free < job['size'] + reserve:
            raise PermanentError('Insufficient disk space')
        message = await client.get_messages(job['chat'], ids=job['message'])
        if not message or not message.file:
            raise PermanentError('Source message no longer available')
        progress.phase_update('downloading')
        configured_workers = max(1, min(8, int(os.environ.get('TEL_DOWNLOAD_WORKERS', '4'))))
        previous_attempts = job['attempts'] if 'attempts' in job.keys() else 0
        workers = max(1, configured_workers // 2**min(3, previous_attempts))
        started = time.monotonic()
        mode = await download_media(client, message, partial, job['size'], progress.update,
                                    workers=workers,
                                    threshold=int(os.environ.get('TEL_PARALLEL_MIN_BYTES', str(32*1024*1024))))
        elapsed = max(.001, time.monotonic() - started)
        log.info('Job %s download: mode=%s, elapsed=%.1fs, rate=%.2f MiB/s',
                 job['id'], mode, elapsed, job['size']/elapsed/1024**2)
        if not partial.is_file() or partial.stat().st_size != job['size']:
            raise OSError('Downloaded size mismatch')
        partial.replace(path)
    progress.phase_update('uploading to Drive')
    target = f"{destination}/{job['id']}-{job['name']}"
    await upload_verified(path, target, progress=progress.update,
                          verifying=lambda: progress.phase_update('verifying Drive checksum'))
