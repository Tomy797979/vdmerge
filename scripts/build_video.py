#!/usr/bin/env python3
"""
build_video.py — Marketing Auto (headless, GitHub Actions edition)

Input : text script + list of media (images and/or videos, URL or local path)
Voice : Microsoft edge-tts (free, no API key needed)
Output: single .mp4, auto-uploaded to Dropbox

Env vars expected (set by the workflow from `workflow_dispatch` inputs / secrets):
    MEDIA_URLS        comma or newline separated list of image/video URLs (or local paths under ./input_media)
    SCRIPT_TEXT        voiceover script text
    VOICE              edge-tts voice name, e.g. en-US-AriaNeural
    RATE               edge-tts rate, e.g. -8%
    RESOLUTION         tiktok | youtube | original
    MOTION             kenburns | static   (only applies to images)
    OUTPUT_NAME        e.g. marketing_video.mp4
    DROPBOX_FOLDER     e.g. /Marketing
    DROPBOX_APP_KEY, DROPBOX_APP_SECRET, DROPBOX_REFRESH_TOKEN   (repo secrets)
"""

import os
import re
import sys
import json
import asyncio
import tempfile
import subprocess
from pathlib import Path

import requests

# ── constants ──────────────────────────────────────────────
IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tiff"}
VIDEO_EXTS = {".mp4", ".mov", ".avi", ".mkv", ".webm"}
FPS = 30
DBX_API = "https://api.dropboxapi.com/2"
DBX_CONTENT = "https://content.dropboxapi.com/2"


def log(msg):
    print(f"[build_video] {msg}", flush=True)


def fail(msg):
    log(f"ERROR: {msg}")
    sys.exit(1)


# ── shell helpers ──────────────────────────────────────────
def run_ff(cmd, timeout=900):
    r = subprocess.run(cmd, capture_output=True, timeout=timeout)
    if r.returncode != 0:
        return False, r.stderr.decode(errors="ignore")[-1500:]
    return True, ""


def ffprobe_json(path):
    r = subprocess.run(
        ["ffprobe", "-v", "quiet", "-print_format", "json", "-show_streams", "-show_format", path],
        capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except Exception:
        return {}


def get_duration(path):
    data = ffprobe_json(path)
    try:
        return float(data.get("format", {}).get("duration", 0) or 0)
    except Exception:
        return 0.0


def has_audio_stream(path):
    r = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "a",
         "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
        capture_output=True, text=True)
    return "audio" in (r.stdout or "")


def get_dims(path):
    data = ffprobe_json(path)
    w = h = 0
    for s in data.get("streams", []):
        if s.get("codec_type") == "video":
            w, h = s.get("width", 0), s.get("height", 0)
            break
    if not w or not h:
        w, h = 1280, 720
    w -= w % 2
    h -= h % 2
    return w, h


# ── media download ─────────────────────────────────────────
def is_url(s):
    return s.startswith("http://") or s.startswith("https://")


HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
}


def download(url, dest):
    r = requests.get(url, headers=HEADERS, stream=True, timeout=120)
    r.raise_for_status()
    size = 0
    with open(dest, "wb") as f:
        for chunk in r.iter_content(512 * 1024):
            f.write(chunk)
            size += len(chunk)
    if size < 512:
        raise ValueError(f"Downloaded file too small ({size} bytes)")


def resolve_media_list(raw_list, tmp_dir):
    """Download URLs / copy local paths, classify each as image or video."""
    items = []
    for i, entry in enumerate(raw_list):
        entry = entry.strip()
        if not entry:
            continue
        ext = Path(entry.split("?")[0]).suffix.lower()
        if not ext:
            log(f"Skip (no extension): {entry}")
            continue
        kind = "image" if ext in IMAGE_EXTS else ("video" if ext in VIDEO_EXTS else None)
        if kind is None:
            log(f"Skip (unsupported extension {ext}): {entry}")
            continue

        dest = os.path.join(tmp_dir, f"media_{i:03d}{ext}")
        if is_url(entry):
            log(f"Downloading {kind}: {entry}")
            download(entry, dest)
        else:
            src = Path(entry)
            if not src.exists():
                fail(f"Local media not found: {entry}")
            dest = str(src)
        items.append({"path": dest, "kind": kind})
    if not items:
        fail("No valid media (images/videos) resolved from MEDIA_URLS.")
    return items


# ── TTS (edge-tts, free Microsoft voices) ─────────────────
def synth_voice(text, voice, rate, out_path):
    import edge_tts

    async def _run():
        communicate = edge_tts.Communicate(text, voice=voice, rate=rate)
        await communicate.save(out_path)

    asyncio.run(_run())
    if not os.path.exists(out_path) or os.path.getsize(out_path) < 1024:
        fail("edge-tts did not produce a usable audio file (check network egress / voice name).")


# ── resolution / clip building ─────────────────────────────
def target_dims(resolution, media_items):
    if resolution == "tiktok":
        return 1080, 1920
    if resolution == "youtube":
        return 1920, 1080
    # "original" -> use first media item's own dimensions
    return get_dims(media_items[0]["path"])


def scale_pad_filter(w, h):
    return (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
            f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:black,setsar=1")


def build_image_clip(img_path, duration, w, h, motion, out_path):
    vf_scale = scale_pad_filter(w, h)
    if motion == "kenburns":
        # Upscale first so the zoompan crop has room to move, then pad to target.
        up_w, up_h = w * 2, h * 2
        vf = (f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
              f"crop={up_w}:{up_h},"
              f"zoompan=z='min(zoom+0.0006,1.16)':d={int(FPS*duration)}:s={w}x{h}:fps={FPS}")
    else:
        vf = f"{vf_scale},fps={FPS}"
    cmd = ["ffmpeg", "-y", "-loop", "1", "-i", img_path, "-t", str(duration),
           "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
           "-pix_fmt", "yuv420p", "-an", out_path]
    ok, err = run_ff(cmd)
    if not ok:
        fail(f"Failed to build image clip [{os.path.basename(img_path)}]: {err}")


def build_video_clip(vid_path, duration, w, h, out_path):
    src_dur = get_duration(vid_path) or duration
    vf = f"{scale_pad_filter(w, h)},fps={FPS}"
    if src_dur < duration - 0.05:
        cmd = ["ffmpeg", "-y", "-stream_loop", "-1", "-i", vid_path, "-t", str(duration),
               "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-an", out_path]
    else:
        cmd = ["ffmpeg", "-y", "-i", vid_path, "-t", str(duration),
               "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "23",
               "-pix_fmt", "yuv420p", "-an", out_path]
    ok, err = run_ff(cmd)
    if not ok:
        fail(f"Failed to build video clip [{os.path.basename(vid_path)}]: {err}")


def build_final_video(media_items, voice_path, resolution, motion, output_path, tmp_dir):
    voice_dur = get_duration(voice_path)
    if not voice_dur or voice_dur <= 0:
        fail("Could not read voiceover duration.")
    log(f"Voiceover duration: {voice_dur:.1f}s across {len(media_items)} media item(s)")

    w, h = target_dims(resolution, media_items)
    per_item = max(0.8, voice_dur / len(media_items))

    clips = []
    for i, item in enumerate(media_items):
        clip_path = os.path.join(tmp_dir, f"clip_{i:03d}.mp4")
        if item["kind"] == "image":
            build_image_clip(item["path"], per_item, w, h, motion, clip_path)
        else:
            build_video_clip(item["path"], per_item, w, h, clip_path)
        clips.append(clip_path)
        log(f"Built clip {i+1}/{len(media_items)} ({item['kind']}, {per_item:.1f}s)")

    concat_txt = os.path.join(tmp_dir, "concat.txt")
    with open(concat_txt, "w") as f:
        f.write("\n".join(f"file '{c}'" for c in clips))
    silent_video = os.path.join(tmp_dir, "silent.mp4")
    ok, err = run_ff(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", concat_txt,
                      "-c", "copy", silent_video])
    if not ok:
        fail(f"Concat failed: {err}")

    voice_aac = os.path.join(tmp_dir, "voice.aac")
    ok, err = run_ff(["ffmpeg", "-y", "-i", voice_path, "-vn", "-c:a", "aac",
                      "-b:a", "192k", "-ar", "44100", "-ac", "2", voice_aac])
    if not ok:
        fail(f"Voice re-encode failed: {err}")

    ok, err = run_ff(["ffmpeg", "-y", "-i", silent_video, "-i", voice_aac,
                      "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
                      "-map", "0:v:0", "-map", "1:a:0", "-shortest",
                      "-movflags", "+faststart", output_path])
    if not ok:
        fail(f"Final mux failed: {err}")
    log(f"Final video ready: {output_path}")


# ── Dropbox upload (refresh-token OAuth) ──────────────────
def dbx_get_access_token(app_key, app_secret, refresh_token):
    r = requests.post(
        "https://api.dropbox.com/oauth2/token",
        data={"grant_type": "refresh_token", "refresh_token": refresh_token,
              "client_id": app_key, "client_secret": app_secret},
        timeout=20)
    data = r.json()
    token = data.get("access_token")
    if not token:
        fail(f"Dropbox auth failed: {data.get('error_description', data.get('error'))}")
    return token


def dbx_list_names(token, folder):
    try:
        r = requests.post(f"{DBX_API}/files/list_folder",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"path": folder, "recursive": False}, timeout=20)
        return {e["name"] for e in r.json().get("entries", [])} if r.status_code == 200 else set()
    except Exception:
        return set()


def unique_name(filename, existing):
    if filename not in existing:
        return filename
    stem, ext = Path(filename).stem, Path(filename).suffix
    i = 1
    while f"{stem}_{i}{ext}" in existing:
        i += 1
    return f"{stem}_{i}{ext}"


def dbx_upload(token, file_path, folder, filename):
    existing = dbx_list_names(token, folder)
    final_name = unique_name(filename, existing)
    dest = (folder.rstrip("/") + "/" + final_name)
    size = os.path.getsize(file_path)
    CHUNK = 148 * 1024 * 1024

    if size <= CHUNK:
        with open(file_path, "rb") as f:
            r = requests.post(f"{DBX_CONTENT}/files/upload",
                headers={"Authorization": f"Bearer {token}",
                         "Dropbox-API-Arg": json.dumps({"path": dest, "mode": "add", "autorename": False}),
                         "Content-Type": "application/octet-stream"},
                data=f, timeout=900)
        if r.status_code != 200:
            fail(f"Dropbox upload error {r.status_code}: {r.text[:300]}")
    else:
        with open(file_path, "rb") as f:
            chunk = f.read(CHUNK)
            r = requests.post(f"{DBX_CONTENT}/files/upload_session/start",
                headers={"Authorization": f"Bearer {token}", "Dropbox-API-Arg": '{"close":false}',
                         "Content-Type": "application/octet-stream"},
                data=chunk, timeout=900)
            if r.status_code != 200:
                fail(f"Dropbox session start error: {r.text[:300]}")
            sid = r.json()["session_id"]
            offset = len(chunk)
            while True:
                chunk = f.read(CHUNK)
                if not chunk:
                    break
                requests.post(f"{DBX_CONTENT}/files/upload_session/append_v2",
                    headers={"Authorization": f"Bearer {token}",
                             "Dropbox-API-Arg": json.dumps({"cursor": {"session_id": sid, "offset": offset}, "close": False}),
                             "Content-Type": "application/octet-stream"},
                    data=chunk, timeout=900)
                offset += len(chunk)
            r = requests.post(f"{DBX_CONTENT}/files/upload_session/finish",
                headers={"Authorization": f"Bearer {token}",
                         "Dropbox-API-Arg": json.dumps({"cursor": {"session_id": sid, "offset": offset},
                                                        "commit": {"path": dest, "mode": "add"}}),
                         "Content-Type": "application/octet-stream"},
                data=b"", timeout=900)
            if r.status_code != 200:
                fail(f"Dropbox session finish error: {r.text[:300]}")

    r2 = requests.post(f"{DBX_API}/sharing/create_shared_link_with_settings",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={"path": dest, "settings": {"requested_visibility": "public"}}, timeout=20)
    url = None
    if r2.status_code == 200:
        url = r2.json().get("url", "")
    elif r2.status_code == 409:
        url = (r2.json().get("error", {}).get("shared_link_already_exists", {})
               .get("metadata", {}).get("url", ""))
    if url:
        url = url.replace("www.dropbox.com", "dl.dropboxusercontent.com").replace("?dl=0", "?dl=1")
    return final_name, url


# ── main ───────────────────────────────────────────────────
def main():
    media_raw = os.environ.get("MEDIA_URLS", "")
    script_text = os.environ.get("SCRIPT_TEXT", "").strip()
    voice = os.environ.get("VOICE", "en-US-AriaNeural")
    rate = os.environ.get("RATE", "-8%")
    resolution = os.environ.get("RESOLUTION", "tiktok")
    motion = os.environ.get("MOTION", "kenburns")
    output_name = os.environ.get("OUTPUT_NAME", "marketing_video.mp4")
    dropbox_folder = os.environ.get("DROPBOX_FOLDER", "/Marketing")

    app_key = os.environ.get("DROPBOX_APP_KEY", "")
    app_secret = os.environ.get("DROPBOX_APP_SECRET", "")
    refresh_token = os.environ.get("DROPBOX_REFRESH_TOKEN", "")

    if not script_text:
        fail("SCRIPT_TEXT is empty.")
    if not (app_key and app_secret and refresh_token):
        fail("Missing Dropbox credentials (DROPBOX_APP_KEY / DROPBOX_APP_SECRET / DROPBOX_REFRESH_TOKEN).")

    raw_list = [x.strip() for x in re.split(r"[\n,]+", media_raw) if x.strip()]
    if not raw_list:
        fail("MEDIA_URLS is empty — provide at least one image or video URL.")

    with tempfile.TemporaryDirectory() as tmp_dir:
        log("Resolving media...")
        media_items = resolve_media_list(raw_list, tmp_dir)

        log(f"Generating voiceover with edge-tts voice={voice} rate={rate}...")
        voice_path = os.path.join(tmp_dir, "voice_raw.mp3")
        synth_voice(script_text, voice, rate, voice_path)
        if not has_audio_stream(voice_path):
            fail("Generated voice file has no audio stream.")

        output_path = os.path.join(tmp_dir, output_name)
        build_final_video(media_items, voice_path, resolution, motion, output_path, tmp_dir)

        log("Authenticating with Dropbox...")
        token = dbx_get_access_token(app_key, app_secret, refresh_token)

        log(f"Uploading to Dropbox: {dropbox_folder}")
        final_name, share_url = dbx_upload(token, output_path, dropbox_folder, output_name)

        log(f"Done. Uploaded as '{final_name}'.")
        if share_url:
            log(f"Shared link: {share_url}")
        else:
            log("Uploaded, but could not create a shared link (check app permissions).")

        # Surface key results in the GitHub Actions run summary.
        summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
        if summary_path:
            with open(summary_path, "a") as f:
                f.write("## 🎬 Marketing Video Result\n\n")
                f.write(f"- **File:** `{final_name}`\n")
                f.write(f"- **Folder:** `{dropbox_folder}`\n")
                if share_url:
                    f.write(f"- **Link:** {share_url}\n")


if __name__ == "__main__":
    main()
