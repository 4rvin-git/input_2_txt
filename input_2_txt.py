#!/usr/bin/env python3
"""
link/local clip ► MP3 ► local Whisper transcript
===========================================

REQUIREMENTS (tested on Windows)
------------------------------------------
1. Python ≥ 3.11
2. ffmpeg.exe on PATH – https://www.gyan.dev/ffmpeg/builds/
3. Inside your venv run:

   pip install -U yt-dlp
   pip install git+https://github.com/openai/whisper.git@main
   # GPU users: install the matching CUDA wheel first, e.g.
   # pip install torch --index-url https://download.pytorch.org/whl/cu121

USER SETTINGS
-------------
Fill in the four variables below.  Leave OUTDIR empty ("") to default
to the script’s own directory.
"""

import contextlib
import io
import pathlib
import re
import sys
import time
from datetime import datetime
import subprocess
import shutil

import whisper
import yt_dlp

# ────────── USER SETTINGS ────────── #
SOURCE   = r"https://www.youtube.com/watch?v=L45Q1_psDqk"  # r"URL_or_local_path"
START_TS = "00:02:30"  # HH:MM:SS (empty → 00:00:00)
END_TS   = "00:03:50"  # HH:MM:SS (empty → clip end)
OUTDIR   = r"output"   # "" → script folder
# ─────────────────────────────────── #

TS_RE = re.compile(r"^(\d\d):([0-5]\d):([0-5]\d)$")
WHISPER_VERBOSE_RE = re.compile(
    r"\[\d{2}:\d{2}(?::\d{2})?\.\d{3}\s*-->\s*\d{2}:\d{2}(?::\d{2})?\.\d{3}\]\s+(.*)"
)

# Allow SOURCE override via CLI
if len(sys.argv) > 1:
    SOURCE = sys.argv[1]


# ───────── helpers ───────── #

def ts_to_sec(ts: str) -> int:
    m = TS_RE.match(ts)
    if not m:
        raise ValueError(f"Bad timestamp {ts!r}. Use HH:MM:SS.")
    h, m_, s = map(int, m.groups())
    return h * 3600 + m_ * 60 + s


def sec_to_ts(sec: int) -> str:
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def ensure_outdir(path_hint: str) -> pathlib.Path:
    d = pathlib.Path(path_hint) if path_hint else pathlib.Path(__file__).parent
    d.mkdir(parents=True, exist_ok=True)
    return d.resolve()


def get_video_duration(url: str) -> int | None:
    """Ask yt‑dlp (quietly) for duration in seconds."""
    opts = {'quiet': True, 'get_duration': True, 'nocheckcertificate': True}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            return int(ydl.extract_info(url, download=False).get('duration') or 0) or None
    except Exception:
        return None

def extract_audio_local(src: pathlib.Path, start: str, end: str, dest_mp3: pathlib.Path):
    """ffmpeg-trim any local mp3/mp4 to an MP3 clip."""
    print('[1/2] Extracting local clip…')
    cmd = [
        'ffmpeg', '-v', 'error',  # keep ffmpeg quiet
        '-ss', start, *(['-to', end] if end else []),
        '-i', str(src),
        '-vn', '-acodec', 'libmp3lame',  # audio-only → mp3
        str(dest_mp3)
    ]
    run = subprocess.run(cmd)
    if run.returncode != 0 or not dest_mp3.exists():
        raise RuntimeError('ffmpeg failed – see messages above.')
    print(f'✔ Clip saved to {dest_mp3}')


def get_local_duration(path: pathlib.Path) -> int | None:
    try:
        out = subprocess.check_output(
            ['ffprobe', '-v', 'error', '-show_entries',
             'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1',
             str(path)], text=True)
        return int(float(out.strip()))
    except Exception:
        return None



class Tee:
    """Duplicate stream to multiple outputs (e.g., file + stdout)."""
    def __init__(self, *streams):
        self.streams = streams

    def write(self, data):
        for s in self.streams:
            s.write(data)

    def flush(self):
        for s in self.streams:
            s.flush()


class YtdlpLogger:
    """Filter yt‑dlp log lines to match the original script’s vibe."""
    def __init__(self):
        self.extracting_announced = False

    def _log(self, msg):
        if msg.lstrip().startswith('[download]') and '%' in msg:
            return  # hide progress spam
        if 'Estimating duration from bitrate' in msg:
            print('[info] Estimating duration…')
            return
        if msg.startswith('[ExtractAudio]') and not self.extracting_announced:
            print('[info] Extracting…')
            self.extracting_announced = True
        print(msg)

    debug = info = warning = error = _log


# ───────── yt‑dlp download ───────── #

def download_audio(url: str, start: str, end: str, dest_mp3: pathlib.Path):
    """Invoke the classic yt‑dlp command that always worked, and ensure audio.mp3 appears."""
    print('[1/2] Downloading & extracting clip…')

    cmd = [
        sys.executable, '-m', 'yt_dlp',
        url,
        '-S', 'hasaud,+filesize',
        '-x', '--audio-format', 'mp3',
        '--download-sections', f"*{start}-{end}",
        '-o', 'audio.%(ext)s',
        '-P', str(dest_mp3.parent)
    ]

    # inherit stdout/stderr → full logging visible (incl. keyboard buffer)
    run = subprocess.run(cmd)
    if run.returncode != 0:
        raise RuntimeError('yt‑dlp failed – see messages above.')

    if not dest_mp3.exists():
        raise RuntimeError('yt‑dlp finished but audio.mp3 not found.')

    print(f'✔ Clip saved to {dest_mp3}')


# ───────── Whisper transcription ───────── #

def transcribe(mp3_path: pathlib.Path, txt_path: pathlib.Path):
    print('\n[2/2] Transcribing audio …')
    t0 = time.time()
    model_name = 'small'

    try:
        import torch
        device = 'cuda' if torch.cuda.is_available() else 'cpu'
    except ImportError:
        device = 'cpu'

    print(f'▶ Loading Whisper "{model_name}" on {device.upper()} …')
    model = whisper.load_model(model_name, device=device)

    captured = io.StringIO()
    status = 'Unknown'

    try:
        tee = Tee(sys.stdout, captured)
        with contextlib.redirect_stdout(tee):
            result = model.transcribe(str(mp3_path), verbose=True)

        print('\n▶ Transcription complete – writing file…')
        txt_path.write_text('\n'.join(seg['text'].strip() for seg in result['segments']), encoding='utf-8')
        status = 'Completed'

    except KeyboardInterrupt:
        print('\n▶ Keyboard interrupt – saving partial transcript…')
        lines = WHISPER_VERBOSE_RE.findall(captured.getvalue())
        partial = '\n'.join(lines) if lines else captured.getvalue()
        txt_path.write_text(partial, encoding='utf-8')
        status = 'Interrupted (Partial)'
        raise  # re‑raise so main() knows the run was stopped

    finally:
        dt = time.time() - t0
        print(f"\n✔ Transcript ({status}) saved to: {txt_path}")
        print(f"  Took {dt/60:.1f} min ({dt:.0f} s).")


# ──────────── main ──────────── #
def main():
    print('--- SCRIPT START ---')
    try:
        out_root = ensure_outdir(OUTDIR)
        run_dir  = out_root / f"run_{datetime.now():%Y%m%d_%H%M%S}"
        run_dir.mkdir()
        print(f'▶ Output → {run_dir}')

        mp3_path = run_dir / 'audio.mp3'
        txt_path = run_dir / 'transcript.txt'

        src_path = pathlib.Path(SOURCE).expanduser()
        is_local = src_path.is_file()

        # — determine duration & clip range —
        if is_local:
            dur = get_local_duration(src_path)
        else:
            dur = get_video_duration(SOURCE)

        if dur:
            s_sec = ts_to_sec(START_TS) if START_TS else 0
            e_sec = ts_to_sec(END_TS) if END_TS else dur
            if e_sec > dur:
                e_sec = dur
            if s_sec >= e_sec:
                s_sec, e_sec = 0, dur
            start, end = sec_to_ts(s_sec), sec_to_ts(e_sec)
        else:
            if not START_TS or not END_TS:
                raise ValueError('Unknown media length – specify both START_TS and END_TS.')
            start, end = START_TS, END_TS

        print(f'▶ Clip range: {start} → {end}')

        # — fetch or extract audio —
        if is_local:
            extract_audio_local(src_path.resolve(), start, end, mp3_path)
        else:
            download_audio(SOURCE, start, end, mp3_path)

        # — Whisper transcription —
        transcribe(mp3_path, txt_path)
        print('\n--- SCRIPT FINISHED SUCCESSFULLY ---')

    except KeyboardInterrupt:
        print('\n--- SCRIPT END (INTERRUPTED BY USER) ---')
        sys.exit(0)
    except Exception as e:
        print('\n--- SCRIPT FAILED ---')
        print(e, file=sys.stderr)
        sys.exit(1)



if __name__ == '__main__':
    main()
