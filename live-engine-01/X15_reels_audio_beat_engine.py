#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN PEMILIH MUZIK & RENTAK LIBROSA (X15)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X15_reels_audio_beat_engine.py
#
# FUNGSI UTAMA:
# 1. Mengimbas koleksi 200+ fail audio/video muzik tempatan di dalam folder
#    /home/braderdin/ps2-retro-nostalgia/assets/music/.
# 2. Mengelakkan lagu berulang menggunakan penggiliran adil Upstash Redis
#    (`ps2n_reels`) + kunci 60 hari bagi setiap trek yang telah digunakan.
# 3. Mengekstrak metadata ID3/MP4 (Tajuk, Artis, Genre) melalui Mutagen.
# 4. Menyahkod gelombang audio melalui MoviePy dan menganalisis tempo BPM
#    serta cap masa ketukan rentak (*beat timestamps*) menggunakan Librosa.
# 5. Memotong segmen audio tepat mengikut sasaran durasi video Reels (30-50s).
# ==============================================================================

import hashlib
import importlib
import random
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import numpy as np
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

CURRENT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = CURRENT_DIR.parent
for p in [CURRENT_DIR, PROJECT_ROOT]:
    if p.exists() and str(p) not in sys.path:
        sys.path.insert(0, str(p))

_redis_mod: Any = importlib.import_module("X04_redis_sharded")
PS2RedisShardedManager: Any = getattr(_redis_mod, "PS2RedisShardedManager")
reels_redis = PS2RedisShardedManager(namespace="ps2n_reels")

try:
    from mutagen import File as MutagenFile
except ImportError:
    MutagenFile = None

librosa: Any = None
try:
    import librosa as _librosa
    librosa = _librosa
    LIBROSA_AVAILABLE = True
except ImportError:
    LIBROSA_AVAILABLE = False

try:
    from moviepy import AudioFileClip
except ImportError:
    from moviepy.editor import AudioFileClip  # type: ignore

MUSIC_DIR: Path = PROJECT_ROOT / "assets" / "music"
SUPPORTED_AUDIO_EXTS = (".mp3", ".mp4", ".m4a", ".wav", ".aac", ".ogg")
MUSIC_COOLDOWN_SECONDS: int = 60 * 86400  # 60 Hari supaya 204 lagu bergilir tanpa ulangan rapat


def _track_redis_id(filename: str) -> str:
    return hashlib.sha1(filename.strip().lower().encode("utf-8")).hexdigest()[:16]


def list_available_music_files() -> List[Path]:
    """Menyenaraikan semua fail muzik sah di dalam assets/music/."""
    if not MUSIC_DIR.exists():
        raise FileNotFoundError(f"Folder muzik tidak ditemui: {MUSIC_DIR}")
    tracks = sorted(
        [p for p in MUSIC_DIR.iterdir() if p.is_file() and p.suffix.lower() in SUPPORTED_AUDIO_EXTS]
    )
    if not tracks:
        raise RuntimeError(f"Tiada fail muzik dijumpai di {MUSIC_DIR}")
    return tracks


def extract_track_metadata(song_path: Path) -> Dict[str, str]:
    """Membaca tajuk, artis dan genre fail audio menggunakan Mutagen."""
    clean_stem = re.sub(r"[_\-]+", " ", song_path.stem).strip()
    clean_stem = re.sub(r"\b(30s|40s|50s|60s|loop|reels|audio|sound)\b", "", clean_stem, flags=re.I)
    clean_stem = re.sub(r"\s+", " ", clean_stem).strip().title()

    title = clean_stem or "Retro Nostalgia Ambient"
    artist = "Meta Sound Collection"
    genre = "Instrumental"

    if MutagenFile is not None and song_path.exists():
        try:
            tag = MutagenFile(str(song_path))
            if tag and hasattr(tag, "get"):
                t_raw = tag.get("\xa9nam") or tag.get("TIT2") or tag.get("title")
                a_raw = tag.get("\xa9ART") or tag.get("TPE1") or tag.get("artist")
                g_raw = tag.get("\xa9gen") or tag.get("TCON") or tag.get("genre")
                if isinstance(t_raw, list):
                    t_raw = t_raw[0]
                if isinstance(a_raw, list):
                    a_raw = a_raw[0]
                if isinstance(g_raw, list):
                    g_raw = g_raw[0]
                if t_raw and not str(t_raw).strip().isdigit():
                    title = str(t_raw).strip()
                if a_raw and not str(a_raw).strip().isdigit():
                    artist = str(a_raw).strip()
                if g_raw:
                    genre = str(g_raw).strip()
        except Exception:
            pass

    return {
        "title": title,
        "artist": artist,
        "genre": genre,
        "filename": song_path.name,
        "file_path": str(song_path),
    }


def analyze_audio_bpm_and_beats(audio_clip: Any, max_scan_sec: float = 48.0) -> Tuple[float, List[float]]:
    """Menganalisis tempo (BPM) dan senarai masa ketukan rentak melalui Librosa."""
    if not LIBROSA_AVAILABLE or librosa is None:
        return 0.0, []

    sr = 22050
    try:
        full_dur = float(audio_clip.duration or 0.0)
        if full_dur <= 2.0:
            return 0.0, []

        scan_end = min(full_dur, max_scan_sec)
        sub_fn = getattr(audio_clip, "subclipped", None) or getattr(audio_clip, "subclip")
        sub_clip = sub_fn(0, scan_end)
        sound_arr = sub_clip.to_soundarray(fps=sr)
        sub_clip.close()

        mono_y = (
            np.mean(sound_arr, axis=1).astype(np.float32)
            if sound_arr.ndim > 1
            else sound_arr.astype(np.float32)
        )
        tempo_raw, beat_frames = librosa.beat.beat_track(y=mono_y, sr=sr)
        tempo_any: Any = tempo_raw
        bpm_val = round(float(tempo_any.item()) if hasattr(tempo_any, "item") else float(tempo_any), 1)
        beat_times = [round(float(t), 2) for t in librosa.frames_to_time(beat_frames, sr=sr).tolist()]
        return bpm_val, beat_times
    except Exception as exc:
        console.print(f"[yellow]⚠️ Amaran Librosa BPM: {exc}[/yellow]")
        return 0.0, []


def classify_music_mood(title: str, bpm: float) -> Tuple[str, str]:
    """Mengkelaskan suasana muzik untuk rujukan gaya penceritaan Persona AI."""
    low = title.lower()
    if any(k in low for k in ("night", "rain", "moon", "lofi", "dream", "calm", "soft", "mist", "peace")):
        return "CHILL_NOSTALGIA", f"Alunan Tenang & Mengimbau Kenangan ({bpm} BPM)"
    if any(k in low for k in ("energy", "action", "run", "power", "drive", "neon", "cyber", "upbeat")):
        return "ENERGETIC_RETRO", f"Rentak Bertenaga & Lincah ({bpm} BPM)"

    if bpm >= 120.0:
        return "ENERGETIC_RETRO", f"Rentak Ceria & Dinamik ({bpm} BPM)"
    if bpm >= 88.0:
        return "GROOVY_SANTAI", f"Alunan Sederhana Santai ({bpm} BPM)"
    return "CHILL_NOSTALGIA", f"Muzik Ambient / Lo-Fi Tenang ({bpm} BPM)"


def prepare_reels_music_track(target_duration_sec: int = 38) -> Tuple[Any, Dict[str, Any]]:
    """
    Memilih 1 trek muzik segar daripada 200+ koleksi di assets/music/,
    menganalisis tempo & ketukan Librosa, dan memotong segmen audio tepat
    mengikut `target_duration_sec`.
    """
    all_tracks = list_available_music_files()
    total_library = len(all_tracks)

    # Tapis trek yang belum digunakan dalam 60 hari daripada sampel rawak 35 lagu
    sample_candidates = random.sample(all_tracks, min(35, total_library))
    fresh_pool: List[Path] = []
    for tr in sample_candidates:
        tid = _track_redis_id(tr.name)
        is_used, _ = reels_redis.is_duplicate_fingerprint(f"music_track:{tid}")
        if not is_used:
            fresh_pool.append(tr)

    active_pool = fresh_pool if fresh_pool else sample_candidates
    by_id = {_track_redis_id(p.name): p for p in active_pool}

    picked_id = reels_redis.pick_least_used_item(
        pool_name="reels_music_tracks",
        candidate_ids=list(by_id.keys()),
    )
    chosen_path = by_id.get(picked_id or "", random.choice(active_pool))
    chosen_tid = _track_redis_id(chosen_path.name)

    meta = extract_track_metadata(chosen_path)
    raw_audio = AudioFileClip(str(chosen_path))
    full_dur = float(raw_audio.duration or 0.0)

    bpm, beat_times = analyze_audio_bpm_and_beats(raw_audio, max_scan_sec=float(target_duration_sec + 6))
    energy_tag, vibe_bm = classify_music_mood(meta["title"], bpm)

    start_sec = 0.0
    if full_dur > target_duration_sec + 4:
        start_sec = float(random.randint(0, int(full_dur - target_duration_sec - 2)))
    end_sec = min(full_dur, start_sec + float(target_duration_sec))

    cut_fn = getattr(raw_audio, "subclipped", None) or getattr(raw_audio, "subclip")
    cut_audio = cut_fn(start_sec, end_sec)

    music_info: Dict[str, Any] = {
        "track_id": chosen_tid,
        "filename": chosen_path.name,
        "file_path": str(chosen_path),
        "title": meta["title"],
        "artist": meta["artist"],
        "genre": meta["genre"],
        "library_total": total_library,
        "full_duration_sec": round(full_dur, 1),
        "clip_start_sec": round(start_sec, 1),
        "clip_duration_sec": round(end_sec - start_sec, 1),
        "bpm": bpm,
        "beat_count": len(beat_times),
        "beat_timestamps": beat_times,
        "energy_tag": energy_tag,
        "vibe_bm": vibe_bm,
    }
    return cut_audio, music_info


def mark_music_track_used(music_info: Dict[str, Any], post_uid: str) -> None:
    """Merekodkan penggunaan lagu ke Redis supaya tidak berulang dalam 60 hari."""
    tid = str(music_info.get("track_id", "")).strip()
    if not tid:
        return
    reels_redis.mark_dedup_fingerprint(
        f"music_track:{tid}",
        post_uid=post_uid,
        ttl_seconds=MUSIC_COOLDOWN_SECONDS,
    )
    reels_redis.increment_rotation_score("reels_music_tracks", tid)


if __name__ == "__main__":
    aud, info = prepare_reels_music_track(target_duration_sec=36)
    try:
        tbl = Table(title="Ujian Kendiri X15 :: Enjin Muzik & Librosa", box=box.ROUNDED, header_style="bold green")
        tbl.add_column("Medan", style="bold white")
        tbl.add_column("Nilai", style="cyan")
        tbl.add_row("Koleksi Muzik", f"{info['library_total']} fail di {MUSIC_DIR}")
        tbl.add_row("Lagu Terpilih", f"{info['title']} ({info['filename']})")
        tbl.add_row("Artis / Genre", f"{info['artist']} | {info['genre']}")
        tbl.add_row("Tempo & Ketukan", f"{info['bpm']} BPM ({info['beat_count']} ketukan)")
        tbl.add_row("Vibe AI", f"[{info['energy_tag']}] {info['vibe_bm']}")
        console.print(tbl)
    finally:
        if aud:
            aud.close()