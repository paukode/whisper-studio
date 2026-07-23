"""Audio and video attachment extraction.

Audio is transcribed locally with mlx-whisper (large-v3-turbo) into a
timestamped, speaker-labeled markdown transcript. Speaker labels reuse the
ECAPA embedding + agglomerative clustering already in server/diarization. Video
uses ffmpeg (via mlx-whisper's loader) for the audio track, and additionally
samples keyframes whose on-screen text is OCR'd with Apple Vision and merged in
as timestamped notes. Those same frames are also RETAINED as downscaled JPEGs
(hybrid video understanding): the transcript + OCR text feed text-only models,
while vision models additionally receive the actual frames so a user can ask
about or reference what was on screen. Frame count is bounded to _MAX_FRAMES —
see _sample_video_frames for the every-~10s cadence and the long-video stretch.

Fully on-device: transcription, diarization, and frame OCR all run locally. No
Bedrock call. (Frame OCR uses Apple Vision directly, not the shared escalating
engine, so a text-free video never fans out into a Haiku call per frame.)

Transcription is submitted to the Whisper backend's single-worker executor (the
same one live ASR decodes on), so the mlx-whisper model is loaded once and
shared, and media decodes are serialized with live recording rather than running
MLX concurrently on a second thread with a second model load.

Blocking (model inference + ffmpeg); callers on the event loop must wrap this in
asyncio.to_thread.
"""

import glob
import logging
import os
import shutil
import subprocess
import tempfile

log = logging.getLogger("whisper-studio")

AUDIO_EXTENSIONS = {
    ".mp3",
    ".wav",
    ".m4a",
    ".flac",
    ".ogg",
    ".aac",
    ".aiff",
    ".aif",
    ".opus",
    ".wma",
}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}

_SR = 16000  # mlx-whisper / ECAPA both expect 16 kHz mono
_FRAME_INTERVAL_SEC = 10  # baseline cadence: one frame every N seconds (short clips)
_MAX_FRAMES = 20  # never retain/OCR more than this many frames per video
# Retained frames are sized for readable OCR: 1288px long edge at JPEG q82 keeps
# on-screen text legible to the model while staying well under the per-image cap.
_FRAME_MAX_DIM = 1288
_FRAME_JPEG_QUALITY = 82


def is_media_ext(ext: str) -> bool:
    return ext in AUDIO_EXTENSIONS or ext in VIDEO_EXTENSIONS


def extract_media(content: bytes, ext: str, filename: str) -> tuple[str, list[dict]]:
    """Blocking. Turn an audio/video file into a markdown transcript (plus
    on-screen text from sampled frames for video).

    Returns ``(text, frames)`` where ``frames`` is a list of retained video
    keyframes ``{"data": <base64 jpeg>, "media_type": "image/jpeg",
    "timestamp": <seconds>}`` — empty for audio and for video with no readable
    frames. Callers that only want text (e.g. the index pipeline) discard the
    second element."""
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=ext, delete=False) as tmp:
            tmp.write(content)
            tmp_path = tmp.name

        transcript = _transcribe_with_speakers(tmp_path)
        sections = [
            f"# Transcript of {filename}\n\n{transcript}"
            if transcript.strip()
            else f"# {filename}\n\n[No speech detected]"
        ]
        frames: list[dict] = []
        if ext in VIDEO_EXTENSIONS:
            notes, frames = _sample_video_frames(tmp_path)
            if notes:
                sections.append("# On-screen text from sampled frames\n\n" + notes)
        return "\n\n".join(sections), frames
    except Exception as e:
        log.warning("media extraction failed for %s: %s", filename, e)
        return f"[Error extracting {filename}: {e}]", []
    finally:
        if tmp_path:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


def _transcribe_with_speakers(path: str) -> str:
    import mlx_whisper
    import numpy as np

    from server.asr import whisper_backend
    from server.infrastructure.config import get as config_get

    # ffmpeg-backed loader: reads audio from any audio OR video container and
    # resamples to 16 kHz mono float32.
    audio = np.array(mlx_whisper.audio.load_audio(path), dtype=np.float32)

    kwargs = {
        "path_or_hf_repo": whisper_backend._ensure_model(),
        "temperature": 0.0,
        "compression_ratio_threshold": 2.4,
        "logprob_threshold": -1.0,
        "no_speech_threshold": 0.6,
        "condition_on_previous_text": False,
    }
    lang = config_get("whisper_language")
    if lang:
        kwargs["language"] = lang

    # Decode on the Whisper backend's single-worker executor rather than on
    # whatever thread is doing attachment/index extraction. That executor is
    # the one live ASR sessions decode on, so routing through it means: (a) the
    # mlx-whisper ModelHolder is loaded exactly once and shared (no second,
    # unlocked model load), and (b) media transcription is serialized with live
    # recording instead of running MLX concurrently with it.
    def _do_transcribe():
        return mlx_whisper.transcribe(audio, **kwargs)

    result = whisper_backend.executor.submit(_do_transcribe).result()
    segments = result.get("segments") or []
    if not segments:
        return (result.get("text") or "").strip()

    labels = _diarize(audio, segments)
    lines = []
    for seg, label in zip(segments, labels, strict=False):
        text = (seg.get("text") or "").strip()
        if not text:
            continue
        lines.append(f"[{_fmt_ts(seg.get('start', 0))}] {label}: {text}")
    return "\n".join(lines)


def _diarize(audio, segments) -> list:
    """Assign a speaker label per segment via ECAPA embeddings + the existing
    agglomerative clusterer. Falls back to a single speaker on any failure."""
    import numpy as np

    n = len(segments)
    try:
        from server.diarization.speakers import MATCH_THRESHOLD, _agglomerative, embed
    except Exception as e:
        log.warning("diarization unavailable (%s); labeling as one speaker", e)
        return ["Speaker 1"] * n

    embeddings = []
    idxs = []
    for i, seg in enumerate(segments):
        clip = audio[int(seg.get("start", 0) * _SR) : int(seg.get("end", 0) * _SR)]
        vec = embed(np.ascontiguousarray(clip))  # None for clips under ~1 s
        if vec is not None:
            embeddings.append(vec)
            idxs.append(i)

    labels = ["Speaker 1"] * n
    if len(embeddings) < 2:
        return labels

    try:
        groups = _agglomerative(
            np.stack(embeddings),
            distance_threshold=1.0 - MATCH_THRESHOLD,
            expected=None,
        )
    except Exception as e:
        log.warning("speaker clustering failed (%s); labeling as one speaker", e)
        return labels

    # Number speakers by first appearance so labels read in order.
    order: dict = {}
    nxt = 1
    seg_label: dict = {}
    for seg_i, g in zip(idxs, [int(x) for x in groups], strict=False):
        if g not in order:
            order[g] = nxt
            nxt += 1
        seg_label[seg_i] = f"Speaker {order[g]}"

    # Segments too short to embed inherit the previous segment's label.
    last = "Speaker 1"
    for i in range(n):
        if i in seg_label:
            last = seg_label[i]
        labels[i] = last
    return labels


def _frame_interval(duration_sec: float) -> float:
    """Seconds between sampled frames. Every ~10 s on short clips; on longer
    videos the interval stretches so we never exceed _MAX_FRAMES retained
    frames (interval = duration / _MAX_FRAMES). A 30 s clip → every 10 s (3
    frames); a 200 s clip → every 10 s (20 frames); a 10 min clip → every 30 s
    (20 frames). Falls back to the baseline cadence when duration is unknown
    (the ffmpeg -frames:v cap still bounds the count)."""
    if duration_sec and duration_sec > 0:
        return max(float(_FRAME_INTERVAL_SEC), duration_sec / _MAX_FRAMES)
    return float(_FRAME_INTERVAL_SEC)


def _video_duration(path: str) -> float:
    """Video length in seconds via ffprobe, or 0.0 if it can't be determined."""
    try:
        out = subprocess.run(
            [
                "ffprobe",
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                path,
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return float((out.stdout or "").strip() or 0)
    except (ValueError, OSError):
        return 0.0


def _encode_frame(img) -> str:
    """Downscale a PIL frame to <= _FRAME_MAX_DIM and return a base64 JPEG.
    Empty string on any failure."""
    import base64
    import io

    from PIL import Image

    try:
        im = img.convert("RGB")
        longest = max(im.size)
        if longest > _FRAME_MAX_DIM:
            scale = _FRAME_MAX_DIM / longest
            im = im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, format="JPEG", quality=_FRAME_JPEG_QUALITY)
        return base64.b64encode(buf.getvalue()).decode()
    except Exception:
        return ""


def _sample_video_frames(path: str) -> tuple[str, list[dict]]:
    """Sample up to _MAX_FRAMES keyframes with ffmpeg on a single pass. Each
    frame is BOTH OCR'd (Apple Vision, on-device) for the transcript merge AND
    retained as a downscaled JPEG for vision models. Returns
    ``(ocr_notes_markdown, frames)``."""
    from PIL import Image

    # Apple Vision directly, not the escalating engine: a text-free video
    # must not fan out into one Haiku call per blank frame.
    try:
        from server.extract.ocr import _ocr_with_apple_vision
    except Exception:
        _ocr_with_apple_vision = None

    interval = _frame_interval(_video_duration(path))
    tmpdir = tempfile.mkdtemp(prefix="frames_")
    try:
        cmd = [
            "ffmpeg",
            "-nostdin",
            "-i",
            path,
            "-vf",
            f"fps=1/{interval}",
            "-frames:v",
            str(_MAX_FRAMES),
            os.path.join(tmpdir, "f%03d.png"),
        ]
        subprocess.run(cmd, capture_output=True, check=False)
        notes: list[str] = []
        frames: list[dict] = []
        for i, fp in enumerate(sorted(glob.glob(os.path.join(tmpdir, "f*.png")))):
            ts = i * interval
            try:
                img = Image.open(fp)
                img.load()
            except Exception:
                continue
            # On-screen text (for text-only models + the transcript merge).
            if _ocr_with_apple_vision is not None:
                try:
                    text = _ocr_with_apple_vision([img])
                    if text and text.strip():
                        notes.append(f"[{_fmt_ts(ts)}] {text.strip()}")
                except Exception:
                    pass
            # Retain the actual frame so vision models can see it, not just its
            # OCR'd text.
            data = _encode_frame(img)
            if data:
                frames.append(
                    {
                        "data": data,
                        "media_type": "image/jpeg",
                        "timestamp": round(ts, 1),
                    }
                )
        return "\n\n".join(notes), frames
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def _fmt_ts(sec) -> str:
    sec = int(sec or 0)
    return f"{sec // 60:02d}:{sec % 60:02d}"
