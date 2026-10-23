"""Decode real packages from representative codecs, not just filename extensions."""
import asyncio
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from easy_tel.media import MediaLibrary

# name, video encoder, audio encoder, container, encoder extras
CASES=[
 ('h264-aac','libx264','aac','mp4',[]),
 ('hevc-10bit','libx265','aac','mkv',['-pix_fmt','yuv420p10le','-x265-params','pools=1:frame-threads=1:log-level=error']),
 ('hdr10-pq','libx265','aac','mkv',['-pix_fmt','yuv420p10le','-x265-params','pools=1:frame-threads=1:log-level=error:colorprim=9:transfer=16:colormatrix=9','-color_primaries','bt2020','-color_trc','smpte2084','-colorspace','bt2020nc']),
 ('hdr-hlg','libx265','aac','mkv',['-pix_fmt','yuv420p10le','-x265-params','pools=1:frame-threads=1:log-level=error:colorprim=9:transfer=18:colormatrix=9','-color_primaries','bt2020','-color_trc','arib-std-b67','-colorspace','bt2020nc']),
 ('av1-opus','libaom-av1','libopus','mkv',['-cpu-used','8']),
 ('vp9-opus','libvpx-vp9','libopus','webm',['-deadline','realtime','-cpu-used','8']),
 ('vp8-vorbis','libvpx','libvorbis','webm',['-deadline','realtime']),
 ('mpeg4-mp3','mpeg4','libmp3lame','avi',[]),
 ('mpeg2-ac3','mpeg2video','ac3','ts',[]),
 ('wmv-wma','wmv2','wmav2','asf',[]),
 ('h264-flac','libx264','flac','mkv',[]),
 ('h264-dts','libx264','dca','mkv',['-strict','-2']),
 ('h264-eac3','libx264','eac3','mkv',[]),
 ('h264-pcm','libx264','pcm_s16le','mov',[]),
 ('h264-high10','libx264','aac','mkv',['-pix_fmt','yuv420p10le']),
 ('h264-444','libx264','aac','mkv',['-pix_fmt','yuv444p']),
 ('h264-surround','libx264','ac3','mkv',['-ac','6']),
 ('h264-silent','libx264',None,'mp4',[]),
 ('h264-vfr','libx264','aac','mkv',['-vf','select=not(mod(n\\,2))+not(mod(n\\,3))','-fps_mode','vfr']),
]


def fixture(folder,case):
    name,video,audio,extension,extra=case
    output=Path(folder)/f'{name}.{extension}'
    command=['ffmpeg','-nostdin','-v','error','-y','-filter_threads','1','-f','lavfi','-i','testsrc2=size=128x72:rate=12']
    if audio: command+=['-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-map','0:v','-map','1:a','-c:a',audio]
    else: command+=['-map','0:v','-an']
    command+=['-t','1.5','-c:v',video,'-threads','2','-pix_fmt','yuv420p',*extra,str(output)]
    subprocess.run(command,check=True,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE)
    return output


class FormatMatrixTests(unittest.IsolatedAsyncioTestCase):
    async def test_representative_codec_matrix_and_decoded_output(self):
        with tempfile.TemporaryDirectory() as folder:
            library=MediaLibrary(folder,'http://localhost:8080',reserve=0)
            for index,case in enumerate(CASES,1):
                with self.subTest(codec=case[0]):
                    source=await asyncio.to_thread(fixture,folder,case)
                    await library.register({'id':index,'chat':123,'name':source.name},source)
                    await library.prepare(index)
                    row=library.get(job=index)
                    self.assertEqual(row['state'],'ready',case[0])
                    meta=json.loads(row['metadata'])
                    self.assertEqual(meta['hdr_converted'],case[0].startswith('hdr'))
                    if case[0]=='h264-aac':
                        self.assertEqual(meta['mode'],'remuxed')
                        self.assertEqual(meta['audio'][0]['mode'],'original AAC')
                    if case[0]=='h264-silent': self.assertEqual(meta['audio'],[])
                    process=await asyncio.create_subprocess_exec('ffmpeg','-v','error','-threads','1','-i',
                        str(library.folder(index)/'master.m3u8'),'-f','null','-',
                        stdout=asyncio.subprocess.DEVNULL,stderr=asyncio.subprocess.PIPE)
                    _,error=await process.communicate()
                    self.assertEqual(process.returncode,0,error.decode()[:300])

    async def test_rotation_is_normalized_not_silently_remuxed(self):
        with tempfile.TemporaryDirectory() as folder:
            original=await asyncio.to_thread(fixture,folder,CASES[0])
            rotated=Path(folder)/'rotated.mp4'
            await asyncio.to_thread(subprocess.run,['ffmpeg','-nostdin','-v','error','-y','-display_rotation','90','-i',str(original),'-c','copy',str(rotated)],check=True)
            library=MediaLibrary(folder,'http://localhost:8080',reserve=0)
            await library.register({'id':1,'chat':123,'name':rotated.name},rotated)
            await library.prepare(1)
            row=library.get(job=1)
            self.assertEqual(row['state'],'ready')
            meta=json.loads(row['metadata'])
            self.assertEqual(meta['mode'],'converted')
            self.assertEqual(meta['resolution'],'128p')

    async def test_corrupt_or_playlist_input_fails_without_losing_original(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'not-a-movie.mp4'
            source.write_bytes(b'#EXTM3U\nfile:///not-an-allowed-source\n')
            library=MediaLibrary(folder,'http://localhost:8080',reserve=0)
            await library.register({'id':1,'chat':123,'name':source.name},source)
            await library.prepare(1)
            self.assertEqual(library.get(job=1)['state'],'error')
            self.assertEqual((library.folder(1)/'source').read_bytes(),source.read_bytes())
