"""Private-by-link HTTP playback service; deploy behind an HTTPS reverse proxy."""
import argparse
import asyncio
import contextlib
import json
import mimetypes
import shutil
from pathlib import Path
import re
import time
from urllib.parse import quote
from aiohttp import web
from .media import MediaLibrary

STATIC=Path(__file__).parent/'static'
TOKEN=re.compile(r'[A-Za-z0-9_-]{43}')
ASSET=re.compile(r'(?:source|poster\.jpg|master\.m3u8|subtitle\d+\.vtt|(?:video|audio\d+)/(?:index\.m3u8|init\.mp4|segment-\d+\.m4s))')


@web.middleware
async def headers(request,handler):
    try:
        response=await handler(request)
    except web.HTTPException as exc:
        response=web.Response(status=exc.status,text=exc.text,headers=exc.headers)
    response.headers.update({'Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff',
      'X-Frame-Options':'DENY','Permissions-Policy':'camera=(), microphone=(), geolocation=()',
      'Content-Security-Policy':"default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; media-src 'self' blob:; connect-src 'self'; worker-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"})
    response.headers.setdefault('Cache-Control','private, no-store')
    return response


def create_app(library,prepare=True):
    app=web.Application(middlewares=[headers],client_max_size=1024)
    preparation_task=None

    def authorized(request):
        token=request.match_info['token']
        if not TOKEN.fullmatch(token): raise web.HTTPNotFound(text='Watch link not found')
        row=library.get(token=token)
        if not row: raise web.HTTPNotFound(text='Watch link not found')
        if row['expires']<=time.time() or row['state']=='deleted':
            raise web.HTTPGone(text='This watch link has expired or was removed.')
        return row

    async def watch(request):
        row=authorized(request)
        library.enqueue(row['job'],row['token'])
        return web.FileResponse(STATIC/'watch.html')

    async def api(request):
        row=authorized(request)
        data=json.loads(row['metadata'])
        prefix=f"/media/{row['token']}/"
        data.update({'title':row['name'],'state':row['state'],'expires':row['expires'],
                     'manifest':prefix+'master.m3u8','poster':prefix+'poster.jpg',
                     'download':prefix+'source'})
        data.setdefault('size',(library.folder(row['job'],row['token'])/'source').stat().st_size)
        for subtitle in data.get('subtitles',[]):
            subtitle['url']=prefix+subtitle.pop('file')
        return web.json_response(data)

    async def asset(request):
        row=authorized(request)
        name=request.match_info['asset']
        if not ASSET.fullmatch(name): raise web.HTTPNotFound()
        if name!='source' and row['state']!='ready': raise web.HTTPNotFound()
        folder=library.folder(row['job'],row['token']).resolve()
        path=folder/name
        if not path.resolve().is_relative_to(folder) or path.is_symlink() or not path.is_file():
            raise web.HTTPNotFound()
        response=web.FileResponse(path)
        types={'.m3u8':'application/vnd.apple.mpegurl','.m4s':'video/iso.segment','.vtt':'text/vtt; charset=utf-8'}
        response.content_type=types.get(path.suffix,mimetypes.guess_type(path.name)[0] or 'application/octet-stream').split(';')[0]
        if name=='source':
            response.content_type='application/octet-stream'
            response.headers['Content-Disposition']="attachment; filename*=UTF-8''"+quote(row['name'],safe='')
        # Private browser caching avoids re-fetching segments, never beyond link expiry.
        response.headers['Cache-Control']=f"private, max-age={max(0,min(300,int(row['expires']-time.time())))}, must-revalidate"
        return response

    async def static(request):
        name=request.match_info['name']
        allowed={'app.js','watch.css','vendor/plyr.js','vendor/plyr.css','vendor/plyr.svg','vendor/hls.min.js'}
        if name not in allowed: raise web.HTTPNotFound()
        return web.FileResponse(STATIC/name,headers={'Cache-Control':'public, max-age=3600'})

    async def health(request):
        task=preparation_task
        healthy=not task or not task.done()
        return web.json_response({'status':'ok' if healthy else 'preparation worker stopped'},status=200 if healthy else 503)
    app.router.add_get('/healthz',health)
    app.router.add_get('/watch/{token}',watch)
    app.router.add_get('/api/{token}',api)
    app.router.add_get('/media/{token}/{asset:.*}',asset)
    app.router.add_get('/static/{name:.*}',static)

    async def lifetime(app):
        nonlocal preparation_task
        if prepare and (not shutil.which('ffmpeg') or not shutil.which('ffprobe')):
            raise RuntimeError('Install ffmpeg and ffprobe before starting media preparation')
        task=asyncio.create_task(library.run()) if prepare else None
        preparation_task=task
        async def clean():
            while True:
                cleanup=asyncio.create_task(asyncio.to_thread(library.cleanup))
                try:
                    await asyncio.shield(cleanup)
                except asyncio.CancelledError:
                    await cleanup
                    raise
                await asyncio.sleep(30)
        janitor=asyncio.create_task(clean())
        try: yield
        finally:
            janitor.cancel()
            await asyncio.gather(janitor,return_exceptions=True)
            if task:
                task.cancel()
                await asyncio.gather(task,return_exceptions=True)
    app.cleanup_ctx.append(lifetime)
    return app


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=8080)
    args=parser.parse_args()
    # Access logs would leak capability tokens in URLs. Reverse-proxy logs must omit them too.
    web.run_app(create_app(MediaLibrary.from_env()),host=args.host,port=args.port,access_log=None)
