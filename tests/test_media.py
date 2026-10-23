import asyncio
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import unittest
from unittest.mock import patch
from aiohttp.test_utils import TestClient,TestServer
from easy_tel.media import MediaLibrary
from easy_tel.web import create_app
from media_fixtures import make_video


class MediaTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixtures=tempfile.TemporaryDirectory()
        cls.source=make_video(cls.fixtures.name)

    @classmethod
    def tearDownClass(cls): cls.fixtures.cleanup()

    async def asyncSetUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.library=MediaLibrary(self.temp.name,'http://127.0.0.1:8080',reserve=0)
        self.job={'id':1,'chat':123,'name':'A film.mkv'}
        await self.library.register(self.job,self.source)
        self.row=self.library.get(job=1)
        self.token=self.row['token']
        self.client=TestClient(TestServer(create_app(self.library,prepare=False)))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        self.temp.cleanup()

    async def test_source_ranges_head_and_invalid_ranges(self):
        url=f'/media/{self.token}/source'
        response=await self.client.get(url,headers={'Range':'bytes=10-29'})
        self.assertEqual(response.status,206)
        self.assertEqual(await response.read(),self.source.read_bytes()[10:30])
        self.assertIn('Content-Range',response.headers)
        response=await self.client.head(url)
        self.assertEqual(int(response.headers['Content-Length']),self.source.stat().st_size)
        self.assertEqual(await response.read(),b'')
        response=await self.client.get(url,headers={'Range':'bytes=999999999-1000000000'})
        self.assertEqual(response.status,416)

    async def test_unguessable_tokens_expiry_revocation_and_cleanup(self):
        self.assertGreaterEqual(len(self.token),43)
        self.assertEqual((await self.client.get('/api/'+'x'*43)).status,404)
        self.assertEqual((await self.client.get(f'/api/{self.token}')).status,200)
        original_folder=self.library.folder(1)
        self.library.revoke(1)
        self.assertEqual((await self.client.get(f'/media/{self.token}/source')).status,410)
        self.assertEqual((await self.client.get(f'/watch/{self.token}')).status,410)
        self.library.cleanup()
        self.assertFalse(original_folder.exists())
        self.assertIsNone(self.library.get(job=1))

    async def test_no_path_traversal_or_private_database_access(self):
        for path in ('library.sqlite3','../library.sqlite3','%2e%2e/library.sqlite3','video/../../library.sqlite3'):
            response=await self.client.get(f'/media/{self.token}/{path}')
            self.assertIn(response.status,(400,404))
        response=await self.client.get('/static/../media.py')
        self.assertEqual(response.status,404)

    async def test_hls_remux_audio_subtitles_and_actual_decode(self):
        await self.library.prepare(1)
        row=self.library.get(job=1)
        self.assertEqual(row['state'],'ready')
        metadata=json.loads(row['metadata'])
        self.assertEqual(metadata['mode'],'remuxed')
        self.assertEqual(len(metadata['audio']),2)
        self.assertEqual(len(metadata['subtitles']),2)
        response=await self.client.get(f'/media/{self.token}/master.m3u8')
        self.assertEqual(response.status,200)
        manifest=await response.text()
        self.assertIn('audio0/index.m3u8',manifest)
        self.assertIn('audio1/index.m3u8',manifest)
        self.assertIn('video/index.m3u8',manifest)
        subtitle=await self.client.get(f'/media/{self.token}/subtitle0.vtt')
        self.assertIn('English subtitle track',await subtitle.text())
        # Decode the resulting HLS package, not just its JSON metadata.
        result=await asyncio.create_subprocess_exec('ffmpeg','-v','error','-threads','2','-i',
             str(self.library.folder(1)/'master.m3u8'),'-t','1','-f','null','-',
             stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
        _,stderr=await result.communicate()
        self.assertEqual(result.returncode,0,stderr.decode()[:500])

    async def test_budget_rejects_registration_without_breaking_source(self):
        tiny=MediaLibrary(Path(self.temp.name)/'tiny','http://localhost:8080',limit=1,reserve=0)
        with self.assertRaises(RuntimeError):
            await tiny.register(self.job,self.source)
        self.assertTrue(self.source.exists())
        self.assertIsNone(tiny.get(job=1))

    async def test_safe_headers_no_api_secret_fields_and_health(self):
        response=await self.client.get(f'/watch/{self.token}')
        self.assertEqual(response.status,200)
        self.assertEqual(response.headers['Referrer-Policy'],'no-referrer')
        self.assertIn("frame-ancestors 'none'",response.headers['Content-Security-Policy'])
        data=await (await self.client.get(f'/api/{self.token}')).json()
        self.assertNotIn('owner',data)
        self.assertNotIn('root',data)
        self.assertNotIn('remote_path',data)
        self.assertEqual((await self.client.get('/healthz')).status,200)

    async def test_non_browser_codec_is_converted(self):
        folder=Path(self.temp.name)/'other';folder.mkdir()
        source=await asyncio.to_thread(make_video,folder,duration=2,codec='mpeg4')
        job=dict(self.job,id=2)
        await self.library.register(job,source)
        await self.library.prepare(2)
        row=self.library.get(job=2)
        self.assertEqual(row['state'],'ready')
        self.assertEqual(json.loads(row['metadata'])['mode'],'converted')

    async def test_media_cache_survives_original_cleanup(self):
        copy=Path(self.temp.name)/'upload.mkv';shutil.copyfile(self.source,copy)
        await self.library.register(dict(self.job,id=2),copy)
        copy.unlink()
        self.assertEqual((self.library.folder(2)/'source').read_bytes(),self.source.read_bytes())

    async def test_web_service_does_not_reset_running_transfer_jobs(self):
        from easy_tel.store import Store
        store=Store(Path(self.temp.name)/'jobs.sqlite3')
        job=store.add(123,456,'file',1)
        store.begin(job['id'])
        await self.client.get('/healthz')
        self.assertEqual(store.job(job['id'])['status'],'running')
        store.db.close()

    async def test_preparation_starts_only_when_watch_page_is_opened(self):
        self.assertEqual(self.library.get(job=1)['state'],'available')
        await self.client.get('/healthz')
        self.assertEqual(self.library.get(job=1)['state'],'available')
        await self.client.get(f'/watch/{self.token}')
        self.assertEqual(self.library.get(job=1)['state'],'queued')

    async def test_large_file_range_above_two_gib(self):
        source=Path(self.temp.name)/'large.mkv'
        position=2**31+123
        with source.open('wb') as stream:
            stream.seek(position)
            stream.write(b'large-file-marker')
        await self.library.register(dict(self.job,id=2),source)
        token=self.library.get(job=2)['token']
        response=await self.client.get(f'/media/{token}/source',headers={'Range':f'bytes={position}-{position+16}'})
        self.assertEqual(response.status,206)
        self.assertEqual(await response.read(),b'large-file-marker')

    async def test_owner_collision_does_not_expose_or_revoke_other_media(self):
        with self.assertRaises(RuntimeError):
            await self.library.register(dict(self.job,chat=999),self.source)
        self.assertIsNone(self.library.link(1,999))
        self.library.revoke(1,999)
        self.assertGreater(self.library.get(job=1)['expires'],time.time())

    async def test_renewed_link_does_not_reuse_old_asset_directory(self):
        old_folder=self.library.folder(1)
        self.library.revoke(1)
        self.library.cleanup()
        await self.library.register(self.job,self.source)
        self.assertNotEqual(old_folder,self.library.folder(1))
        self.assertNotEqual(self.token,self.library.get(job=1)['token'])

    async def test_background_worker_prepares_after_first_visit(self):
        background=TestClient(TestServer(create_app(self.library,prepare=True)))
        await background.start_server()
        try:
            self.assertEqual(self.library.get(job=1)['state'],'available')
            await background.get(f'/watch/{self.token}')
            end=time.monotonic()+12
            while time.monotonic()<end and self.library.get(job=1)['state'] in ('queued','processing'):
                await asyncio.sleep(.1)
            self.assertEqual(self.library.get(job=1)['state'],'ready')
            self.assertEqual((await background.get('/healthz')).status,200)
        finally:
            await background.close()

    async def test_failed_cross_filesystem_copy_releases_budget_and_keeps_source(self):
        before=self.library.usage()
        with patch('easy_tel.media.os.link',side_effect=OSError('cross filesystem')), \
             patch('easy_tel.media.shutil.copyfile',side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                await self.library.register(dict(self.job,id=2),self.source)
        self.assertIsNone(self.library.get(job=2))
        self.assertEqual(self.library.usage(),before)
        self.assertTrue(self.source.exists())
