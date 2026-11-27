#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN SEMANTIK UPSTASH VECTOR 5-SHARD (X05)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X05_vector_sharded.py
#
# FUNGSI UTAMA (SERASI UNTUK SEMUA PERSONA, MEDIA & PROJEK MASA HADAPAN):
# 1. Mengagihkan vektor merentasi 5 indeks Upstash Vector mengikut hash kunci
#    topik/kategori (CRC32 % 5) supaya setiap carian hanya guna 1 kuota harian.
# 2. Mod Adaptif: Mencuba Hosted Embedding Upstash (`data=...`) dan beralih secara
#    automatik kepada vektor tempatan ternormalisasi L2 berdimensi 1536 (`vector=...`).
# 3. Penapis Semantik Bertingkat Berasaskan Umur Post:
#    - Ayat 100% / hampir sama (skor >= 0.95) -> dijarakkan minimum 60 hari (2 bulan).
#    - Ayat 75% - 80% sama (skor >= 0.75) -> dijarakkan minimum 5 hari.
# 4. Pengkelasan & Carian Semantik Media Visual (Gambar Unsplash & Video Reels):
#    Mengkelaskan aset ke kategori (`cat_controller`, `cat_console_setup`,
#    `cat_multiplayer`, `cat_storage_tech`, `cat_reels_clip`) dan mencari gambar
#    yang seragam/hampir sama untuk hantaran multi-foto.
# 5. Sokongan Multi-Namespace (`namespace="ps2n"`) untuk digunakan semula dalam
#    projek-projek lain pada masa hadapan tanpa pertindihan ID.
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


# Prototaip kategori visual untuk pengkelasan semantik automatik tanpa kos API
MEDIA_CATEGORY_PROTOTYPES: Dict[str, str] = {
    "cat_controller": (
        "playstation dualshock controller gamepad analog buttons close up hand holding joystick retro controller"
    ),
    "cat_console_setup": (
        "playstation console television crt tv retro gaming desk setup living room console hardware"
    ),
    "cat_multiplayer": (
        "two friends playing video games together couch co-op multiplayer gaming night session"
    ),
    "cat_storage_tech": (
        "usb flash drive pendrive portable external hard drive hdd memory card storage cable hardware"
    ),
    "cat_reels_clip": (
        "retro arcade gameplay neon screen action video clip vertical gaming motion"
    ),
}


def normalize_text_for_embedding(text: str) -> str:
    cleaned = re.sub(r"https?://\S+", " ", text.lower())
    cleaned = re.sub(r"[^a-z0-9\s]", " ", cleaned)
    return " ".join(cleaned.split())


def build_local_semantic_vector(text: str, dimension: int = 1536) -> List[float]:
    """
    Menjana vektor ciri semantik L2-normalized (default 1536 dimensi) daripada
    gabungan kata kunci, frasa 2-perkataan, dan trigram aksara sekiranya indeks
    Upstash Vector dikonfigurasikan untuk vektor mentah.
    """
    norm_text = normalize_text_for_embedding(text)
    if not norm_text:
        norm_text = "ps2 retro nostalgia"

    vec = [0.0] * dimension
    words = norm_text.split()

    # 1. Pemberat tinggi untuk perkataan penuh (2.5) & frasa 2-perkataan (3.0)
    features: List[Tuple[str, float]] = [(w, 2.5) for w in words if len(w) >= 2]
    for i in range(len(words) - 1):
        features.append((f"{words[i]}_{words[i + 1]}", 3.0))

    # 2. Pemberat asas (1.0) untuk trigram aksara (menangkap variasi imbuhan/ejaan BM)
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


def compute_local_cosine_similarity(text_a: str, text_b: str, dimension: int = 512) -> float:
    """Mengira persamaan kosain tempatan pantas antara dua teks (0.0 - 1.0)."""
    va = build_local_semantic_vector(text_a, dimension=dimension)
    vb = build_local_semantic_vector(text_b, dimension=dimension)
    dot = sum(x * y for x, y in zip(va, vb))
    return round(max(0.0, min(1.0, dot)), 4)


def classify_media_category(description_text: str, ocr_text: str = "") -> Tuple[str, float]:
    """
    Mengkelaskan gambar atau video secara automatik kepada salah satu kategori visual:
    `cat_controller`, `cat_console_setup`, `cat_multiplayer`, `cat_storage_tech`,
    atau `cat_reels_clip` berdasarkan persamaan semantik tempatan.
    """
    combined = f"{description_text} {ocr_text}".strip()
    if not combined:
        return "cat_controller", 0.0

    best_cat = "cat_controller"
    best_score = -1.0
    for cat_key, proto_desc in MEDIA_CATEGORY_PROTOTYPES.items():
        score = compute_local_cosine_similarity(combined, proto_desc, dimension=512)
        if score > best_score:
            best_score = score
            best_cat = cat_key

    return best_cat, round(max(0.0, best_score), 4)


class PS2VectorShardedManager:
    """
    Pengurus Semantik Berpusat Upstash Vector 5-Indeks (Multi-Namespace & Time-Aware).
    Boleh diimport oleh mana-mana modul persona atau projek lain melalui:
        from X05_vector_sharded import vector_sharded
    atau dengan namespace khusus untuk projek baharu:
        custom_vec = PS2VectorShardedManager(namespace="projek_baru")
    """

    def __init__(
        self,
        accounts: List[Dict[str, Any]] = cfg.VECTOR_ACCOUNTS,
        namespace: str = "ps2n",
    ) -> None:
        self.accounts = accounts
        self.namespace = re.sub(r"[^a-zA-Z0-9_-]", "", namespace).lower() or "ps2n"
        self._clients: Dict[int, VectorIndex] = {}
        self._embed_mode_cache: Dict[int, str] = {}
        self._dim_cache: Dict[int, int] = {}

    def _ns_id(self, raw_id: str) -> str:
        clean_id = raw_id.strip()
        prefix = f"{self.namespace}:"
        return clean_id if clean_id.startswith(prefix) else f"{prefix}{clean_id}"

    def _strip_ns(self, stored_id: str) -> str:
        prefix = f"{self.namespace}:"
        return stored_id[len(prefix) :] if stored_id.startswith(prefix) else stored_id

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
        top_k: int = 5,
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
    # 1. OPERASI TERAS GENERIK (UNTUK MANA-MANA PROJEK & JENIS KANDUNGAN)
    # --------------------------------------------------------------------------
    def upsert_semantic_item(
        self,
        item_id: str,
        routing_key: str,
        semantic_text: str,
        metadata: Optional[Dict[str, Any]] = None,
        record_type: str = "story",
    ) -> Tuple[bool, int]:
        """
        Menyimpan sebarang rekod vektor ke indeks sasaran mengikut `routing_key`.
        Memulangkan: (berjaya, index_vector_digunakan)
        """
        shard = ledger_db.select_balanced_vector_shard(f"{self.namespace}:{routing_key.strip().lower()}")
        shard_idx = int(shard["index"])
        namespaced_id = self._ns_id(item_id)
        now_ts = int(time.time())

        full_meta: Dict[str, Any] = {
            "namespace": self.namespace,
            "record_type": record_type,
            "item_id": self._strip_ns(namespaced_id),
            "post_uid": self._strip_ns(namespaced_id),
            "routing_key": routing_key.strip(),
            "snippet": semantic_text.strip()[:240],
            "vector_shard_index": shard_idx,
            "created_at": now_ts,
            "posted_epoch": now_ts,
        }
        if metadata:
            full_meta.update(metadata)
            # Pastikan kedua-dua medan cap masa selari jika salah satu dibekalkan
            if "posted_epoch" in metadata and "created_at" not in metadata:
                full_meta["created_at"] = int(metadata["posted_epoch"])
            elif "created_at" in metadata and "posted_epoch" not in metadata:
                full_meta["posted_epoch"] = int(metadata["created_at"])

        try:
            self._upsert_adaptive(shard, namespaced_id, semantic_text.strip(), full_meta)
            ledger_db.record_account_activity("vector", shard_idx, ops_delta=1)
            return True, shard_idx
        except Exception as exc:
            console.print(f"[bold red]❌ Ralat upsert Upstash Vector #{shard_idx:03d}: {exc}[/bold red]")
            return False, shard_idx

    def search_semantic_items(
        self,
        routing_key: str,
        query_text: str,
        top_k: int = 5,
        record_type: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Mencari vektor paling hampir dalam indeks yang dipetakan oleh `routing_key`
        dan menapis mengikut `namespace` serta `record_type` (jika dinyatakan).
        """
        shard = ledger_db.select_balanced_vector_shard(f"{self.namespace}:{routing_key.strip().lower()}")
        shard_idx = int(shard["index"])

        try:
            raw_matches = self._query_adaptive(shard, query_text.strip(), top_k=max(top_k * 2, 6))
            ledger_db.record_account_activity("vector", shard_idx, ops_delta=1)

            cleaned_results: List[Dict[str, Any]] = []
            for m in raw_matches:
                score = float(getattr(m, "score", 0.0) or 0.0)
                meta = dict(getattr(m, "metadata", None) or {})
                raw_id = str(getattr(m, "id", "") or "")

                # Keserasian dengan rekod lama yang belum mempunyai medan namespace
                item_ns = str(meta.get("namespace") or "ps2n").lower()
                if item_ns != self.namespace:
                    continue

                if record_type:
                    item_rtype = str(meta.get("record_type") or "story").lower()
                    if item_rtype != record_type.lower():
                        continue

                cleaned_results.append(
                    {
                        "matched_id": self._strip_ns(raw_id),
                        "raw_vector_id": raw_id,
                        "score": round(score, 4),
                        "metadata": meta,
                    }
                )
                if len(cleaned_results) >= top_k:
                    break

            return cleaned_results, shard_idx
        except Exception as exc:
            console.print(f"[bold yellow]⚠️ Amaran carian Vector #{shard_idx:03d}: {exc}[/bold yellow]")
            return [], shard_idx

    def delete_semantic_item(self, item_id: str, routing_key: str) -> bool:
        """Memadam satu vektor daripada indeks sasaran."""
        shard = ledger_db.select_balanced_vector_shard(f"{self.namespace}:{routing_key.strip().lower()}")
        shard_idx = int(shard["index"])
        namespaced_id = self._ns_id(item_id)
        try:
            client = self._get_client(shard)
            client.delete(ids=[namespaced_id])
            ledger_db.record_account_activity("vector", shard_idx, ops_delta=1)
            return True
        except Exception:
            return False

    # --------------------------------------------------------------------------
    # 2. OPERASI CERITA AI & PENAPIS COOLDOWN BERTINGKAT (60 HARI / 5 HARI)
    # --------------------------------------------------------------------------
    def upsert_story_vector(
        self,
        post_uid: Optional[str] = None,
        game_title: str = "",
        story_text: str = "",
        extra_meta: Optional[Dict[str, Any]] = None,
        *,
        story_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, int]:
        """
        Menyimpan vektor cerita PS2 ke indeks sasaran mengikut hash tajuk game.
        Menerima kedua-dua gaya argumen (`post_uid`/`story_id` & `extra_meta`/`metadata`)
        supaya serasi 100% dengan semua skrip sedia ada.
        """
        uid = (post_uid or story_id or f"story_{int(time.time())}").strip()
        merged_meta: Dict[str, Any] = {"game_title": game_title.strip()}
        if metadata:
            merged_meta.update(metadata)
        if extra_meta:
            merged_meta.update(extra_meta)

        combined_text = f"{game_title.strip()} | {story_text.strip()}"
        return self.upsert_semantic_item(
            item_id=uid,
            routing_key=game_title or "ps2_general",
            semantic_text=combined_text,
            metadata=merged_meta,
            record_type="story",
        )

    def check_semantic_duplicate(
        self,
        game_title: str,
        candidate_story: str,
        similarity_threshold: float = 0.75,
        exact_threshold: float = 0.95,
        exact_cooldown_days: int = 60,
        similar_cooldown_days: int = 5,
        now_epoch: Optional[int] = None,
    ) -> Tuple[bool, Optional[Dict[str, Any]], int]:
        """
        Menyemak pendua semantik cerita dengan kawalan jarak masa bertingkat:
        1. Persamaan >= `exact_threshold` (lalai 95%) -> disekat selama `exact_cooldown_days` (60 hari).
        2. Persamaan >= `similarity_threshold` (lalai 75%) -> disekat selama `similar_cooldown_days` (5 hari).
        3. Jika rekod lama sudah melepasi tempoh jarak masa tersebut, ia DIBENARKAN semula.

        Memulangkan: (adalah_pendua_disekat, butiran_padanan, index_vector)
        """
        combined_text = f"{game_title.strip()} | {candidate_story.strip()}"
        matches, shard_idx = self.search_semantic_items(
            routing_key=game_title or "ps2_general",
            query_text=combined_text,
            top_k=5,
            record_type="story",
        )
        if not matches:
            return False, None, shard_idx

        current_ts = int(now_epoch or time.time())
        top_preview: Optional[Dict[str, Any]] = None

        for m in matches:
            score = float(m["score"])
            meta = m["metadata"]
            prev_ts = int(meta.get("posted_epoch") or meta.get("created_at") or current_ts)
            age_days = max(0.0, (current_ts - prev_ts) / 86400.0)

            info_obj = {
                "matched_id": m["matched_id"],
                "score": score,
                "age_days": round(age_days, 2),
                "game_title": meta.get("game_title", ""),
                "snippet": meta.get("snippet", ""),
                "post_uid": meta.get("post_uid", m["matched_id"]),
                "metadata": meta,
            }
            if top_preview is None:
                top_preview = info_obj

            if score >= exact_threshold and age_days < exact_cooldown_days:
                info_obj["block_reason"] = (
                    f"Ayat hampir sama ({score*100:.1f}%) dalam jarak {age_days:.1f} hari (had {exact_cooldown_days} hari)."
                )
                return True, info_obj, shard_idx

            if score >= similarity_threshold and age_days < similar_cooldown_days:
                info_obj["block_reason"] = (
                    f"Ayat serupa ({score*100:.1f}%) dalam jarak {age_days:.1f} hari (had {similar_cooldown_days} hari)."
                )
                return True, info_obj, shard_idx

        return False, top_preview, shard_idx

    def delete_story_vector(self, post_uid: str, game_title: str) -> bool:
        """Memadam rekod vektor cerita daripada indeks game berkaitan."""
        return self.delete_semantic_item(item_id=post_uid, routing_key=game_title or "ps2_general")

    # --------------------------------------------------------------------------
    # 3. OPERASI SEMANTIK MEDIA VISUAL (KATEGORI GAMBAR & VIDEO REELS)
    # --------------------------------------------------------------------------
    def upsert_media_vector(
        self,
        media_uid: str,
        category: str,
        visual_description: str,
        ocr_text: str = "",
        extra_meta: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, int]:
        """
        Menyimpan vektor semantik gambar (Unsplash/B2) atau video (Pexels Reels)
        mengikut kategori supaya sistem mudah memilih set gambar yang hampir sama.
        """
        clean_cat = category.strip().lower() or "cat_controller"
        combined_desc = f"{clean_cat} | {visual_description.strip()} | OCR: {ocr_text.strip() or 'None'}"
        meta: Dict[str, Any] = {
            "media_uid": media_uid,
            "category": clean_cat,
            "ocr_text": ocr_text.strip() or "None",
        }
        if extra_meta:
            meta.update(extra_meta)

        return self.upsert_semantic_item(
            item_id=f"media_{media_uid}",
            routing_key=f"media:{clean_cat}",
            semantic_text=combined_desc,
            metadata=meta,
            record_type="media",
        )

    def find_similar_media(
        self,
        category: str,
        query_description: str,
        top_k: int = 5,
        min_score: float = 0.55,
        exclude_media_uids: Optional[List[str]] = None,
    ) -> Tuple[List[Dict[str, Any]], int]:
        """
        Mencari aset media (gambar/video) yang mempunyai gaya visual dan objek
        paling hampir sama dalam kategori yang dipilih.
        """
        clean_cat = category.strip().lower() or "cat_controller"
        excluded = set(exclude_media_uids or [])
        matches, shard_idx = self.search_semantic_items(
            routing_key=f"media:{clean_cat}",
            query_text=f"{clean_cat} | {query_description.strip()}",
            top_k=max(top_k * 2, 8),
            record_type="media",
        )

        selected: List[Dict[str, Any]] = []
        for m in matches:
            if m["score"] < min_score:
                continue
            meta = m["metadata"]
            m_uid = str(meta.get("media_uid") or m["matched_id"].replace("media_", ""))
            if m_uid in excluded:
                continue
            selected.append(
                {
                    "media_uid": m_uid,
                    "score": m["score"],
                    "category": meta.get("category", clean_cat),
                    "ocr_text": meta.get("ocr_text", "None"),
                    "snippet": meta.get("snippet", ""),
                    "metadata": meta,
                }
            )
            if len(selected) >= top_k:
                break

        return selected, shard_idx

    # --------------------------------------------------------------------------
    # 4. SEMAKAN KESIHATAN KESEMUA 5 INDEKS UPSTASH VECTOR
    # --------------------------------------------------------------------------
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


# Singleton Instance utama untuk diimport oleh semua modul projek
vector_sharded = PS2VectorShardedManager()


# ==============================================================================
# DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ==============================================================================
if __name__ == "__main__":
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: UPSTASH VECTOR 5-INDEX SMART SEMANTIC TEST (X05)[/bold cyan]",
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

    # Ujian 1: Upsert guna parameter story_id & metadata (keserasian X11)
    up_ok, used_idx = vector_sharded.upsert_story_vector(
        story_id=sample_uid,
        game_title=sample_game,
        story_text=original_story,
    )

    time.sleep(1.0)

    # Ujian 2: Semakan sekatan aktif (umur 0 hari < 5 hari -> disekat)
    dup_blocked, dup_info, _ = vector_sharded.check_semantic_duplicate(
        game_title=sample_game,
        candidate_story=similar_story,
        similarity_threshold=0.75,
    )

    # Ujian 3: Simulasi masa hadapan (+10 hari -> lulus had 5 hari untuk persamaan 75%-85%)
    future_epoch = int(time.time()) + (10 * 86400)
    dup_after_10d, _, _ = vector_sharded.check_semantic_duplicate(
        game_title=sample_game,
        candidate_story=similar_story,
        similarity_threshold=0.75,
        now_epoch=future_epoch,
    )

    # Ujian 4: Pengkelasan kategori media & carian visual
    auto_cat, cat_score = classify_media_category(
        "Black PlayStation 2 DualShock controller with 8MB memory card on desk",
        ocr_text="SONY",
    )
    media_uid = f"m_probe_{int(time.time())}"
    m_up_ok, m_idx = vector_sharded.upsert_media_vector(
        media_uid=media_uid,
        category=auto_cat,
        visual_description="Black PlayStation 2 DualShock controller close up",
        ocr_text="SONY",
    )
    time.sleep(0.8)
    m_matches, _ = vector_sharded.find_similar_media(
        category=auto_cat,
        query_description="Sony PlayStation DualShock 2 controller",
        top_k=2,
    )

    # Pembersihan rekod ujian
    clean_story_ok = vector_sharded.delete_story_vector(post_uid=sample_uid, game_title=sample_game)
    clean_media_ok = vector_sharded.delete_semantic_item(f"media_{media_uid}", f"media:{auto_cat}")

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
    console.print(
        Panel(
            f"• Upsert Vektor Cerita (Indeks #{used_idx:03d}): [bold green]{'BERJAYA' if up_ok else 'GAGAL'}[/bold green]\n"
            f"• Semakan Cooldown Hari ke-0 (Skor {sim_score}): [bold green]{'DISEKAT (<5 HARI)' if dup_blocked else 'TERLEPAS'}[/bold green]\n"
            f"• Simulasi Cooldown Hari ke-10 (Skor {sim_score}): [bold green]{'DIBENARKAN (>5 HARI)' if not dup_after_10d else 'MASIH DISEKAT'}[/bold green]\n"
            f"• Pengkelasan Media Otomatik: [bold cyan]{auto_cat}[/bold cyan] (Skor {cat_score} | Indeks #{m_idx:03d} | Padanan: {len(m_matches)})\n"
            f"• Pembersihan Vektor Ujian: [bold green]{'BERSIH' if (clean_story_ok and clean_media_ok and m_up_ok) else 'RALAT'}[/bold green]",
            title="Ringkasan Ujian Pintar X05",
            border_style="green",
        )
    )