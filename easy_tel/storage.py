"""Owner-scoped Drive links, file browser, and confirmed single-file deletion."""
import asyncio
import json
import re
import secrets
import time
from urllib.parse import parse_qs, urlencode, urlsplit
from telethon import Button, errors
from .progress import size_text
from .retry import RcloneError
from .transfer import rclone


class StorageError(RuntimeError):
    pass


def download_url(public_url):
    parts = urlsplit(public_url.strip())
    if parts.scheme != 'https' or parts.hostname not in ('drive.google.com', 'docs.google.com') or parts.username:
        raise StorageError('Drive returned an unexpected link')
    query = parse_qs(parts.query)
    file_id = query.get('id', [None])[0]
    match = re.search(r'/d/([A-Za-z0-9_-]+)', parts.path)
    file_id = file_id or (match[1] if match else None)
    if not file_id or not re.fullmatch(r'[A-Za-z0-9_-]+', file_id):
        raise StorageError('Drive link has no file ID')
    params = {'export':'download', 'id':file_id}
    if query.get('resourcekey'):
        params['resourcekey'] = query['resourcekey'][0]
    return 'https://drive.google.com/uc?' + urlencode(params)


class Storage:
    def __init__(self, store, destination, clock=time.monotonic):
        self.store, self.destination, self.clock = store, destination, clock
        self.confirmations = {}
        self.lock = asyncio.Lock()
        self.quota_text = ''
        self.quota_at = float('-inf')

    def owned(self, job_id, chat):
        row = self.store.owned_file(job_id, chat)
        if row is None:
            raise StorageError('This file is unavailable or belongs to another user')
        if not row['remote_path']:
            # Legacy uploads used the current folder; validate them before any action.
            self.store.prepare_remote(job_id, self.destination)
            row = self.store.owned_file(job_id, chat)
        target = row['remote_path']
        expected = f"{job_id}-{row['name']}"
        if not target.startswith('gdrive:') or target.rsplit('/', 1)[-1] != expected:
            raise StorageError('Invalid recorded file path')
        return row

    async def checked(self, job_id, chat):
        row = self.owned(job_id, chat)
        result = json.loads(await rclone('lsjson', row['remote_path'], '--stat', '--hash',
                                        '--contimeout', '15s', '--timeout', '30s'))
        hashes = {key.lower(): value for key, value in result.get('Hashes', {}).items()}
        if result.get('IsDir') or result.get('Size') != row['size']:
            raise StorageError('The Drive file changed; action refused')
        if row['remote_id'] and row['remote_id'] != result.get('ID'):
            raise StorageError('The Drive file was replaced; action refused')
        if row['remote_md5'] and row['remote_md5'].lower() != hashes.get('md5', '').lower():
            raise StorageError('The Drive file contents changed; action refused')
        self.store.remember_file(job_id, result)
        return self.owned(job_id, chat)

    async def link(self, job_id, chat):
        async with self.lock:
            row = await self.checked(job_id, chat)
            if row['download_url']:
                return row['download_url']
            raw = await rclone('link', row['remote_path'], '--contimeout', '15s', '--timeout', '30s')
            lines = raw.strip().splitlines()
            if not lines:
                raise StorageError('Drive did not return a link')
            url = download_url(lines[-1])
            if row['remote_id'] and parse_qs(urlsplit(url).query)['id'][0] != row['remote_id']:
                raise StorageError('Drive link points to a different file; action refused')
            self.store.save_link(job_id, url)
            return url

    def file_buttons(self, job_id, url=None, page=0):
        download = Button.url('Download', url) if url else Button.inline('Get download link', f'link:{job_id}:{page}'.encode())
        return [[download, Button.inline('Delete', f'delete:{job_id}:{page}'.encode())],
                [Button.inline('Manage storage', f'storage:{page}'.encode())]]

    async def quota(self):
        if self.clock() - self.quota_at < 60:
            return self.quota_text
        self.quota_at = self.clock()
        try:
            result = json.loads(await asyncio.wait_for(rclone('about', 'gdrive:', '--json'), 10))
            values = [f"{label}: {size_text(result[key])}" for key, label in
                      [('used','used'), ('free','free'), ('total','total'), ('trashed','trash')]
                      if isinstance(result.get(key), (int,float)) and result[key] >= 0]
            self.quota_text = 'Drive account quota — ' + ', '.join(values) if values else ''
        except Exception:
            self.quota_text = 'Drive account quota unavailable; uploaded-file totals shown below.'
        return self.quota_text

    async def page(self, chat, page=0):
        count, total = self.store.file_totals(chat)
        page = min(max(0, page), max(0, (count-1)//5))
        rows = self.store.files(chat, page)
        lines = ['Your uploaded files', f'{count} files · {size_text(total)}', await self.quota()]
        buttons = []
        for row in rows:
            lines.append(f"#{row['id']} · {row['name']} · {size_text(row['size'])}")
            buttons.append([Button.inline(f"Open #{row['id']}", f"link:{row['id']}:{page}".encode()),
                            Button.inline(f"Delete #{row['id']}", f"delete:{row['id']}:{page}".encode())])
        if not rows:
            lines.append('No uploaded files to manage.')
        nav = []
        if page:
            nav.append(Button.inline('Previous', f'storage:{page-1}'.encode()))
        if (page+1)*5 < count:
            nav.append(Button.inline('Next', f'storage:{page+1}'.encode()))
        nav.append(Button.inline('Refresh', f'storage:{page}'.encode()))
        buttons.append(nav)
        lines.append(f'Page {page+1}/{max(1,(count+4)//5)}. Delete offers trash or confirmed permanent removal.')
        return '\n'.join(line for line in lines if line), buttons

    def confirmation(self, job_id, chat, page, permanent=False):
        row = self.owned(job_id, chat)
        now = self.clock()
        self.confirmations = {token: value for token, value in self.confirmations.items()
                              if value[2] > now and value[:2] != (job_id, chat)}
        token = secrets.token_urlsafe(12)
        self.confirmations[token] = (job_id, chat, now+120, permanent)
        if permanent:
            text = f"Permanently delete #{job_id} — {row['name']} ({size_text(row['size'])})?\nThis cannot be recovered from Drive trash."
            buttons = [[Button.inline('Confirm permanent deletion', f'confirm:{token}:{page}'.encode())]]
        else:
            text = f"Move #{job_id} — {row['name']} ({size_text(row['size'])}) to Drive trash?\nTrash still counts toward quota; shared access may remain until permanent deletion."
            buttons = [[Button.inline('Confirm move to trash', f'confirm:{token}:{page}'.encode())],
                       [Button.inline('Permanently delete instead', f'permanent:{job_id}:{page}'.encode())]]
        buttons.append([Button.inline('Cancel', f'cancel:{token}:{page}'.encode())])
        return text, buttons

    async def delete(self, token, chat):
        async with self.lock:
            confirmation = self.confirmations.get(token)
            if not confirmation or confirmation[1] != chat or confirmation[2] <= self.clock():
                raise StorageError('Confirmation expired or invalid. Open /storage and try again.')
            job_id = confirmation[0]
            self.confirmations.pop(token)
            try:
                row = await self.checked(job_id, chat)
            except RcloneError as exc:
                if exc.code != 4:
                    raise
                self.store.status(job_id, 'deleted')
                return 'The file is already absent from its recorded Drive location.'
            self.store.status(job_id, 'deleting')
            try:
                await rclone('deletefile', row['remote_path'],
                             '--drive-use-trash=false' if confirmation[3] else '--drive-use-trash=true',
                             '--retries', '1', '--contimeout', '15s', '--timeout', '30s')
            except BaseException:
                self.store.status(job_id, 'done')
                raise
            self.store.status(job_id, 'deleted')
            self.store.save_link(job_id, None)
            self.quota_at = float('-inf')
            return (f'File #{job_id} permanently deleted.' if confirmation[3]
                    else f'File #{job_id} moved to Drive trash. Trash still uses quota and may preserve shared access.')

    async def callback(self, event):
        try:
            parts = event.data.decode('ascii').split(':')
            action = parts[0]
            page = max(0, int(parts[-1]))
            # Acknowledge promptly; Drive calls can take seconds.
            await event.answer()
            if action == 'storage' and len(parts) == 2:
                text, buttons = await self.page(event.chat_id, page)
            elif action == 'link' and len(parts) == 3:
                job_id = int(parts[1])
                url = await asyncio.wait_for(self.link(job_id, event.chat_id), 45)
                row = self.owned(job_id, event.chat_id)
                text = f"#{job_id} — {row['name']}\n{size_text(row['size'])}\nAnyone with the download link can access this file."
                buttons = self.file_buttons(job_id, url, page)
            elif action in ('delete', 'permanent') and len(parts) == 3:
                text, buttons = self.confirmation(int(parts[1]), event.chat_id, page, permanent=action == 'permanent')
            elif action == 'confirm' and len(parts) == 3:
                result = await asyncio.wait_for(self.delete(parts[1], event.chat_id), 60)
                text, buttons = await self.page(event.chat_id, page)
                text = result + '\n\n' + text
            elif action == 'cancel' and len(parts) == 3:
                value = self.confirmations.get(parts[1])
                if value and value[1] == event.chat_id:
                    self.confirmations.pop(parts[1])
                text, buttons = await self.page(event.chat_id, page)
            else:
                raise StorageError('Invalid action')
            try:
                await event.edit(text, buttons=buttons, parse_mode=None, link_preview=False)
            except errors.MessageNotModifiedError:
                pass
            except (errors.MessageIdInvalidError, errors.MessageAuthorRequiredError,
                    errors.MessageEditTimeExpiredError):
                await event.respond(text, buttons=buttons, parse_mode=None, link_preview=False)
        except (ValueError, UnicodeError, StorageError) as exc:
            message = str(exc) if isinstance(exc, StorageError) else 'Invalid action'
            await event.respond(message, parse_mode=None)
        except Exception:
            await event.respond('Storage action failed. Check rclone sharing/deletion permissions and Drive access, then try /storage again.', parse_mode=None)
