import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch
from easy_tel.store import Store
from easy_tel.transfer import safe_name, upload_verified

class TransferTests(unittest.IsolatedAsyncioTestCase):
    async def test_verified_upload_removes_local_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'file'
            path.write_bytes(b'payload')
            metadata = {'Size': 7, 'Hashes': {'md5': hashlib.md5(b'payload').hexdigest()}}
            runner = AsyncMock(side_effect=['', json.dumps(metadata)])
            with patch('easy_tel.transfer.rclone', runner):
                await upload_verified(path, 'gdrive:easy_tel/1-file')
            self.assertFalse(path.exists())
            self.assertIn('--immutable', runner.call_args_list[0].args)

    async def test_checksum_mismatch_keeps_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'file'
            path.write_bytes(b'payload')
            runner = AsyncMock(side_effect=['', json.dumps({'Size': 7, 'Hashes': {'MD5': 'wrong'}})])
            with patch('easy_tel.transfer.rclone', runner):
                with self.assertRaises(RuntimeError):
                    await upload_verified(path, 'gdrive:easy_tel/file')
            self.assertTrue(path.exists())

    async def test_upload_failure_keeps_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'file'
            path.write_bytes(b'payload')
            with patch('easy_tel.transfer.rclone', AsyncMock(side_effect=RuntimeError('upload failed'))):
                with self.assertRaises(RuntimeError):
                    await upload_verified(path, 'gdrive:easy_tel/file')
            self.assertTrue(path.exists())

    def test_names_cannot_escape_directory(self):
        self.assertEqual(safe_name('../../file.zip'), 'file.zip')
        self.assertEqual(safe_name('..\\..\\file.zip'), 'file.zip')
        self.assertEqual(safe_name('..'), 'file.bin')

    def test_queue_recovers_and_deduplicates(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'jobs.db'
            store = Store(path)
            job = store.add(123, 456, 'file.zip', 100)
            self.assertEqual(store.add(123, 456, 'file.zip', 100)['id'], job['id'])
            self.assertEqual(store.pending_count(), 1)
            store.status(job['id'], 'running')
            store.db.close()
            store = Store(path)
            self.assertEqual(store.next()['status'], 'queued')
            store.status(job['id'], 'done')
            self.assertIsNone(store.next())
            store.db.close()

    async def test_real_rclone_local_transfer(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.bin'
            target = Path(folder) / 'destination.bin'
            payload = b'local integration payload' * 1000
            path.write_bytes(payload)
            await upload_verified(path, str(target))
            self.assertEqual(target.read_bytes(), payload)
            self.assertFalse(path.exists())
