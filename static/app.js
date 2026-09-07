(() => {
  const form = document.getElementById('download-form');
  if (!form) return;
  const input = document.getElementById('media-url');
  const paste = document.getElementById('paste-btn');
  const submit = document.getElementById('analyze-btn');
  const status = document.getElementById('status');
  const result = document.getElementById('result');
  const thumb = document.getElementById('thumb');
  const title = document.getElementById('result-title');
  const uploader = document.getElementById('uploader');
  const duration = document.getElementById('duration');
  const qualityList = document.getElementById('quality-list');
  const mediaList = document.getElementById('media-list');
  const audio = document.getElementById('audio-btn');

  const qs = new URLSearchParams(location.search);
  if (qs.get('notice') === 'stories') status.textContent = 'Story downloads can require account authentication, so anonymous story viewing is not enabled in this build.';
  if (qs.get('notice') === 'profile') status.textContent = 'Bulk/profile scraping is not enabled. Paste a specific public post or Reel instead.';

  paste.addEventListener('click', async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) input.value = text.trim();
      input.focus();
    } catch (_) {
      input.focus();
      status.textContent = 'Clipboard permission was blocked. Long-press and paste the link manually.';
    }
  });

  const fmtDuration = (seconds) => {
    if (!seconds || !Number.isFinite(seconds)) return '';
    const s = Math.floor(seconds % 60).toString().padStart(2, '0');
    const m = Math.floor((seconds / 60) % 60);
    const h = Math.floor(seconds / 3600);
    return h ? `${h}:${m.toString().padStart(2, '0')}:${s}` : `${m}:${s}`;
  };

  const chip = (label, href) => {
    const a = document.createElement('a');
    a.className = 'download-chip';
    a.textContent = `Download ${label}`;
    a.href = href;
    return a;
  };

  form.addEventListener('submit', async (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (!url) { status.textContent = 'Paste a link first.'; return; }
    result.hidden = true;
    qualityList.replaceChildren(); mediaList.replaceChildren();
    status.className = 'status loading'; status.textContent = 'Checking the link and fetching media options…';
    submit.disabled = true;
    try {
      const res = await fetch('/api/analyze', {method:'POST', headers:{'content-type':'application/json'}, body:JSON.stringify({url})});
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.detail || 'Could not analyze this link.');

      title.textContent = data.title || 'Media ready';
      uploader.textContent = data.uploader ? `@${String(data.uploader).replace(/^@/, '')}` : 'Public media';
      if (data.thumbnail) { thumb.src = data.thumbnail; thumb.hidden = false; } else { thumb.hidden = true; }
      const d = fmtDuration(Number(data.duration));
      duration.textContent = d; duration.hidden = !d;

      (data.formats || []).forEach((f) => {
        const h = f.height == null ? 'original' : String(f.height);
        qualityList.appendChild(chip(f.label || 'Original', `/api/download/video?token=${encodeURIComponent(data.token)}&height=${encodeURIComponent(h)}`));
      });
      (data.media || []).forEach((m, i) => mediaList.appendChild(chip(`${m.type === 'image' ? 'Photo' : 'Media'} ${i+1}`, m.download)));
      if (data.audio) { audio.href = `/api/download/audio?token=${encodeURIComponent(data.token)}`; audio.hidden = false; } else { audio.hidden = true; }

      status.textContent = '';
      status.className = 'status';
      result.hidden = false;
      result.scrollIntoView({behavior:'smooth', block:'nearest'});
    } catch (err) {
      status.className = 'status';
      status.textContent = err.message || 'Something went wrong.';
    } finally {
      submit.disabled = false;
    }
  });
})();
