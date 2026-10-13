import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from telethon import errors
from easy_tel.progress import Progress
from easy_tel.retry import PermanentError, RcloneError, retry_delay
from easy_tel.store import Store
from easy_tel.transfer import parse_stats, rclone
from easy_tel.worker import transfer_job


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    def reporter(self, **kwargs):
        self.clock = Mock(return_value=100)
        self.client = SimpleNamespace(send_message=AsyncMock(return_value=SimpleNamespace(id=42)),
                                      edit_message=AsyncMock())
        return Progress(self.client, 123, 1, 'file.zip', 1000, clock=self.clock, **kwargs)

    async def test_edits_one_message_and_throttles_callbacks(self):
        progress = self.reporter()
        progress.phase_update('downloading')
        await progress.emit()
        progress.update(500)
        self.clock.return_value = 102
        await progress.emit()
        self.client.edit_message.assert_not_awaited()
        self.clock.return_value = 106
        await progress.emit()
        self.client.send_message.assert_awaited_once()
        self.client.edit_message.assert_awaited_once()
        self.assertIn('50.0%', self.client.edit_message.call_args.args[2])
        self.assertIn('ETA:', self.client.edit_message.call_args.args[2])
        await progress.emit(force=True)
        self.client.edit_message.assert_awaited_once()

    async def test_deleted_message_falls_back_to_new_message(self):
        save = Mock()
        progress = self.reporter(message_id=10, save_message=save)
        self.client.edit_message.side_effect = errors.MessageIdInvalidError(request=None)
        await progress.emit()
        self.assertEqual(progress.message_id, 42)
        self.client.send_message.assert_awaited_once()
        save.assert_called_once_with(42)

    async def test_flood_wait_does_not_send_fallback_or_block_transfer(self):
        progress = self.reporter(message_id=10)
        self.client.edit_message.side_effect = errors.FloodWaitError(request=None, capture=30)
        await progress.emit()
        self.clock.return_value = 120
        progress.update(500)
        await progress.emit(force=True)
        self.client.edit_message.assert_awaited_once()
        self.client.send_message.assert_not_awaited()
        self.client.edit_message.side_effect = None
        self.clock.return_value = 131
        await progress.emit()
        self.assertEqual(self.client.edit_message.await_count, 2)

    async def test_transient_edit_error_retries_without_new_message(self):
        progress = self.reporter(message_id=10)
        self.client.edit_message.side_effect = ConnectionError()
        await progress.emit()
        self.clock.return_value = 106
        self.client.edit_message.side_effect = None
        await progress.emit()
        self.assertEqual(self.client.edit_message.await_count, 2)
        self.client.send_message.assert_not_awaited()

    async def test_not_modified_is_success(self):
        progress = self.reporter(message_id=10)
        self.client.edit_message.side_effect = errors.MessageNotModifiedError(request=None)
        await progress.emit()
        await progress.emit(force=True)
        self.client.edit_message.assert_awaited_once()

    def test_json_stats_uses_per_file_not_retried_aggregate(self):
        result = parse_stats(json.dumps({'stats': {'bytes': 1500, 'totalBytes': 2000,
                            'transferring': [{'bytes': 500, 'size': 1000, 'speedAvg': 100, 'eta': 5}]}}))
        self.assertEqual(result, (500, 1000, 100, 5))

    def test_old_rclone_text_fallback_and_malformed_lines(self):
        result = parse_stats(json.dumps({'msg': 'Transferred: 1 MiB / 2 MiB, 50%, 512 KiB/s, ETA 2s'}))
        self.assertEqual(result, (1024**2, 2*1024**2, 512*1024, 2))
        for line in ('not JSON', '{}', '[]', '{"stats":{"bytes":"bad"}}'):
            self.assertIsNone(parse_stats(line))

    async def test_actual_rclone_progress_stream(self):
        with tempfile.TemporaryDirectory() as folder:
            source, target = Path(folder)/'source', Path(folder)/'target'
            source.write_bytes(b'x' * 1024 * 1024)
            callback = Mock()
            await rclone('copyto', str(source), str(target), '--bwlimit', '512k',
                         '--use-json-log', '--stats', '100ms', '--stats-one-line',
                         '--stats-log-level', 'NOTICE', progress=callback)
            self.assertTrue(callback.called, 'No progress records parsed from the installed rclone')
            self.assertEqual(target.read_bytes(), source.read_bytes())

    async def test_buttons_can_be_added_without_changing_terminal_text(self):
        from telethon import Button
        progress = self.reporter(message_id=10)
        progress.phase_update('uploaded and verified')
        await progress.emit()
        progress.buttons = [[Button.url('Download','https://drive.google.com/uc?id=test')]]
        await progress.emit(force=True)
        self.assertEqual(self.client.edit_message.await_count,2)
        self.assertEqual(self.client.edit_message.call_args.kwargs['buttons'],progress.buttons)

    async def test_buttons_changed_during_edit_are_not_lost(self):
        from telethon import Button
        progress = self.reporter(message_id=10)
        entered, release = asyncio.Event(), asyncio.Event()
        async def delayed_edit(*args, **kwargs):
            entered.set()
            await release.wait()
        self.client.edit_message.side_effect = delayed_edit
        task = asyncio.create_task(progress.emit())
        await entered.wait()
        progress.buttons = [[Button.url('Download','https://drive.google.com/uc?id=test')]]
        release.set()
        await task
        await progress.emit(force=True)
        self.assertEqual(self.client.edit_message.await_count,2)
        self.assertEqual(progress.last_buttons,progress.buttons)


class RetryTests(unittest.IsolatedAsyncioTestCase):
    def test_retry_policy(self):
        self.assertIsNone(retry_delay(PermanentError(), 1))
        self.assertIsNone(retry_delay(RcloneError('copyto', 7), 1))
        self.assertIsNone(retry_delay(RcloneError('copyto', 2), 1))
        self.assertIsNone(retry_delay(RcloneError('copyto', 6), 1))
        self.assertGreaterEqual(retry_delay(RcloneError('copyto', 5), 2), 30)
        self.assertGreaterEqual(retry_delay(ConnectionError(), 1), 15)
        self.assertEqual(retry_delay(errors.FloodWaitError(None, 600), 1), 600)

    def test_legacy_database_migration_and_persistent_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'jobs.db'
            db = sqlite3.connect(path)
            db.execute('CREATE TABLE jobs (id INTEGER PRIMARY KEY, chat INTEGER, message INTEGER, name TEXT, size INTEGER, status TEXT, UNIQUE(chat,message))')
            db.execute("INSERT INTO jobs VALUES (1,123,456,'file',100,'running')")
            db.commit()
            db.close()
            store = Store(path)
            self.assertEqual(store.next()['attempts'], 0)
            store.begin(1)
            store.defer(1, 600, 'ConnectionError')
            store.save_progress(1, 42)
            self.assertIsNone(store.next())
            self.assertEqual(store.pending_count(), 1)
            store.db.close()
            store = Store(path)
            row = store.next(now=10**12)
            self.assertEqual(row['attempts'], 1)
            self.assertEqual(row['progress_message'], 42)
            store.status(1, 'failed')
            self.assertFalse(store.retry(1, 999))
            self.assertTrue(store.retry(1, 123))
            self.assertEqual(store.next()['id'], 1)
            self.assertEqual(store.next()['attempts'], 0)
            self.assertFalse(store.retry(1, 123))
            store.db.close()

    async def test_failed_upload_retry_reuses_download_and_target(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            job = {'id': 1, 'chat': 123, 'message': 456, 'name': 'file', 'size': 7}
            async def download(message, file, progress_callback):
                Path(file).write_bytes(b'payload')
                progress_callback(7, 7)
            client = SimpleNamespace(get_messages=AsyncMock(return_value=SimpleNamespace(file=True)),
                                     download_media=AsyncMock(side_effect=download))
            progress = Mock()
            upload = AsyncMock(side_effect=[RcloneError('copyto', 5), None])
            with patch('easy_tel.worker.upload_verified', upload):
                with self.assertRaises(RcloneError):
                    await transfer_job(client, job, state, 0, 'gdrive:easy_tel', progress)
                await transfer_job(client, job, state, 0, 'gdrive:easy_tel', progress)
            client.download_media.assert_awaited_once()
            self.assertEqual(upload.call_args_list[0].args, upload.call_args_list[1].args)
            self.assertFalse((state/'downloads/1/file.part').exists())

    async def test_incomplete_download_is_not_used_on_retry(self):
        with tempfile.TemporaryDirectory() as folder:
            state = Path(folder)
            job = {'id': 1, 'chat': 123, 'message': 456, 'name': 'file', 'size': 7}
            calls = 0
            async def download(message, file, progress_callback):
                nonlocal calls
                calls += 1
                Path(file).write_bytes(b'partial' if calls == 1 else b'payload')
                if calls == 1:
                    raise ConnectionError()
            client = SimpleNamespace(get_messages=AsyncMock(return_value=SimpleNamespace(file=True)),
                                     download_media=AsyncMock(side_effect=download))
            with patch('easy_tel.worker.upload_verified', AsyncMock()) as upload:
                with self.assertRaises(ConnectionError):
                    await transfer_job(client, job, state, 0, 'gdrive:easy_tel', Mock())
                upload.assert_not_awaited()
                await transfer_job(client, job, state, 0, 'gdrive:easy_tel', Mock())
                upload.assert_awaited_once()
            self.assertEqual(calls, 2)
