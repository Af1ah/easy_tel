'use strict';
(() => {
  const $ = id => document.getElementById(id);
  const video = $('video');
  const token = location.pathname.split('/').filter(Boolean).pop();
  const notice = text => { $('notice').textContent = text; };
  const bytes = n => n ? (n / 1024 ** 3 >= 1 ? (n / 1024 ** 3).toFixed(2) + ' GiB' : (n / 1024 ** 2).toFixed(1) + ' MiB') : '';
  let player, hls, meta, lastSubtitle = '0', started = false, requestedAudio = 0;
  let networkRetries = 0, mediaRecovery = false;
  function unavailable(title, description) {
    $('preparing').hidden = false;
    $('state-title').textContent = title;
    $('state-description').textContent = description;
    document.querySelector('.spinner').hidden = true;
  }
  function skip(amount, zone) {
    if (!player || !Number.isFinite(video.duration)) return;
    video.currentTime = Math.max(0, Math.min(video.duration, video.currentTime + amount));
    const flash = zone?.querySelector('.seek-flash');
    if (flash) {
      flash.textContent = (amount > 0 ? '+' : '−') + Math.abs(amount) + ' seconds';
      flash.classList.add('visible');
      clearTimeout(zone.flashTimer);
      zone.flashTimer = setTimeout(() => flash.classList.remove('visible'), 650);
    }
  }
  document.querySelectorAll('.touch-zone').forEach(zone => {
    let last = 0, timer;
    zone.addEventListener('pointerup', event => {
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      event.preventDefault(); event.stopPropagation();
      const now = performance.now();
      if (last && now - last < 320) {
        clearTimeout(timer); last = 0; skip(Number(zone.dataset.seek), zone);
      } else {
        last = now;
        timer = setTimeout(() => { last = 0; if (player) { if (event.pointerType === 'touch') player.toggleControls(true); else player.togglePlay(); } }, 320);
      }
    });
    zone.addEventListener('dblclick', event => event.preventDefault());
    zone.addEventListener('keydown', event => {
      if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); skip(Number(zone.dataset.seek), zone); }
    });
  });
  function fillAudio(tracks) {
    const select = $('audio'); select.replaceChildren();
    tracks.forEach((track, i) => {
      const option = new Option(meta.audio?.[i]?.label || track.name || track.label || `Audio ${i+1}`, String(i));
      select.add(option);
    });
    if (!tracks.length) select.add(new Option('No audio tracks', ''));
    select.disabled = tracks.length < 2;
    select.value = String(hls ? Math.max(0, hls.audioTrack) : 0);
  }
  document.querySelectorAll('.track-menu').forEach(menu => {
    menu.addEventListener('toggle', () => { if (menu.open) { document.querySelectorAll('.track-menu').forEach(other => { if (other !== menu) other.open = false; }); player?.toggleControls(true); } });
    menu.querySelector('.close-panel').addEventListener('click', () => { menu.open = false; menu.querySelector('summary').focus(); });
  });
  document.addEventListener('keydown', event => { if (event.key === 'Escape') document.querySelectorAll('.track-menu').forEach(menu => { menu.open = false; }); });
  document.addEventListener('pointerdown', event => { if (!event.target.closest('.track-menu')) document.querySelectorAll('.track-menu').forEach(menu => { menu.open = false; }); });
  $('audio').addEventListener('change', () => {
    const index = Number($('audio').value);
    $('audio-label').textContent = $('audio').selectedOptions[0]?.textContent || 'Audio';
    requestedAudio = index;
    if (hls && hls.audioTracks.length) hls.audioTrack = index;
    else if (video.audioTracks) Array.from(video.audioTracks).forEach((track, i) => { track.enabled = i === index; });
  });
  function captions(value) {
    if (!player) return;
    const enabled = value !== 'off';
    if (enabled) { lastSubtitle = value; player.currentTrack = Number(value); }
    player.toggleCaptions(enabled);
    $('cc').setAttribute('aria-pressed', String(enabled));
    $('subtitles').value = value;
    $('subtitle-label').textContent = enabled ? $('subtitles').selectedOptions[0].textContent : 'Subtitles';
  }
  $('subtitles').addEventListener('change', () => captions($('subtitles').value));
  $('cc').addEventListener('click', () => captions($('subtitles').value === 'off' ? lastSubtitle : 'off'));
  async function initialize(data) {
    meta = data;
    data.subtitles.forEach((sub, i) => {
      const track = document.createElement('track');
      track.kind = 'subtitles'; track.label = sub.label; track.srclang = sub.language;
      track.src = sub.url; video.appendChild(track);
      $('subtitles').add(new Option(sub.label, String(i)));
    });
    $('subtitles').disabled = !data.subtitles.length;
    $('cc').disabled = !data.subtitles.length;
    video.poster = data.poster;
    player = new Plyr(video, {
      iconUrl: '/static/vendor/plyr.svg', blankVideo: '', seekTime: 10,
      clickToPlay: false, invertTime: false, storage: { enabled: false },
      captions: { active: false, update: true, language: 'auto' },
      keyboard: { focused: true, global: false },
      fullscreen: { enabled: true, fallback: true, iosNative: true, container: '.screen' },
      settings: ['speed'], controls: ['play-large','play','rewind','fast-forward','progress','current-time','duration','mute','volume','settings','pip','fullscreen'],
    });
    $('track-menus').hidden = false;
    fillAudio(data.audio.map(track => ({ name: track.label })));
    $('audio-label').textContent = data.audio[0]?.label || 'Audio';
    $('audio-note').textContent = data.audio.length ? 'Only the selected track is streamed.' : 'This video has no audio track.';
    player.on('controlshidden', () => document.querySelector('.screen').classList.add('is-idle'));
    player.on('controlsshown', () => document.querySelector('.screen').classList.remove('is-idle'));
    // Keep touch zones inside the chosen fullscreen container, above video but below controls.
    if (Hls.isSupported()) {
      hls = new Hls({ autoStartLoad: false, maxBufferLength: 20, maxMaxBufferLength: 30,
        maxBufferSize: 12*1024*1024, backBufferLength: 10, enableWorker: true });
      hls.on(Hls.Events.MEDIA_ATTACHED, () => hls.loadSource(data.manifest));
      hls.on(Hls.Events.MANIFEST_PARSED, () => { fillAudio(hls.audioTracks); hls.audioTrack = requestedAudio; });
      hls.on(Hls.Events.AUDIO_TRACKS_UPDATED, () => fillAudio(hls.audioTracks));
      hls.on(Hls.Events.AUDIO_TRACK_SWITCHED, (_, info) => { $('audio').value = String(info.id); $('audio-label').textContent = $('audio').selectedOptions[0]?.textContent || 'Audio'; });
      hls.on(Hls.Events.ERROR, (_, error) => {
        if (!error.fatal) return;
        if (error.type === Hls.ErrorTypes.NETWORK_ERROR && networkRetries < 3) {
          setTimeout(() => hls.startLoad(video.currentTime || -1), 1000 * 2 ** networkRetries++);
          notice('Connection interrupted. Retrying playback…');
        } else if (error.type === Hls.ErrorTypes.MEDIA_ERROR && !mediaRecovery) {
          mediaRecovery = true; hls.recoverMediaError();
        } else notice('Playback could not continue. The link may have expired, or this browser cannot decode the video. Download the original to open it in VLC.');
      });
      hls.attachMedia(video);
      video.addEventListener('play', () => { started = true; hls.startLoad(-1); });
      video.addEventListener('pause', () => hls.stopLoad());
      video.addEventListener('seeking', () => { if (started) hls.startLoad(video.currentTime); });
      video.addEventListener('seeked', () => { if (video.paused) hls.stopLoad(); });
    } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
      video.src = data.manifest;
      const refresh = () => {
        fillAudio(video.audioTracks ? Array.from(video.audioTracks) : []);
        if (!video.audioTracks && data.audio.length) {
          fillAudio([{ name: data.audio[0].label }]);
          if (data.audio.length > 1) notice('This browser does not expose audio switching. Try a current Chrome or Firefox browser.');
        }
      };
      video.addEventListener('loadedmetadata', refresh);
      video.audioTracks?.addEventListener('addtrack', refresh);
    } else {
      unavailable('This browser cannot stream this video', 'Download the original and open it in VLC, or use a current browser.');
      return;
    }
    video.addEventListener('playing', () => { networkRetries = 0; notice((data.warnings || []).join(' ')); });
    video.addEventListener('error', () => notice('Video playback failed. Download the original for VLC, or try another browser.'));
    $('preparing').hidden = true;
    notice((data.warnings || []).join(' '));
  }
  async function poll() {
    try {
      const response = await fetch(`/api/${encodeURIComponent(token)}`, { cache: 'no-store' });
      if (!response.ok) {
        unavailable(response.status === 410 ? 'This watch link has expired' : 'Video unavailable', 'Ask the sender for a new link.');
        return;
      }
      const data = await response.json();
      $('title').textContent = data.title.replace(/\.(mp4|mkv|mov|webm|avi|m4v|ts|wmv)$/i, '');
      document.title = data.title + ' · Easy Tel';
      $('expires').textContent = 'Available until ' + new Date(data.expires * 1000).toLocaleString([], { month:'short',day:'numeric',hour:'2-digit',minute:'2-digit' });
      $('download').href = data.download; $('download').classList.remove('disabled'); $('download').setAttribute('aria-disabled','false');
      if (data.duration) $('duration').textContent = Math.ceil(data.duration/60) + ' MIN';
      if (data.size) $('size').textContent = bytes(data.size);
      if (data.resolution) { $('quality').hidden = false; $('quality').textContent = data.resolution + ' · ' + (data.codec || 'H.264'); }
      $('playback-info').textContent = data.mode === 'remuxed' ? 'Original video · no re-encoding' : data.mode === 'converted' ? 'Browser-compatible playback' : 'On-demand playback';
      if (data.state === 'ready') await initialize(data);
      else if (data.state === 'error') unavailable('This video could not be prepared', 'The original is still available to download. Open it in VLC, or ask the sender to try a different video format.');
      else {
        $('state-description').textContent = data.state === 'processing' ? 'Preparing video, audio, and subtitles. Some formats need conversion; this may take a little while.' : 'Your video is in the preparation queue. You can keep this page open.';
        setTimeout(poll, 2000);
      }
    } catch (error) {
      notice('Could not load video details. Retrying…');
      setTimeout(poll, 4000);
    }
  }
  poll();
})();
