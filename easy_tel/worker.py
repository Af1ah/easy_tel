import shutil
from .retry import PermanentError
from .transfer import upload_verified


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
        await client.download_media(message, file=str(partial), progress_callback=progress.update)
        if not partial.is_file() or partial.stat().st_size != job['size']:
            raise OSError('Downloaded size mismatch')
        partial.replace(path)
    progress.phase_update('uploading to Drive')
    target = f"{destination}/{job['id']}-{job['name']}"
    await upload_verified(path, target, progress=progress.update,
                          verifying=lambda: progress.phase_update('verifying Drive checksum'))
