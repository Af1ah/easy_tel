"""Real Chromium playback checks with synthetic media; no Telegram/Drive credentials.
Run: python tests/browser_media.py
"""
import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import time
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from aiohttp.test_utils import TestServer
from playwright.async_api import async_playwright
from easy_tel.media import MediaLibrary
from easy_tel.web import create_app
from media_fixtures import make_video


async def wait_until(function,timeout=12):
    end=time.monotonic()+timeout
    while time.monotonic()<end:
        if await function(): return
        await asyncio.sleep(.1)
    raise AssertionError('Browser condition did not complete')


async def main():
    output=Path(os.environ.get('TEL_TEST_ARTIFACTS','/tmp/easy-tel-browser-artifacts'))
    output.mkdir(parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory() as folder:
        source=await asyncio.to_thread(make_video,folder,40)
        library=MediaLibrary(folder,'http://localhost:8080',reserve=0)
        await library.register({'id':1,'chat':123,'name':'A quiet afternoon.mkv'},source)
        await library.prepare(1)
        row=library.get(job=1)
        assert row['state']=='ready'
        server=TestServer(create_app(library,prepare=False))
        await server.start_server()
        url=str(server.make_url('/watch/'+row['token']))
        try:
            async with async_playwright() as playwright:
                browser=await playwright.chromium.launch(executable_path=os.environ.get('CHROMIUM_PATH','/usr/bin/chromium'),headless=True,args=['--no-sandbox'])
                page=await browser.new_page(viewport={'width':1440,'height':1080})
                errors=[];media_requests=[];foreign=[]
                page.on('pageerror',lambda error:errors.append(str(error)))
                page.on('request',lambda request:media_requests.append(request.url) if '.m4s' in request.url else None)
                page.on('request',lambda request:foreign.append(request.url) if request.url.startswith('http') and not request.url.startswith(str(server.make_url('/'))) else None)
                devtools=await page.context.new_cdp_session(page)
                await devtools.send('Network.enable')
                await devtools.send('Network.emulateNetworkConditions',{'offline':False,'latency':80,'downloadThroughput':2*1024*1024,'uploadThroughput':512*1024})
                await page.goto(url)
                await page.locator('.plyr__control--overlaid').wait_for()
                assert not media_requests,'Fetched video segments before user pressed play'
                started=time.monotonic()
                await page.locator('.plyr__control--overlaid').click()
                async def playing(): return await page.locator('video').evaluate('(v)=>v.currentTime>.2')
                await wait_until(playing)
                startup=time.monotonic()-started
                await page.locator('.screen').hover()
                await page.locator('#audio-menu summary').click()
                assert await page.locator('#audio option').all_text_contents()==['English','Hindi']
                await page.select_option('#audio','1')
                async def second_audio(): return any('/audio1/' in path for path in media_requests)
                await wait_until(second_audio)
                await page.locator('#audio-menu .close-panel').click()
                await page.locator('#subtitles-menu summary').click()
                await page.select_option('#subtitles','0')
                async def captions(): return 'English subtitle track' in await page.locator('.plyr__captions').inner_text()
                await wait_until(captions)
                await page.locator('#cc').click()
                assert await page.locator('#subtitles').input_value()=='off'
                await page.select_option('#subtitles','1')
                async def second_caption(): return 'Second subtitle track' in await page.locator('.plyr__captions').inner_text()
                await wait_until(second_caption)
                await page.locator('#subtitles-menu .close-panel').click()
                before=await page.locator('video').evaluate('(v)=>v.currentTime')
                await page.locator('.touch-zone.right').dblclick(delay=75)
                await asyncio.sleep(.5)
                after=await page.locator('video').evaluate('(v)=>v.currentTime')
                assert after-before>=9.5,(before,after)
                await page.locator('.touch-zone.left').dblclick(delay=75)
                await asyncio.sleep(.5)
                back=await page.locator('video').evaluate('(v)=>v.currentTime')
                assert after-back>=8.5,(after,back)
                await page.locator('video').evaluate('(v)=>v.pause()')
                await page.locator('.screen').hover()
                slider=page.locator('input[data-plyr="seek"]')
                await slider.focus()
                await slider.press('End')
                await asyncio.sleep(.5)
                assert await page.locator('video').evaluate('(v)=>v.currentTime')>35
                await slider.press('Home')
                await page.locator('[data-plyr="mute"]').click()
                assert await page.locator('video').evaluate('(v)=>v.muted')
                await page.screenshot(path=str(output/'desktop.png'),full_page=True)
                mobile=await browser.new_page(viewport={'width':390,'height':844},has_touch=True,is_mobile=True)
                mobile.on('pageerror',lambda error:errors.append(str(error)))
                await mobile.goto(url)
                await mobile.locator('.plyr__control--overlaid').tap()
                async def mobile_playing(): return await mobile.locator('video').evaluate('(v)=>v.currentTime>.5')
                await wait_until(mobile_playing)
                await mobile.locator('.touch-zone.right').tap()
                await mobile.locator('.touch-zone.right').tap()
                await asyncio.sleep(.5)
                assert await mobile.locator('video').evaluate('(v)=>v.currentTime')>=10
                await mobile.locator('.touch-zone.left').tap()
                await mobile.locator('.touch-zone.left').tap()
                await asyncio.sleep(.5)
                assert await mobile.locator('video').evaluate('(v)=>v.currentTime')<5
                await mobile.locator('video').evaluate('(v)=>v.pause()')
                await mobile.locator('.touch-zone.right').tap()
                await mobile.locator('#audio-menu summary').tap()
                await mobile.select_option('#audio','1')
                await mobile.locator('#audio-menu .close-panel').tap()
                await mobile.screenshot(path=str(output/'mobile.png'),full_page=True)
                await mobile.locator('[data-plyr="mute"]').tap()
                assert await mobile.locator('video').evaluate('(v)=>v.muted')
                screen_box=await mobile.locator('.screen').bounding_box()
                for name in ('play','rewind','fast-forward','mute','fullscreen'):
                    button=mobile.locator(f'.plyr__controls [data-plyr="{name}"]')
                    box=await button.bounding_box()
                    assert box and box['y']+box['height']<=screen_box['y']+screen_box['height']+1, f'Mobile {name} control clipped'
                await mobile.screenshot(path=str(output/'mobile.png'),full_page=True)
                width=await mobile.evaluate('document.documentElement.scrollWidth')
                assert width<=390,'Mobile layout overflows horizontally'
                assert not errors,errors
                assert not foreign,'Player made an external request'
                report={'desktop_playback':True,'startup_seconds_at_80ms_simulated_latency':round(startup,2),
                        'audio_switch':True,'subtitle_switch_and_off':True,'seek_slider':True,
                        'desktop_and_mobile_double_tap':True,'mute':True,'mobile_overflow':False,'mobile_controls_visible_and_mute_works':True,
                        'page_errors':errors,'external_requests':foreign}
                (output/'report.json').write_text(json.dumps(report,indent=2))
                print(json.dumps(report,indent=2))
                print('Screenshots:',output)
                await browser.close()
        finally:
            await server.close()


if __name__=='__main__': asyncio.run(main())
