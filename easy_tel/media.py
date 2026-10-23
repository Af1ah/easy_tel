"""Expiring local video library and bounded FFmpeg HLS preparation."""
import asyncio
import contextlib
import fcntl
import json
import logging
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import time
from urllib.parse import urlsplit

log = logging.getLogger('easy_tel.media')
VIDEO_EXTENSIONS = {'.mp4','.mkv','.mov','.webm','.m4v','.avi','.ts','.mts','.wmv','.flv','.mpg','.mpeg','.ogv','.m2ts','.3gp','.3g2','.vob','.asf','.m2v'}
DEMUXERS = 'mov,matroska,webm,avi,mpegts,mpeg,mpegvideo,flv,ogg,asf'
TEXT_SUBS = {'subrip','srt','ass','ssa','webvtt','mov_text','text'}


class MediaLibrary:
    def __init__(self, state, base_url='', ttl=86400, limit=40*1024**3, reserve=1024**3):
        self.root = (Path(state).resolve()/'media')
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.base_url = base_url.rstrip('/')
        if self.base_url:
            url = urlsplit(self.base_url)
            if not url.hostname or url.username or url.query or url.fragment or url.path not in ('','/'):
                raise ValueError('TEL_MEDIA_BASE_URL must be an origin, without a path')
            if url.scheme != 'https' and not (url.scheme=='http' and url.hostname in ('127.0.0.1','localhost')):
                raise ValueError('Watch links need HTTPS')
        self.ttl, self.limit, self.reserve = max(60,ttl), limit, reserve
        self.threads=max(1,min(4,int(os.environ.get('TEL_MEDIA_THREADS','2'))))
        self.max_height=max(360,min(1080,int(os.environ.get('TEL_MEDIA_MAX_HEIGHT','1080'))))
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS media (
              job INTEGER PRIMARY KEY, owner INTEGER NOT NULL, name TEXT NOT NULL,
              token TEXT UNIQUE NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL,
              reserved INTEGER NOT NULL, metadata TEXT NOT NULL DEFAULT '{}')''')

    @classmethod
    def from_env(cls):
        return cls(os.environ.get('TEL_STATE_DIR','./state'), os.environ.get('TEL_MEDIA_BASE_URL',''),
                   int(os.environ.get('TEL_MEDIA_TTL_SECONDS','86400')),
                   int(os.environ.get('TEL_MEDIA_MAX_BYTES',str(40*1024**3))),
                   int(os.environ.get('TEL_DISK_RESERVE_BYTES',str(1024**3))))

    def db(self):
        db = sqlite3.connect(self.root/'library.sqlite3',timeout=30)
        db.row_factory = sqlite3.Row
        return Database(db)

    def folder(self, job, token=None):
        token = token or (self.get(job=job) or {}).get('token')
        return self.root/str(int(job))/token if token else self.root/str(int(job))

    def get(self, *, job=None, token=None):
        with self.db() as db:
            if job is not None:
                row = db.execute('SELECT * FROM media WHERE job=?',(job,)).fetchone()
            else:
                row = db.execute('SELECT * FROM media WHERE token=?',(token,)).fetchone()
        return dict(row) if row else None

    def link(self, job, owner=None):
        row = self.get(job=job)
        if row and (owner is None or row['owner']==owner) and row['expires']>time.time() and row['state'] not in ('deleted','staging') and self.base_url:
            return f"{self.base_url}/watch/{row['token']}"
        return None

    def usage(self):
        with self.db() as db:
            rows = db.execute("SELECT job,token,reserved,state FROM media").fetchall()
        return sum(max(row['reserved'],folder_bytes(self.folder(row['job'],row['token'])))
                   if row['state']=='processing' else row['reserved'] for row in rows)

    def cleanup(self):
        with self.db() as db:
            rows = db.execute("SELECT job,token FROM media WHERE expires<=? AND state!='processing'",(time.time(),)).fetchall()
        for row in rows:
            shutil.rmtree(self.folder(row['job'],row['token']),ignore_errors=True)
            with self.db() as db:
                db.execute('DELETE FROM media WHERE job=? AND token=?',(row['job'],row['token']))

    def revoke(self, job, owner=None):
        # Deny new HTTP requests immediately. A processing worker stops at its next budget check.
        with self.db() as db:
            db.execute("UPDATE media SET expires=0,state=CASE WHEN state='processing' THEN 'processing' ELSE 'deleted' END WHERE job=? AND (? IS NULL OR owner=?)",(job,owner,owner))

    async def register(self, job, source):
        if not self.base_url or source.suffix.lower() not in VIDEO_EXTENSIONS:
            return None
        existing = self.get(job=job['id'])
        if existing and existing['owner']!=job['chat']:
            raise RuntimeError('Media job ID belongs to another owner')
        if existing and existing['expires']>time.time() and existing['state'] not in ('deleted','staging'):
            return self.link(job['id'])
        if existing and existing['state']=='processing':
            raise RuntimeError('Expired media preparation is stopping')
        await asyncio.to_thread(self.cleanup)
        # Shared between bot and HTTP worker processes; no concurrent registrations overcommit storage.
        with (self.root/'.register.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX)
            if existing and existing['state']=='staging':
                shutil.rmtree(self.folder(job['id'],existing['token']),ignore_errors=True)
                with self.db() as db:
                    db.execute('DELETE FROM media WHERE job=? AND token=?',(job['id'],existing['token']))
            needed = source.stat().st_size*3
            if await asyncio.to_thread(self.usage)+needed>self.limit:
                raise RuntimeError('Media cache full')
            token=secrets.token_urlsafe(32)
            folder = self.folder(job['id'],token)
            folder.mkdir(parents=True,mode=0o700)
            try:
                # Reserve first, so a crash/copy failure cannot leave unaccounted media bytes.
                with self.db() as db:
                    db.execute('INSERT OR REPLACE INTO media(job,owner,name,token,expires,state,reserved) VALUES(?,?,?,?,?,?,?)',
                               (job['id'],job['chat'],job['name'],token,time.time()+self.ttl,'staging',needed))
                try:
                    os.link(source,folder/'source')
                except OSError:
                    if shutil.disk_usage(self.root).free<source.stat().st_size+self.reserve:
                        raise RuntimeError('Insufficient media disk space')
                    copy=asyncio.create_task(asyncio.to_thread(shutil.copyfile,source,folder/'source'))
                    try:
                        await asyncio.shield(copy)
                    except asyncio.CancelledError:
                        await copy
                        raise
                with self.db() as db:
                    db.execute("UPDATE media SET state='available' WHERE job=? AND token=?",(job['id'],token))
            except BaseException:
                shutil.rmtree(folder,ignore_errors=True)
                with self.db() as db:
                    db.execute('DELETE FROM media WHERE job=? AND token=?',(job['id'],token))
                raise
        return self.link(job['id'])

    def enqueue(self, job, token):
        with self.db() as db:
            db.execute("UPDATE media SET state='queued' WHERE job=? AND token=? AND state='available' AND expires>?",
                       (job,token,time.time()))

    async def command(self, *args, job=None, capture=False):
        if args[0]=='ffmpeg':
            args=('nice','-n','10',*args)
            if shutil.which('ionice'): args=('ionice','-c','3',*args)
        process = await asyncio.create_subprocess_exec(*map(str,args),stdout=asyncio.subprocess.PIPE if capture else asyncio.subprocess.DEVNULL,
                                                       stderr=asyncio.subprocess.DEVNULL)
        task = asyncio.create_task(process.communicate())
        started = time.monotonic()
        try:
            while not task.done():
                await asyncio.wait((task,),timeout=1)
                if job is not None:
                    row = self.get(job=job)
                    if not row or row['expires']<=time.time() or row['state']=='deleted':
                        raise RuntimeError('Watch link expired')
                    if await asyncio.to_thread(self.usage)>self.limit or shutil.disk_usage(self.root).free<self.reserve:
                        raise RuntimeError('Media storage budget exceeded')
                if time.monotonic()-started>(30 if capture else 6*3600):
                    raise RuntimeError('Media command timeout')
            stdout,_ = await task
            if process.returncode:
                raise RuntimeError('FFmpeg could not prepare this media')
            return stdout
        finally:
            if process.returncode is None:
                with contextlib.suppress(ProcessLookupError): process.terminate()
                try:
                    await asyncio.wait_for(process.wait(),5)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
            task.cancel()
            await asyncio.gather(task,return_exceptions=True)

    async def prepare(self, job):
        row = self.get(job=job)
        if not row or row['expires']<=time.time() or row['state']=='deleted':
            return
        folder = self.folder(job,row['token'])
        with self.db() as db:
            db.execute("UPDATE media SET state='processing' WHERE job=?",(job,))
        try:
            info = json.loads(await self.command('ffprobe','-v','error','-protocol_whitelist','file,pipe',
                     '-format_whitelist',DEMUXERS,'-show_streams','-show_format','-of','json',folder/'source',capture=True,job=job))
            videos = [s for s in info['streams'] if s['codec_type']=='video' and not s.get('disposition',{}).get('attached_pic')]
            if not videos:
                raise RuntimeError('No video stream found')
            video = videos[0]
            if video.get('width',0)*video.get('height',0)>4096*2160:
                raise RuntimeError('Video exceeds the low-resource 4K input limit')
            duration = float(info['format'].get('duration',0))
            if duration<=0 or duration>24*3600:
                raise RuntimeError('Unsupported video duration')
            audios = [s for s in info['streams'] if s['codec_type']=='audio'][:8]
            all_subs = [s for s in info['streams'] if s['codec_type']=='subtitle']
            subtitles = [s for s in all_subs if s['codec_name'] in TEXT_SUBS][:16]
            if any(side.get('dv_profile')==5 for side in video.get('side_data_list',[])):
                raise RuntimeError('Dolby Vision profile 5 requires a compatible external player')
            hdr=video.get('color_transfer') in ('smpte2084','arib-std-b67')
            rotation=any(abs(float(side.get('rotation',0)))%360 for side in video.get('side_data_list',[]))
            copy = (not hdr and not rotation and video['codec_name']=='h264' and video.get('pix_fmt') in ('yuv420p','yuvj420p')
                    and video.get('level',0)<=51)
            budget = int((folder/'source').stat().st_size + (max((folder/'source').stat().st_size, duration*len(audios)*16000)
                           if copy else duration*(8500000+len(audios)*192000)/8) + 4*1024**2)
            with self.db() as db:
                db.execute('UPDATE media SET reserved=? WHERE job=?',(budget,job))
            if await asyncio.to_thread(self.usage)>self.limit:
                raise RuntimeError('Not enough cache space to prepare video')
            base = ['ffmpeg','-nostdin','-hide_banner','-loglevel','error','-y','-protocol_whitelist','file,pipe',
                    '-format_whitelist',DEMUXERS,'-threads','1','-filter_threads','1','-i',folder/'source']
            hls = ['-f','hls','-hls_time','4','-hls_playlist_type','vod','-hls_segment_type','fmp4',
                   '-hls_fmp4_init_filename','init.mp4']
            vdir = folder/'video'; vdir.mkdir(exist_ok=True)
            scale=f'scale=w=min({self.max_height*16//9}\\,iw):h=min({self.max_height}\\,ih):force_original_aspect_ratio=decrease:force_divisible_by=2'
            filters=scale+':out_color_matrix=bt709:out_range=tv'
            if hdr:
                filters='zscale=t=linear:npl=100,format=gbrpf32le,tonemap=tonemap=hable:desat=0,zscale=p=bt709:t=bt709:m=bt709:r=tv,format=yuv420p,'+filters
            encode = ['-c:v','copy'] if copy else ['-c:v','libx264','-threads',str(self.threads),'-preset','veryfast','-crf','22',
                     '-maxrate','8M','-bufsize','16M','-pix_fmt','yuv420p','-vf',filters,
                     '-fpsmax','30','-force_key_frames','expr:gte(t,n_forced*4)','-sc_threshold','0',
                     '-color_primaries','bt709','-color_trc','bt709','-colorspace','bt709']
            await self.command(*base,'-map',f"0:{video['index']}",'-an',*encode,*hls,
                               '-hls_segment_filename',vdir/'segment-%05d.m4s',vdir/'index.m3u8',job=job)
            audio_meta = []
            for i,stream in enumerate(audios):
                directory = folder/f'audio{i}'; directory.mkdir(exist_ok=True)
                audio_copy=(stream['codec_name']=='aac' and stream.get('profile')=='LC'
                            and stream.get('channels',0)<=2 and int(stream.get('sample_rate',0)) in (44100,48000))
                audio_encode=['-c:a','copy'] if audio_copy else ['-c:a','aac','-threads',str(self.threads),'-b:a','192k','-ac','2','-ar','48000']
                await self.command(*base,'-map',f"0:{stream['index']}",'-vn',*audio_encode,*hls,'-hls_segment_filename',
                                   directory/'segment-%05d.m4s',directory/'index.m3u8',job=job)
                name=label(stream,i,'Audio')
                if any(track['label']==name for track in audio_meta): name+=f' ({i+1})'
                audio_meta.append({'label':name,'language':language(stream),'mode':'original AAC' if audio_copy else 'AAC stereo'})
            subtitle_meta=[]
            for i,stream in enumerate(subtitles):
                await self.command(*base,'-map',f"0:{stream['index']}",'-c:s','webvtt',folder/f'subtitle{i}.vtt',job=job)
                subtitle_meta.append({'label':label(stream,i,'Subtitles'),'language':language(stream),'file':f'subtitle{i}.vtt'})
            # Read the resulting codec/profile, including the transcode path.
            prepared = json.loads(await self.command('ffprobe','-v','error','-show_streams','-of','json',vdir/'init.mp4',capture=True,job=job))
            vs = prepared['streams'][0]
            profile={'High':'6400','Main':'4d00','Baseline':'42e0','Constrained Baseline':'42e0'}.get(vs.get('profile'),'6400')
            codec = 'avc1.'+profile+format(max(1,vs.get('level',41)),'02x')
            master = ['#EXTM3U','#EXT-X-VERSION:7']
            for i,audio in enumerate(audio_meta):
                master.append(f'#EXT-X-MEDIA:TYPE=AUDIO,GROUP-ID="audio",NAME="{audio["label"]}",LANGUAGE="{audio["language"]}",DEFAULT={"YES" if i==0 else "NO"},AUTOSELECT=YES,URI="audio{i}/index.m3u8"')
            bandwidth=max(256000,int(info['format'].get('bit_rate',4000000))) if copy else 8500000
            master.append(f'#EXT-X-STREAM-INF:BANDWIDTH={bandwidth},CODECS="{codec}{",mp4a.40.2" if audios else ""}"'+(',AUDIO="audio"' if audios else ''))
            master.append('video/index.m3u8')
            (folder/'master.m3u8').write_text('\n'.join(master)+'\n')
            try:
                await self.command('ffmpeg','-nostdin','-v','error','-y','-protocol_whitelist','file,pipe','-format_whitelist',DEMUXERS,
                                   '-threads','2','-filter_threads','1','-ss',str(min(5,duration/4)),'-i',folder/'source','-map',f"0:{video['index']}",
                                   '-frames:v','1','-vf','scale=640:-2',folder/'poster.jpg',job=job)
            except RuntimeError:
                pass
            metadata={'duration':duration,'audio':audio_meta,'subtitles':subtitle_meta,
                      'size':(folder/'source').stat().st_size,'mode':'remuxed' if copy else 'converted',
                      'resolution':f"{vs.get('height',0)}p",'codec':'H.264','hdr_converted':hdr,
                      'warnings':(['HDR converted to SDR for browser compatibility. Download the original to retain HDR.'] if hdr else [])+(['Image-based or excess subtitles are not available in this browser player.'] if len(subtitles)<len(all_subs) else [])}
            actual=folder_bytes(folder)
            with self.db() as db:
                db.execute("UPDATE media SET state='ready',metadata=?,reserved=? WHERE job=? AND state!='deleted'",
                           (json.dumps(metadata),actual,job))
        except asyncio.CancelledError:
            with self.db() as db:
                db.execute("UPDATE media SET state='queued' WHERE job=? AND state='processing'",(job,))
            raise
        except Exception as exc:
            log.warning('Media job %s failed: %s',job,type(exc).__name__)
            for path in folder.iterdir() if folder.exists() else []:
                if path.name!='source':
                    shutil.rmtree(path) if path.is_dir() else path.unlink()
            with self.db() as db:
                db.execute("UPDATE media SET state='error',reserved=? WHERE job=? AND state!='deleted'",
                           ((folder/'source').stat().st_size if (folder/'source').exists() else 0,job))

    async def run(self):
        with (self.root/'.worker.lock').open('a') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            await self._run()

    async def _run(self):
        # One HTTP service / preparation worker per state directory.
        with self.db() as db:
            db.execute("UPDATE media SET state='queued' WHERE state='processing'")
        while True:
            await asyncio.to_thread(self.cleanup)
            with self.db() as db:
                row = db.execute("SELECT job FROM media WHERE state='queued' AND expires>? ORDER BY job LIMIT 1",(time.time(),)).fetchone()
            if row: await self.prepare(row['job'])
            else: await asyncio.sleep(2)


class Database:
    def __init__(self,connection): self.connection=connection
    def __enter__(self): return self.connection
    def __exit__(self,kind,value,tb):
        try:
            if kind: self.connection.rollback()
            else: self.connection.commit()
        finally: self.connection.close()


def label(stream,index,prefix):
    tags=stream.get('tags',{})
    names={'eng':'English','en':'English','hin':'Hindi','hi':'Hindi','mal':'Malayalam','ml':'Malayalam',
           'tam':'Tamil','ta':'Tamil','ara':'Arabic','spa':'Spanish','fra':'French','jpn':'Japanese'}
    value=tags.get('title') or names.get(tags.get('language'),tags.get('language')) or f'{prefix} {index+1}'
    return re.sub(r'["\r\n\\]','',value)[:80] or f'{prefix} {index+1}'


def language(stream):
    value=stream.get('tags',{}).get('language','und')
    return value if re.fullmatch(r'[a-zA-Z-]{2,15}',value) else 'und'


def folder_bytes(folder):
    total=0
    # Another process may be cleaning an expired cache directory concurrently.
    with contextlib.suppress(FileNotFoundError):
        for path in folder.rglob('*'):
            with contextlib.suppress(FileNotFoundError):
                if path.is_file(): total+=path.stat().st_size
    return total
