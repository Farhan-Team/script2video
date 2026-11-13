#!/usr/bin/env python3
"""Simple web UI for script2video:  python app.py  → open http://127.0.0.1:7860"""
import os
import time
from pathlib import Path

import gradio as gr

import script2video as s2v

s2v.load_env()
HERE = Path(__file__).resolve().parent
OUT = Path(os.getenv("S2V_OUTPUT_DIR", HERE / "outputs"))
WORK = Path(os.getenv("S2V_WORK_DIR", HERE / "work"))
OUT.mkdir(parents=True, exist_ok=True)

VOICES = [
    "en-US-GuyNeural", "en-US-JennyNeural", "en-US-AriaNeural", "en-US-ChristopherNeural",
    "en-US-AndrewNeural", "en-US-EmmaNeural", "en-GB-RyanNeural", "en-GB-SoniaNeural",
    "en-AU-WilliamNeural", "en-IN-PrabhatNeural", "en-IN-NeerjaNeural",
    "ur-PK-AsadNeural", "ur-PK-UzmaNeural", "hi-IN-MadhurNeural", "hi-IN-SwaraNeural",
    "ar-SA-HamedNeural",
]
FORMATS = {"Landscape 16:9 (YouTube) 1920x1080": (1920, 1080),
           "Landscape 720p (faster)": (1280, 720),
           "Vertical 9:16 (Shorts/Reels/TikTok)": (1080, 1920)}


CAPTIONS = {"Word-by-word (highlight)": "words", "Phrases": "phrases", "Sentences": "sentences", "None": "none"}
MOODS = ["cinematic ambient", "calm piano", "upbeat corporate", "inspiring", "documentary", "lofi chill",
         "epic orchestral", "none"]


def generate(script_file, script_text, voice, speed, fmt, prefer, captions, caption_style, clip_seconds,
             mood, music, music_volume, media_dir, pexels, pixabay, unsplash, progress=gr.Progress()):
    text = ""
    if script_file:
        text = Path(script_file).read_text("utf-8", errors="ignore")
    if not text.strip():
        text = script_text or ""
    if not text.strip():
        raise gr.Error("Upload a script file or paste your script.")
    w, h = FORMATS[fmt]
    stem = time.strftime("video_%Y%m%d_%H%M%S")
    cfg = s2v.Config(
        script_text=text,
        output=OUT / f"{stem}.mp4",
        workdir=WORK / s2v.sha(text, w, h),     # same script = resumes from cache
        voice=voice, rate=f"{int(speed):+d}%",
        width=w, height=h, prefer=prefer.lower(),
        captions=CAPTIONS.get(captions, "words"), caption_style=caption_style.lower(),
        clip_seconds=float(clip_seconds),
        music=Path(music) if music else None, music_volume=float(music_volume),
        auto_music=(not music) and mood != "none", music_mood=mood,
        media_dir=Path(media_dir) if media_dir and Path(media_dir).is_dir() else None,
        pexels_key=pexels.strip(), pixabay_key=pixabay.strip(), unsplash_key=unsplash.strip(),
        progress=lambda f, m: progress(f, desc=m),
    )
    try:
        out = s2v.build_video(cfg)
    except Exception as e:
        raise gr.Error(str(e)[:800])
    return str(out), str(out.with_suffix(".credits.txt"))


with gr.Blocks(title="Script → Video") as demo:
    gr.Markdown("# Script → Video\nUpload a script, get a narrated stock-footage video with captions. 100% free.")
    with gr.Row():
        with gr.Column(scale=3):
            script_file = gr.File(label="Script (.txt / .md)", file_types=[".txt", ".md"], type="filepath")
            script_text = gr.Textbox(label="…or paste your script", lines=12,
                                     placeholder="Blank line = new scene. Optional: [search: city at night] "
                                                 "to choose the footage for a paragraph.")
        with gr.Column(scale=2):
            voice = gr.Dropdown(VOICES, value="en-US-GuyNeural", label="Voice", allow_custom_value=True)
            speed = gr.Slider(-30, 30, value=0, step=5, label="Speech speed (%)")
            fmt = gr.Dropdown(list(FORMATS), value=list(FORMATS)[0], label="Format")
            prefer = gr.Radio(["Video", "Photo"], value="Video", label="Stock footage type")
            captions = gr.Radio(list(CAPTIONS), value="Word-by-word (highlight)", label="Text overlay")
            caption_style = gr.Radio(["Box", "Outline"], value="Box", label="Caption style")
            clip_seconds = gr.Slider(3, 15, value=6, step=1, label="Change shot every N seconds")
            mood = gr.Dropdown(MOODS, value="cinematic ambient", label="Background music (free, automatic)",
                               allow_custom_value=True)
    with gr.Accordion("Music, own footage & API keys", open=False):
        music = gr.File(label="Your own music file (optional — replaces the automatic music)",
                        file_types=["audio"], type="filepath")
        music_volume = gr.Slider(0.02, 0.5, value=0.18, step=0.01, label="Music volume (dips under the voice)")
        media_dir = gr.Textbox(label="Folder with your own footage (optional, matched by file name)",
                               value=os.getenv("S2V_MEDIA_DIR", ""))
        pexels = gr.Textbox(label="Pexels API key", value=os.getenv("PEXELS_API_KEY", ""), type="password")
        pixabay = gr.Textbox(label="Pixabay API key", value=os.getenv("PIXABAY_API_KEY", ""), type="password")
        unsplash = gr.Textbox(label="Unsplash Access Key", value=os.getenv("UNSPLASH_ACCESS_KEY", ""),
                              type="password")
    btn = gr.Button("Generate video", variant="primary")
    video = gr.Video(label="Result")
    credits = gr.File(label="Footage credits")
    btn.click(generate, [script_file, script_text, voice, speed, fmt, prefer, captions, caption_style,
                         clip_seconds, mood, music, music_volume, media_dir, pexels, pixabay, unsplash],
              [video, credits])

if __name__ == "__main__":
    demo.queue().launch(inbrowser=not os.getenv("S2V_HEADLESS"),
                        server_name=os.getenv("S2V_HOST", "127.0.0.1"), server_port=int(os.getenv("S2V_PORT", "7860")),
                        allowed_paths=[str(OUT)])
