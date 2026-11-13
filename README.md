# script2video: script in, 10+ minute video out (free)

You give it a script, and you get back a narrated MP4 with:
- stock footage from Pexels, Pixabay or Unsplash, or your own clips
- text captions on screen
- optional background music

**Everything is free:**
- voices come from Microsoft Edge's neural voices, through `edge-tts`
- footage comes from free stock APIs
- FFmpeg does all the rendering on your computer

[![Open in Colab](https://colab.research.google.com/assets/colab-badge.svg)](https://colab.research.google.com/github/Farhan-Team/script2video/blob/main/script2video_colab.ipynb)

## Option 1: Run free in the cloud (Google Colab + Drive + Cloudflare link)

No install and no PC power needed.

1. Click **Open in Colab** above.
2. Add your `PEXELS_API_KEY` under 🔑 **Secrets**.
3. Run the cells.

You can either:
- open the web page through a free `trycloudflare.com` link, or
- render every script you put in **Google Drive → `MyDrive/script2video/scripts`**.

Videos are saved to `MyDrive/script2video/outputs`.

## Option 2: Run on your own computer

## 1. Install (one time)

1. Install **Python 3.10+** from https://python.org. On Windows, tick "Add to PATH".
2. Install **FFmpeg**:
   - Windows: `winget install Gyan.FFmpeg`
   - Mac: `brew install ffmpeg`
   - Linux: `sudo apt install ffmpeg`
3. In this folder, run:
   ```
   pip install -r requirements.txt
   ```
4. Get free API keys. Pexels alone is enough to start.
   - Pexels: https://www.pexels.com/api/
   - Pixabay: https://pixabay.com/api/docs/ (log in and your key is shown on the page)
   - Unsplash (optional, photos only): https://unsplash.com/developers

   Copy `.env.example` to `.env` and paste the keys in.

## 2. Make a video

**Web page (easiest):**
```
python app.py
```
Your browser opens. Upload your script, pick a voice, and click **Generate video**.
Finished videos are saved in `outputs/`.

**Share it publicly with a free Cloudflare link:**
1. Install `cloudflared` (`winget install --id Cloudflare.cloudflared`).
2. Run `share_cloudflare.bat` (or `./share_cloudflare.sh` on Mac/Linux).
3. It prints an `https://….trycloudflare.com` link that anyone can open while the window stays open.

Your PC does the rendering.

**Command line:**
```
python script2video.py my_script.txt
python script2video.py my_script.txt --voice en-GB-RyanNeural --music bg.mp3
python script2video.py my_script.txt --vertical              # Shorts / Reels
python script2video.py my_script.txt --size 1280x720          # renders ~2x faster
python script2video.py my_script.txt --media-dir D:\footage   # your own clips first
python script2video.py --help                                 # all options
```

## 3. Writing the script

- A **blank line starts a new scene**. Long paragraphs are split automatically at about 30 words.
- Keywords for the footage are picked automatically from each scene. To choose the footage yourself, put a tag anywhere in the paragraph. The tag is not read aloud:
  ```
  [search: city skyline at night] Meanwhile, cities never sleep...
  ```
- Markdown headings (`# Title`) are spoken, so they work as a title scene.
- **Urdu or Hindi voiceover:** use `--voice ur-PK-AsadNeural` or `ur-PK-UzmaNeural`. Add English `[search: ...]` tags, because stock sites search in English. Also use `--font "Noto Nastaliq Urdu"`, or another installed Urdu font, so the captions display correctly.

**Length guide:** 150 words of script makes about 1 minute of video, so **10 minutes ≈ 1,500 words**.

## How it works

1. **Script → scenes.** The script is split into scenes, and search keywords are picked for each one.
2. **Voiceover.** Each scene is read aloud by `edge-tts`. The length of each recording sets that scene's length.
3. **Footage.** The tool needs about one shot every 6 seconds (`--clip-seconds`). It looks in this order:
   1. your own folder (matched by file name)
   2. Pexels videos, then Pixabay videos
   3. photos with a slow zoom
   4. a plain gradient background, if nothing else is found
4. **Rendering.** Each scene is rendered with captions timed to the voice.
5. **Joining.** All scenes are joined into one file, and the background music is mixed in quietly.

**Resuming and editing.** Everything is cached in `<script>_work/`. If a run stops halfway, run the same command again and it continues where it left off. If you edit one paragraph, only that scene is rebuilt.

**Credits.** A `.credits.txt` file is saved next to each video. Pexels and Pixabay don't require credits, but they appreciate them, so paste the list into your video description.

## Speed and troubleshooting

- **Render time.** A 10-minute 1080p video usually takes about 5–15 minutes on a normal laptop.
  - Faster: `--size 1280x720` or `--workers 4`
  - Smaller file: `--crf 26`
- **"edge-tts failed".** This needs internet. Run it again (finished voice parts are cached), or use the offline voice: `--tts system` (requires `pip install pyttsx3`).
- **Plain blue backgrounds everywhere.** Your API keys are missing or wrong. Check `.env`.
- **Captions in the wrong font.** Use `--font "Font Name"` with a font that's installed on your computer.
