"""Small deterministic media fixtures; no external videos or accounts required."""
from pathlib import Path
import subprocess


def make_video(folder,duration=16,codec='libx264'):
    folder=Path(folder)
    for name,text in [('en','English subtitle track.'),('ml','Second subtitle track.')]:
        (folder/f'{name}.srt').write_text(f'1\n00:00:00,000 --> 00:00:{int(duration-1):02d},900\n{text}\n')
    path=folder/'fixture.mkv'
    subprocess.run(['ffmpeg','-nostdin','-v','error','-y','-f','lavfi','-i','testsrc2=size=320x180:rate=24',
        '-f','lavfi','-i','sine=frequency=440:sample_rate=48000','-f','lavfi','-i','sine=frequency=880:sample_rate=48000',
        '-i',str(folder/'en.srt'),'-i',str(folder/'ml.srt'),'-map','0:v','-map','1:a','-map','2:a','-map','3:s','-map','4:s',
        '-t',str(duration),'-c:v',codec,'-threads','2','-pix_fmt','yuv420p','-g','48','-c:a','aac','-c:s','srt',
        '-metadata:s:a:0','language=eng','-metadata:s:a:0','title=English','-metadata:s:a:1','language=hin',
        '-metadata:s:a:1','title=Hindi','-metadata:s:s:0','language=eng','-metadata:s:s:1','language=mal',str(path)],check=True)
    return path
