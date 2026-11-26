#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN SEMANTIK UPSTASH VECTOR 5-SHARD (X05)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X05_vector_sharded.py
#
# FUNGSI:
# 1. Mengagihkan vektor cerita PS2 merentasi 5 indeks Upstash Vector mengikut
#    hash tajuk game (CRC32 % 5) supaya carian pendua hanya guna 1 kuota/semakan.
# 2. Menyokong Upstash Hosted Embedding (data=text) serta sandaran vektor L2
#    1536-dimensi secara automatik mengikut tetapan indeks.
# 3. Menapis cerita atau fakta PS2 yang berulang berdasarkan skor keserupaan
#    kosain (cosine similarity threshold).
# ==============================================================================

import hashlib
import math
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from upstash_vector import Index as VectorIndex

console = Console()

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import X01_config as cfg
from X02_sqlite_ledger import ledger_db


def normalize_text_for_embedding(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", " ", text.lower())
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return " ".join(cleaned.split())


def build_local_semantic_vector(text: str, dimension: int = 1536) -> List[float]:
    """
    Menjana vektor ciri semantik L2-normalized (default 1536 dimensi) daripada
    gabungan kata kunci dan n-gram aksara sekiranya indeks Upstash Vector
    tidak mengaktifkan model hosted embedding.
    """
    norm_text = normalize_text_for_embedding(text)
    if not norm_text:
        norm_text = "ps2 retro nostalgia"

    vec = [0.0] * dimension
    words = norm_text.split()

    # 1. Pemberat tinggi (2.5) untuk perkataan penuh & frasa 2-perkataan
    features: List[Tuple[str, float]] = [(w, 2.5) for w in words if len(w) >= 2]
    for i in range(len(words) - 1):
        features.append((f"{words[i]}_{words[i + 1]}", 3.0))

    # 2. Pemberat asas (1.0) untuk trigram aksara (menangkap variasi ejaan/imbuhan BM)
    padded = f" {norm_text} "
    for i in range(len(padded) - 2):
        features.append((padded[i : i + 3], 1.0))

    for feat, weight in features:
        digest = hashlib.sha256(feat.encode("utf-8")).digest()
        bucket = int.from_bytes(digest[:4], "big") % dimension
        sign = 1.0 if (digest[4] & 1) == 0 else -1.0
        vec[bucket] += sign * weight

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0.0:
        vec = [v / norm for v in vec]
    return vec


class PS2VectorShardedManager:
    def __init__(self, accounts: List[Dict[str, Any]] = cfg.VECTOR_ACCOUNTS):
        self.accounts = accounts
        self._clients: Dict[int, VectorIndex] = {}
        # Cache mod embedding per indeks: 'data' atau 'vector'
        self._embed_mode_cache: Dict[int, str] = {}
        self._dim_cache: Dict[int, int] = {}

    def _get_client(self, shard: Dict[str, Any]) -> VectorIndex:
        idx = int(shard["index"])
        if idx not in self._clients:
            self._clients[idx] = VectorIndex(url=shard["url"], token=shard["token"])
        return self._clients[idx]

    def _get_dimension(self, shard: Dict[str, Any]) -> int:
        idx = int(shard["index"])
        if idx not in self._dim_cache:
            client = self._get_client(shard)
            info = client.info()
            self._dim_cache[idx] = int(getattr(info, "dimension", 1536) or 1536)
        return self._dim_cache[idx]

    def _upsert_adaptive(
        self,
        shard: Dict[str, Any],
        vec_id: str,
        semantic_text: str,
        metadata: Dict[str, Any],
    ) -> None:
        idx = int(shard["index"])
        client = self._get_client(shard)
        cached_mode = self._embed_mode_cache.get(idx)

        if cached_mode != "vector":
            try:
                client.upsert(
                    vectors=[
                        {
                            "id": vec_id,
                            "data": semantic_text,
                            "metadata": metadata,
                        }
                    ]
                )
                self._embed_mode_cache[idx] = "data"
                return
            except Exception:
                self._embed_mode_cache[idx] = "vector"

        dim = self._get_dimension(shard)
        dense_vec = build_local_semantic_vector(semantic_text, dimension=dim)
        client.upsert(
            vectors=[
                {
                    "id": vec_id,
                    "vector": dense_vec,
                    "metadata": metadata,
                }
            ]
        )

    def _query_adaptive(
        self,
        shard: Dict[str, Any],
        semantic_text: str,
        top_k: int = 3,
    ) -> List[Any]:
        idx = int(shard["index"])
        client = self._get_client(shard)
        cached_mode = self._embed_mode_cache.get(idx)

        if cached_mode != "vector":
            try:
                res = client.query(
                    data=semantic_text,
                    top_k=top_k,
                    include_metadata=True,
                )
                self._embed_mode_cache[idx] = "data"
                return list(res or [])
            except Exception:
                self._embed_mode_cache[idx] = "vector"

        dim = self._get_dimension(shard)
        dense_vec = build_local_semantic_vector(semantic_text, dimension=dim)
        res = client.query(
            vector=dense_vec,
            top_k=top_k,
            include_metadata=True,
        )
        return list(res or [])

    # --------------------------------------------------------------------------
    # OPERASI UTAMA: SIMPAN VEKTOR CERITA & SEMAK PENDUA SEMANTIK
    # --------------------------------------------------------------------------
    def upsert_story_vector(
        self,
        post_uid: str,
        game_title: str,
        story_text: str,
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, int]:
        """
        Menyimpan vektor cerita PS2 ke indeks sasaran mengikut hash tajuk game.
        Memulangkan: (berjaya, index_vector_digunakan)
        """
        shard = ledger_db.select_balanced_vector_shard(game_title)
        shard_idx = int(shard["index"])
        combined_text = f"{game_title.strip()} | {story_text.strip()}"

        metadata: Dict[str, Any] = {
            "post_uid": post_uid,
            "game_title": game_title.strip(),
            "snippet": story_text.strip()[:220],
            "vector_shard_index": shard_idx,
            "created_at": int(time.time()),
        }
        if extra_meta:
            metadata.update(extra_meta)

        try:
            self._upsert_adaptive(shard, post_uid, combined_text, metadata)
            ledger_db.record_account_activity("vector", shard_idx, ops_delta=1)
            return True, shard_idx
        except Exception as exc:
            console.print(f"[bold red]❌ Ralat upsert Upstash Vector #{shard_idx:03d}: {exc}[/bold red]")
            return False, shard_idx

    def check_semantic_duplicate(
        self,
        game_title: str,
        candidate_story: str,
        similarity_threshold: float = 0.86,
    ) -> Tuple[bool, Optional[Dict[str, Any]], int]:
        """
        Menyemak sama ada idea atau cerita PS2 terlalu serupa dengan hantaran lepas
        di dalam indeks game yang sama (hanya guna 1 kuota carian).
        Memulangkan: (adalah_pendua, butiran_padanan, index_vector)
        """
        shard = ledger_db.select_balanced_vector_shard(game_title)
        shard_idx = int(shard["index"])
        combined_text = f"{game_title.strip()} | {candidate_story.strip()}"

        try:
            matches = self._query_adaptive(shard, combined_text, top_k=3)
            ledger_db.record_account_activity("vector", shard_idx, ops_delta=1)

            if not matches:
                return False, None, shard_idx

            top_match = matches[0]
            score = float(getattr(top_match, "score", 0.0) or 0.0)
            meta = getattr(top_match, "metadata", None) or {}
            match_id = str(getattr(top_match, "id", "") or "")

            match_info = {
                "matched_id": match_id,
                "score": round(score, 4),
                "game_title": meta.get("game_title", ""),
                "snippet": meta.get("snippet", ""),
                "post_uid": meta.get("post_uid", match_id),
            }

            if score >= similarity_threshold:
                return True, match_info, shard_idx
            return False, match_info, shard_idx

        except Exception as exc:
            console.print(f"[bold yellow]⚠️ Amaran carian Vector #{shard_idx:03d}: {exc}[/bold yellow]")
            return False, None, shard_idx

    def delete_story_vector(self, post_uid: str, game_title: str) -> bool:
        """Memadam rekod vektor daripada indeks game berkaitan."""
        shard = ledger_db.select_balanced_vector_shard(game_title)
        shard_idx = int(shard["index"])
        try:
            client = self._get_client(shard)
            client.delete(ids=[post_uid])
            ledger_db.record_account_activity("vector", shard_idx, ops_delta=1)
            return True
        except Exception:
            return False

    def inspect_all_indexes(self) -> List[Dict[str, Any]]:
        """Memeriksa status kesihatan dan bilangan vektor bagi semua 5 akaun."""
        summaries: List[Dict[str, Any]] = []
        for shard in self.accounts:
            idx = int(shard["index"])
            t0 = time.perf_counter()
            try:
                client = self._get_client(shard)
                info = client.info()
                latency_ms = (time.perf_counter() - t0) * 1000
                dim = int(getattr(info, "dimension", 0) or 0)
                self._dim_cache[idx] = dim or 1536
                summaries.append(
                    {
                        "index": idx,
                        "host": shard["url"].replace("https://", ""),
                        "online": True,
                        "dimension": dim,
                        "metric": str(getattr(info, "similarity_function", "-") or "-"),
                        "vector_count": int(getattr(info, "vector_count", 0) or 0),
                        "latency_ms": latency_ms,
                    }
                )
            except Exception:
                summaries.append(
                    {
                        "index": idx,
                        "host": shard["url"].replace("https://", ""),
                        "online": False,
                        "dimension": 0,
                        "metric": "-",
                        "vector_count": 0,
                        "latency_ms": (time.perf_counter() - t0) * 1000,
                    }
                )
        return summaries


# Singleton Instance untuk diimport oleh modul X07..X09
vector_sharded = PS2VectorShardedManager()


# ==============================================================================
# DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ==============================================================================
if __name__ == "__main__":
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: UPSTASH VECTOR 5-INDEX SEMANTIC TEST (X05)[/bold cyan]",
            border_style="cyan",
        )
    )

    sample_game = "GTA San Andreas"
    sample_uid = f"vec_probe_{int(time.time())}"
    original_story = (
        "Ingat lagi misi cari misteri Bigfoot kat hutan Back O Beyond dalam GTA San Andreas PS2? "
        "Sebelum masuk hutan wajib simpan save data dulu dalam memory card 8MB takut PS2 hang."
    )
    similar_story = (
        "Kenangan cari legenda Bigfoot di hutan Back O Beyond masa main GTA San Andreas kat PS2, "
        "siap simpan save data dalam kad memori 8MB sebelum meronda waktu malam."
    )
    different_game = "Winning Eleven 8"
    different_story = (
        "Zaman main Winning Eleven 8 kat kedai game, kalau dapat sepakan percuma terus pilih "
        "Roberto Carlos sebab rembatan padu dia memang susah penjaga gol nak tepis."
    )

    up_ok, used_idx = vector_sharded.upsert_story_vector(
        post_uid=sample_uid,
        game_title=sample_game,
        story_text=original_story,
    )

    # Beri jeda ringkas 1 saat untuk indeks Upstash Vector memuktamadkan kemas kini
    time.sleep(1.0)

    dup_detected, dup_info, _ = vector_sharded.check_semantic_duplicate(
        game_title=sample_game,
        candidate_story=similar_story,
        similarity_threshold=0.80,
    )
    diff_detected, diff_info, diff_idx = vector_sharded.check_semantic_duplicate(
        game_title=different_game,
        candidate_story=different_story,
        similarity_threshold=0.80,
    )

    clean_ok = vector_sharded.delete_story_vector(post_uid=sample_uid, game_title=sample_game)
    all_stats = vector_sharded.inspect_all_indexes()

    tbl = Table(box=box.ROUNDED, header_style="bold yellow")
    tbl.add_column("Indeks", justify="center", style="cyan")
    tbl.add_column("Host Upstash Vector", style="white")
    tbl.add_column("Dimensi", justify="right", style="yellow")
    tbl.add_column("Metrik", justify="center", style="magenta")
    tbl.add_column("Bil. Vektor", justify="right", style="white")
    tbl.add_column("Latensi", justify="right", style="dim")
    tbl.add_column("Status", justify="center")

    for st in all_stats:
        tbl.add_row(
            f"#{st['index']:03d}",
            st["host"],
            str(st["dimension"]),
            st["metric"],
            str(st["vector_count"]),
            f"{st['latency_ms']:.0f} ms",
            "[bold green]ONLINE[/bold green]" if st["online"] else "[bold red]OFFLINE[/bold red]",
        )
    console.print(tbl)

    sim_score = dup_info.get("score", 0.0) if dup_info else 0.0
    diff_score = diff_info.get("score", 0.0) if diff_info else 0.0

    console.print(
        Panel(
            f"• Upsert Vektor Cerita (Indeks #{used_idx:03d}): [bold green]{'BERJAYA' if up_ok else 'GAGAL'}[/bold green]\n"
            f"• Ujian Cerita Serupa (GTA SA): Skor [bold yellow]{sim_score}[/bold yellow] -> "
            f"[bold green]{'PENDUA DIKESAN (DITAPIS)' if dup_detected else 'LULUS'}[/bold green]\n"
            f"• Ujian Game Berbeza (WE8 -> Indeks #{diff_idx:03d}): Skor [bold cyan]{diff_score}[/bold cyan] -> "
            f"[bold green]{'UNIK (DIBENARKAN)' if not diff_detected else 'TERTAPIS'}[/bold green]\n"
            f"• Pembersihan Vektor Ujian: [bold green]{'BERSIH' if clean_ok else 'RALAT'}[/bold green]",
            title="Ringkasan Ujian Semantik X05",
            border_style="green",
        )
    )