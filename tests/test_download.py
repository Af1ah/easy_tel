import asyncio
import datetime
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from telethon import TelegramClient, errors, types
from telethon.sessions import MemorySession
from easy_tel.download import CHUNK_SIZE, download_media, parallel_download
from easy_tel.retry import retry_delay
from easy_tel.worker import transfer_job


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    def client_and_message(self, payload):
        session = MemorySession()
        session.set_dc(2, '127.0.0.1', 443)
        # Test fixture only; no login or socket connection is performed.
        client = TelegramClient(session, 1, 'test fixture')
        document = types.Document(id=1, access_hash=1, file_reference=b'fixture',
            date=datetime.datetime.now(datetime.timezone.utc), mime_type='application/octet-stream',
            size=len(payload), dc_id=2, attributes=[])
        media = types.MessageMediaDocument(document=document)
        message = SimpleNamespace(document=document, media=media, file=SimpleNamespace(size=len(payload)))
        self.active = self.maximum = 0
        self.offsets = []
        async def call(sender, request):
            self.active += 1
            self.maximum = max(self.maximum, self.active)
            self.offsets.append(request.offset)
            try:
                # Deliberately deliver chunks out of order.
                await asyncio.sleep(.005 * (4 - (request.offset//CHUNK_SIZE) % 4))
                return types.upload.File(type=types.storage.FileUnknown(), mtime=0,
                    bytes=payload[request.offset:request.offset + request.limit])
            finally:
                self.active -= 1
        client._call = AsyncMock(side_effect=call)
        client.download_media = AsyncMock()
        return client, message

    async def test_real_telethon_iterators_parallel_output_and_concurrency(self):
        payload = bytes(range(251)) * 11000 + b'tail'
        client, message = self.client_and_message(payload)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'file.part'
            progress = Mock()
            mode = await download_media(client, message, path, len(payload), progress, workers=4, threshold=0)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(self.maximum, 4)
            self.assertIn('parallel', mode)
            self.assertEqual(progress.call_args.args, (len(payload), len(payload)))
            self.assertEqual(sorted(self.offsets), list(range(0, len(payload), CHUNK_SIZE)))
            client.download_media.assert_not_awaited()

    async def test_parallel_tiny_file_limits_workers_and_short_writes(self):
        payload = b'payload' * 100
        client, message = self.client_and_message(payload)
        import os
        original = os.pwrite
        def short_write(fd, data, offset):
            return original(fd, data[:100], offset)
        with tempfile.TemporaryDirectory() as folder, patch('easy_tel.download.os.pwrite', side_effect=short_write):
            path = Path(folder)/'file.part'
            await parallel_download(client, message.media, path, len(payload), Mock(), 8)
            self.assertEqual(path.read_bytes(), payload)
            self.assertEqual(self.maximum, 1)

    async def test_small_files_use_standard_downloader(self):
        client, message = self.client_and_message(b'payload')
        with tempfile.TemporaryDirectory() as folder:
            mode = await download_media(client, message, Path(folder)/'file.part', 7, Mock())
            self.assertEqual(mode, 'standard')
            client.download_media.assert_awaited_once()
            client._call.assert_not_awaited()

    async def test_unsupported_parallel_falls_back(self):
        client, message = self.client_and_message(b'payload')
        client.iter_download = Mock(side_effect=TypeError('unsupported fixture'))
        async def sequential(message, file, progress_callback):
            Path(file).write_bytes(b'payload')
        client.download_media.side_effect = sequential
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'file.part'
            mode = await download_media(client, message, path, 7, Mock(), threshold=0)
            self.assertEqual(path.read_bytes(), b'payload')
            self.assertEqual(mode, 'standard')
            client.download_media.assert_awaited_once()

    async def test_premium_wait_is_respected_not_bypassed(self):
        payload = b'x' * (CHUNK_SIZE * 4)
        client, message = self.client_and_message(payload)
        client._call.side_effect = errors.FloodPremiumWaitError(request=None, capture=120)
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(errors.FloodPremiumWaitError) as raised:
                await download_media(client, message, Path(folder)/'file.part', len(payload), Mock(), threshold=0)
            self.assertEqual(retry_delay(raised.exception, 1), 120)
            client.download_media.assert_not_awaited()

    async def test_failed_worker_cancels_other_workers_before_file_close(self):
        payload = b'x' * (CHUNK_SIZE * 4)
        client, message = self.client_and_message(payload)
        cancelled = []
        async def failing(sender, request):
            if request.offset == CHUNK_SIZE:
                await asyncio.sleep(.01)
                raise ConnectionError()
            try:
                await asyncio.sleep(5)
            except asyncio.CancelledError:
                cancelled.append(request.offset)
                raise
        client._call.side_effect = failing
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(ConnectionError):
                await download_media(client, message, Path(folder)/'file.part', len(payload), Mock(), threshold=0)
            self.assertEqual(len(cancelled), 3)
            client.download_media.assert_not_awaited()

    async def test_preallocated_size_does_not_hide_missing_chunks(self):
        client, message = self.client_and_message(b'x' * CHUNK_SIZE * 4)
        client._call.return_value = types.upload.File(type=types.storage.FileUnknown(), mtime=0, bytes=b'')
        client._call.side_effect = None
        with tempfile.TemporaryDirectory() as folder:
            with self.assertRaises(OSError):
                await download_media(client, message, Path(folder)/'file.part', CHUNK_SIZE*4, Mock(), threshold=0)
            client.download_media.assert_not_awaited()

    async def test_job_retry_reduces_parallel_concurrency(self):
        payload = b'x' * (CHUNK_SIZE * 4 + 31)
        client, message = self.client_and_message(payload)
        client.get_messages = AsyncMock(return_value=message)
        job = {'id': 1, 'chat': 123, 'message': 456, 'name': 'file',
               'size': len(payload), 'attempts': 1}
        with tempfile.TemporaryDirectory() as folder, patch.dict(os.environ,
                {'TEL_DOWNLOAD_WORKERS': '4', 'TEL_PARALLEL_MIN_BYTES': '0'}):
            with patch('easy_tel.worker.upload_verified', AsyncMock()) as upload:
                await transfer_job(client, job, Path(folder), 0, 'gdrive:easy_tel', Mock())
                self.assertEqual(self.maximum, 2)
                self.assertEqual(upload.call_args.args[0].read_bytes(), payload)
                self.assertEqual(upload.call_args.args[1], 'gdrive:easy_tel/1-file')
