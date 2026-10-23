import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
from easy_tel.storage import Storage, StorageError, download_url
from easy_tel.store import Store
from easy_tel.retry import RcloneError


class StorageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.folder.name)/'jobs.db')
        self.clock = Mock(return_value=100)
        self.storage = Storage(self.store, 'gdrive:easy_tel', clock=self.clock)
        self.meta = {'ID':'drive-file-id', 'Size':7, 'IsDir':False, 'Hashes':{'md5':'abc'}}
        self.job_id = self.store.add(123, 456, 'file.bin', 7)['id']
        self.store.prepare_remote(self.job_id, 'gdrive:easy_tel')
        self.store.complete(self.job_id, self.meta)

    def tearDown(self):
        self.store.db.close()
        self.folder.cleanup()

    def token(self, permanent=False):
        text, buttons = self.storage.confirmation(self.job_id, 123, 0, permanent)
        return buttons[0][0].data.decode().split(':')[1]

    async def test_link_creates_and_caches_download_button(self):
        runner = AsyncMock(side_effect=[json.dumps(self.meta), 'https://drive.google.com/open?id=drive-file-id\n', json.dumps(self.meta)])
        with patch('easy_tel.storage.rclone', runner):
            url = await self.storage.link(self.job_id, 123)
            self.assertEqual(await self.storage.link(self.job_id, 123), url)
        self.assertIn('export=download', url)
        self.assertEqual([call.args[0] for call in runner.call_args_list], ['lsjson','link','lsjson'])
        buttons = self.storage.file_buttons(self.job_id, url)
        self.assertEqual(buttons[0][0].url, url)

    async def test_other_user_cannot_link_confirm_or_delete(self):
        with patch('easy_tel.storage.rclone', AsyncMock()) as runner:
            with self.assertRaises(StorageError):
                await self.storage.link(self.job_id, 999)
            with self.assertRaises(StorageError):
                self.storage.confirmation(self.job_id, 999, 0)
            token = self.token()
            with self.assertRaises(StorageError):
                await self.storage.delete(token, 999)
            runner.assert_not_awaited()

    async def test_delete_requires_unexpired_confirmation_and_replay_fails(self):
        token = self.token()
        self.clock.return_value = 221
        with patch('easy_tel.storage.rclone', AsyncMock()) as runner:
            with self.assertRaises(StorageError):
                await self.storage.delete(token, 123)
            runner.assert_not_awaited()
        self.clock.return_value = 100
        token = self.token()
        runner = AsyncMock(side_effect=[json.dumps(self.meta), ''])
        with patch('easy_tel.storage.rclone', runner):
            await self.storage.delete(token, 123)
            with self.assertRaises(StorageError):
                await self.storage.delete(token, 123)
        self.assertEqual(self.store.job(self.job_id)['status'], 'deleted')
        self.assertEqual(self.store.file_totals(123), (0,0))
        self.assertEqual(runner.call_args.args[0], 'deletefile')
        self.assertEqual(runner.call_args.args[1], 'gdrive:easy_tel/1-file.bin')
        self.assertIn('--drive-use-trash=true', runner.call_args.args)

    async def test_permanent_delete_is_explicit_token_bound_choice(self):
        token = self.token(permanent=True)
        runner = AsyncMock(side_effect=[json.dumps(self.meta), ''])
        with patch('easy_tel.storage.rclone', runner):
            text = await self.storage.delete(token, 123)
        self.assertIn('permanently deleted', text)
        self.assertIn('--drive-use-trash=false', runner.call_args.args)

    async def test_remote_identity_mismatch_prevents_link_or_delete(self):
        changed = dict(self.meta, ID='some-other-file')
        with patch('easy_tel.storage.rclone', AsyncMock(return_value=json.dumps(changed))) as runner:
            with self.assertRaises(StorageError):
                await self.storage.link(self.job_id, 123)
            with self.assertRaises(StorageError):
                await self.storage.delete(self.token(), 123)
        self.assertTrue(all(call.args[0]=='lsjson' for call in runner.call_args_list))
        self.assertEqual(self.store.job(self.job_id)['status'], 'done')

    async def test_delete_failure_preserves_catalog_for_retry(self):
        runner = AsyncMock(side_effect=[json.dumps(self.meta), RcloneError('deletefile',5)])
        with patch('easy_tel.storage.rclone', runner):
            with self.assertRaises(RcloneError):
                await self.storage.delete(self.token(),123)
        self.assertEqual(self.store.job(self.job_id)['status'], 'done')
        self.assertEqual(self.store.file_totals(123), (1,7))

    async def test_link_failure_does_not_fail_verified_upload(self):
        runner = AsyncMock(side_effect=[json.dumps(self.meta), RcloneError('link',7)])
        with patch('easy_tel.storage.rclone', runner):
            with self.assertRaises(RcloneError):
                await self.storage.link(self.job_id,123)
        self.assertEqual(self.store.job(self.job_id)['status'], 'done')

    async def test_storage_paginates_owner_files_and_quota_failure_falls_back(self):
        for i in range(8):
            job_id = self.store.add(123,1000+i,f'file{i}',10)['id']
            self.store.complete(job_id,{})
        other = self.store.add(999,1,'private-other-user',100)['id']
        self.store.complete(other,{})
        with patch('easy_tel.storage.rclone', AsyncMock(side_effect=RcloneError('about',7))):
            text, buttons = await self.storage.page(123)
            self.assertIn('9 files',text)
            self.assertNotIn('private-other-user',text)
            self.assertIn('quota unavailable',text)
            self.assertEqual(len(self.store.files(123,0)),5)
            text, _ = await self.storage.page(123,1)
            self.assertIn('Page 2/2',text)

    async def test_cancelled_confirmation_cannot_delete(self):
        token = self.token()
        event = SimpleNamespace(data=f'cancel:{token}:0'.encode(),chat_id=123,
            answer=AsyncMock(),edit=AsyncMock(),respond=AsyncMock())
        with patch.object(self.storage,'quota',AsyncMock(return_value='')):
            await self.storage.callback(event)
        with patch('easy_tel.storage.rclone',AsyncMock()) as runner:
            with self.assertRaises(StorageError):
                await self.storage.delete(token,123)
            runner.assert_not_awaited()

    def test_recorded_destination_survives_configuration_change(self):
        self.store.prepare_remote(self.job_id,'gdrive:different-folder')
        self.assertEqual(self.store.job(self.job_id)['remote_path'],'gdrive:easy_tel/1-file.bin')

    def test_download_url_resource_key_and_untrusted_host(self):
        url = download_url('https://drive.google.com/file/d/abc123/view?resourcekey=key123')
        self.assertIn('resourcekey=key123',url)
        for bad in ('https://example.com/?id=abc','http://drive.google.com/?id=abc','https://drive.google.com/','https://user@drive.google.com/?id=abc'):
            with self.assertRaises(StorageError):
                download_url(bad)

    async def test_command_menu_adds_storage_and_keeps_existing_commands(self):
        from telethon import types, functions
        from easy_tel.__main__ import register_commands
        client = AsyncMock(side_effect=[[types.BotCommand('existing','Existing menu entry')],True])
        await register_commands(client)
        request = client.call_args.args[0]
        self.assertIsInstance(request,functions.bots.SetBotCommandsRequest)
        self.assertIn('storage',[command.command for command in request.commands])
        self.assertIn('existing',[command.command for command in request.commands])

    async def test_watch_button_and_successful_delete_revoke_local_media(self):
        from easy_tel.media import MediaLibrary
        source=Path(self.folder.name)/'movie.mkv'
        source.write_bytes(b'payload')
        movie=self.store.add(123,999,'movie.mkv',7)
        self.store.prepare_remote(movie['id'],'gdrive:easy_tel')
        self.store.complete(movie['id'],self.meta)
        library=MediaLibrary(self.folder.name,'https://watch.example.test',reserve=0)
        await library.register(dict(movie),source)
        self.storage.media=library
        buttons=self.storage.file_buttons(movie['id'])
        self.assertEqual(buttons[0][0].text,'Watch video')
        self.assertEqual(buttons[0][0].url,library.link(movie['id'],123))
        _,confirmation=self.storage.confirmation(movie['id'],123,0)
        token=confirmation[0][0].data.decode().split(':')[1]
        with patch('easy_tel.storage.rclone',AsyncMock(side_effect=[json.dumps(self.meta),''])):
            await self.storage.delete(token,123)
        self.assertIsNone(library.link(movie['id'],123))
