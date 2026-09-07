# ReelFetch

Original Instagram/Facebook public-media downloader inspired by the *workflow* of paste-link downloader sites, not copied from their proprietary source code.

## What works
- Paste + URL validation
- Server-side metadata fetch using current `yt-dlp`
- Thumbnail preview when the source exposes one
- Real available video resolution choices (no fake 4K labels)
- On-demand video download + FFmpeg merge
- MP3 extraction
- Best-effort public Instagram photo/carousel fallback with Instaloader
- Signed, expiring download tokens
- CDN proxy allowlist to reduce SSRF risk
- Mobile-first UI, SEO article, FAQ, Privacy, Terms, DMCA, Contact pages
- Docker + Render config

## Important reliability note
Instagram changes extraction endpoints and rate limits frequently. There is no honest way to promise that every public Instagram URL will work forever with a zero-maintenance self-hosted scraper. Public Reels/video support is currently strongest through `yt-dlp`; photo/profile-related open-source extractors have active breakage reports in September 2026. A server-side cookies file can improve reliability but can also be rate-limited and must be handled securely.

## Deploy on Render
1. Create a new GitHub repository.
2. Upload this project with `Dockerfile` at the repository root.
3. In Render choose **New > Web Service**, connect the repository, and select **Docker**.
4. Add `PUBLIC_BASE_URL=https://your-domain.example` after the first deploy/domain setup.
5. Render should generate `SECRET_KEY` if using `render.yaml`; otherwise create a long random value manually.
6. Optional: add `INSTAGRAM_COOKIES_B64` as a secret environment variable if anonymous public extraction is being rate-limited.

No build command is needed for Docker. Start command is already in `Dockerfile`.

## Cookie secret (optional)
Use only an Instagram account/session you are authorized to use. Export cookies in Netscape `cookies.txt` format. On your computer:

```bash
base64 -w 0 cookies.txt
```

On macOS:

```bash
base64 < cookies.txt | tr -d '\n'
```

Put the resulting value into Render's `INSTAGRAM_COOKIES_B64` secret. Never commit cookies to GitHub.

## Local run

```bash
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
pip install -r requirements.txt
uvicorn app.main:app --reload
```

FFmpeg must also be installed locally for MP3 and merged formats.

## Before production
- Replace `support@example.com` and `copyright@example.com` in `templates/legal.html`.
- Set a real `PUBLIC_BASE_URL` and strong `SECRET_KEY`.
- Keep the service limited to content users own or have permission to download.
- If you add AdSense/analytics, update Privacy/consent text and add the scripts only after your policy setup is complete.
- For multiple server replicas, move rate limiting/token state as needed; tokens are already stateless but rate limiting is per process.

## Test

```bash
python -m unittest discover -s tests -v
```
