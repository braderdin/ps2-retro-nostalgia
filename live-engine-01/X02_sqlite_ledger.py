#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - BUKU LEJAR SQLITE3 & AUTO-SPLIT MANAGER (X02)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X02_sqlite_ledger.py
#
# FUNGSI:
# 1. Pengurusan Pangkalan Data SQLite3 di /home/braderdin/ps2-retro-nostalgia/data/
#    dengan Auto-Split automatik pada 30 MB (lingkungan 25MB - 35MB) mengikut
#    penomboran fail: ps2n_ledger_001.db, ps2n_ledger_002.db, ...
# 2. Utiliti pemecahan fail JSON (25MB - 35MB) untuk data kekal (data/) & data
#    sementara (temp/).
# 3. Formula Pengagihan Sekata (Balanced Load Router) untuk 5 Akaun B2,
#    5 Shard Upstash Redis, dan 5 Indeks Upstash Vector.
# 4. Pengurusan inventori aset media, sejarah siaran FB, dan pautan Shopee.
# ==============================================================================

import json
import sqlite3
import sys
import time
import zlib
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import X01_config as cfg

MYT = timezone(timedelta(hours=8))

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS media_assets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_uid TEXT UNIQUE NOT NULL,
    game_title TEXT NOT NULL,
    media_type TEXT NOT NULL,              -- 'image', 'video', 'reel'
    source_provider TEXT NOT NULL,         -- 'local_gameplay', 'unsplash', 'pexels'
    b2_account_index INTEGER NOT NULL,
    b2_bucket TEXT NOT NULL,
    b2_file_path TEXT NOT NULL,
    proxy_url TEXT NOT NULL,
    size_bytes INTEGER DEFAULT 0,
    sha256 TEXT DEFAULT '',
    status TEXT DEFAULT 'ready',           -- 'ready', 'published', 'deleted'
    created_at INTEGER NOT NULL,
    last_used_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_media_status ON media_assets(status);
CREATE INDEX IF NOT EXISTS idx_media_game ON media_assets(game_title);

CREATE TABLE IF NOT EXISTS published_posts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_uid TEXT UNIQUE NOT NULL,
    fb_post_id TEXT NOT NULL,
    fb_comment_id TEXT DEFAULT '',
    asset_uid TEXT DEFAULT '',
    game_title TEXT NOT NULL,
    post_type TEXT NOT NULL,               -- 'photo', 'reel', 'video'
    story_caption TEXT NOT NULL,
    shopee_product_type TEXT NOT NULL,     -- 'memory_card', 'pendrive', 'store'
    shopee_url TEXT NOT NULL,
    redis_shard_index INTEGER NOT NULL,
    vector_shard_index INTEGER NOT NULL,
    published_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_posts_game ON published_posts(game_title);

CREATE TABLE IF NOT EXISTS account_usage_daily (
    service_type TEXT NOT NULL,            -- 'b2', 'redis', 'vector'
    account_index INTEGER NOT NULL,
    date_myt TEXT NOT NULL,
    ops_today INTEGER DEFAULT 0,
    bytes_used INTEGER DEFAULT 0,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (service_type, account_index, date_myt)
);

CREATE TABLE IF NOT EXISTS shopee_catalog (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    product_code TEXT UNIQUE NOT NULL,     -- 'memory_card', 'pendrive', 'store'
    product_name TEXT NOT NULL,
    keywords TEXT NOT NULL,
    shopee_url TEXT NOT NULL,
    cta_template TEXT NOT NULL,
    active INTEGER DEFAULT 1
);
"""


def now_myt_str() -> str:
    return datetime.now(tz=MYT).strftime("%Y-%m-%d")


def format_bytes(num_bytes: int) -> str:
    if num_bytes < 1024:
        return f"{num_bytes} B"
    if num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.2f} KB"
    if num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    return f"{num_bytes / (1024 * 1024 * 1024):.3f} GB"


# ==============================================================================
# UTILITI AUTO-SPLIT JSON (25 MB - 35 MB) UNTUK DATA/ DAN TEMP/
# ==============================================================================
def save_json_with_split(
    records: List[Any],
    base_prefix: str,
    target_dir: Path = cfg.DATA_DIR,
    split_bytes: int = cfg.SPLIT_TARGET_BYTES,
) -> List[Path]:
    """
    Menyimpan senarai rekod ke dalam fail JSON bernombor (cth: feed_001.json, feed_002.json).
    Jika saiz mencecah split_bytes (lalai 30 MB, dalam julat 25MB-35MB), fail dipecahkan
    secara automatik kepada bahagian bernombor seterusnya.
    """
    target_dir.mkdir(parents=True, exist_ok=True)
    # Clamp had supaya sentiasa berada dalam julat 25MB - 35MB
    effective_limit = max(cfg.SPLIT_MIN_BYTES, min(cfg.SPLIT_MAX_BYTES, split_bytes))

    written_files: List[Path] = []
    part_num = 1
    current_batch: List[Any] = []
    current_bytes = 2  # kurungan '[]'

    for item in records:
        item_str = json.dumps(item, ensure_ascii=False, separators=(",", ":"))
        item_len = len(item_str.encode("utf-8")) + 1

        if current_batch and (current_bytes + item_len) >= effective_limit:
            out_path = target_dir / f"{base_prefix}_{part_num:03d}.json"
            out_path.write_text(json.dumps(current_batch, ensure_ascii=False, indent=2), encoding="utf-8")
            written_files.append(out_path)
            part_num += 1
            current_batch = []
            current_bytes = 2

        current_batch.append(item)
        current_bytes += item_len

    if current_batch or not written_files:
        out_path = target_dir / f"{base_prefix}_{part_num:03d}.json"
        out_path.write_text(json.dumps(current_batch, ensure_ascii=False, indent=2), encoding="utf-8")
        written_files.append(out_path)

    return written_files


# ==============================================================================
# PENGURUS PANGKALAN DATA SQLITE3 DENGAN AUTO-ROTATION (30 MB / FAIL)
# ==============================================================================
class PS2SQLiteLedger:
    def __init__(
        self,
        data_dir: Path = cfg.DATA_DIR,
        db_prefix: str = "ps2n_ledger",
        split_threshold_bytes: int = cfg.SPLIT_TARGET_BYTES,
    ):
        self.data_dir = data_dir
        self.db_prefix = db_prefix
        self.split_threshold_bytes = max(
            cfg.SPLIT_MIN_BYTES,
            min(cfg.SPLIT_MAX_BYTES, split_threshold_bytes),
        )
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.active_db_path = self._resolve_or_rotate_active_db()
        self._init_schema(self.active_db_path)
        self._seed_default_shopee_catalog()

    def list_db_shards(self) -> List[Path]:
        """Menyenaraikan semua fail .db bernombor mengikut urutan."""
        shards = sorted(self.data_dir.glob(f"{self.db_prefix}_*.db"))
        return shards

    def _get_next_shard_path(self, current_shards: List[Path]) -> Path:
        if not current_shards:
            return self.data_dir / f"{self.db_prefix}_001.db"
        max_idx = 0
        for p in current_shards:
            stem = p.stem  # cth: ps2n_ledger_001
            parts = stem.rsplit("_", 1)
            if len(parts) == 2 and parts[1].isdigit():
                max_idx = max(max_idx, int(parts[1]))
        return self.data_dir / f"{self.db_prefix}_{max_idx + 1:03d}.db"

    def _resolve_or_rotate_active_db(self) -> Path:
        shards = self.list_db_shards()
        if not shards:
            return self.data_dir / f"{self.db_prefix}_001.db"

        latest = shards[-1]
        if latest.exists() and latest.stat().st_size >= self.split_threshold_bytes:
            next_db = self._get_next_shard_path(shards)
            console.print(
                f"[bold yellow]📦 Fail SQLite {latest.name} mencecah had ({format_bytes(latest.stat().st_size)}). "
                f"Membuka fail baharu: {next_db.name}[/bold yellow]"
            )
            return next_db
        return latest

    def _connect(self, db_path: Optional[Path] = None) -> sqlite3.Connection:
        target = db_path or self.active_db_path
        conn = sqlite3.connect(str(target), timeout=15.0)
        conn.row_factory = sqlite3.Row
        # TRUNCATE memastikan semua transaksi ditulis terus ke fail .db utama supaya saiz fail tepat
        conn.execute("PRAGMA journal_mode=TRUNCATE;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        return conn

    def _init_schema(self, db_path: Path) -> None:
        with self._connect(db_path) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()

    def ensure_write_capacity(self) -> Path:
        """Dipanggil sebelum operasi tulis untuk mengasingkan fail jika saiz >= 30 MB."""
        if self.active_db_path.exists() and self.active_db_path.stat().st_size >= self.split_threshold_bytes:
            self.active_db_path = self._resolve_or_rotate_active_db()
            self._init_schema(self.active_db_path)
            self._seed_default_shopee_catalog()
        return self.active_db_path

    def _seed_default_shopee_catalog(self) -> None:
        """Mengisi katalog asas produk Shopee jika jadual masih kosong."""
        defaults = [
            (
                "memory_card",
                "PS2 Memory Card 8MB / 64MB (Siap Save Data & FMCB)",
                "save,memory,kad memori,tammat,boss,rahsia,unlock,cheat,fmcb,opl",
                cfg.PS2N_SHOPEE_MC_URL,
                "Rindu nak sambung game PS2 tanpa risau save data hilang? Boleh tengok koleksi Memory Card PS2 kat sini:",
            ),
            (
                "pendrive",
                "PS2 Pendrive / Hard Disk Siap Isi Game Klasik (Plug & Play)",
                "disk calar,rosak,lens,loading,pendrive,usb,koleksi game,gameplay,nostalgia",
                cfg.PS2N_SHOPEE_PENDRIVE_URL,
                "Konsol PS2 lama masih ada tapi disk banyak calar? Boleh layan balik game klasik guna Pendrive siap isi game kat sini:",
            ),
            (
                "store",
                "Kedai Aksesori & Game Retro PS2 Shopee",
                "ps2,retro,console,aksesori,controller,kabel",
                cfg.PS2N_SHOPEE_STORE_URL,
                "Nak hidupkan semula konsol PS2 lama kat rumah? Boleh tengok kelengkapan PS2 kat sini:",
            ),
        ]
        with self._connect(self.active_db_path) as conn:
            for row in defaults:
                conn.execute(
                    """
                    INSERT OR IGNORE INTO shopee_catalog
                    (product_code, product_name, keywords, shopee_url, cta_template, active)
                    VALUES (?, ?, ?, ?, ?, 1)
                    """,
                    row,
                )
            conn.commit()

    # --------------------------------------------------------------------------
    # FORMULA PENGAGIHAN SEKATA (5 B2, 5 REDIS, 5 VECTOR)
    # --------------------------------------------------------------------------
    def record_account_activity(
        self,
        service_type: str,
        account_index: int,
        ops_delta: int = 1,
        bytes_used: Optional[int] = None,
    ) -> None:
        self.ensure_write_capacity()
        today = now_myt_str()
        now_ts = int(time.time())
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT ops_today, bytes_used FROM account_usage_daily
                WHERE service_type = ? AND account_index = ? AND date_myt = ?
                """,
                (service_type, account_index, today),
            ).fetchone()

            if row:
                new_ops = int(row["ops_today"]) + ops_delta
                new_bytes = int(bytes_used) if bytes_used is not None else int(row["bytes_used"])
                conn.execute(
                    """
                    UPDATE account_usage_daily
                    SET ops_today = ?, bytes_used = ?, updated_at = ?
                    WHERE service_type = ? AND account_index = ? AND date_myt = ?
                    """,
                    (new_ops, new_bytes, now_ts, service_type, account_index, today),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO account_usage_daily
                    (service_type, account_index, date_myt, ops_today, bytes_used, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (service_type, account_index, today, ops_delta, int(bytes_used or 0), now_ts),
                )
            conn.commit()

    def get_today_usage_map(self, service_type: str) -> Dict[int, Dict[str, int]]:
        today = now_myt_str()
        usage: Dict[int, Dict[str, int]] = {}
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT account_index, ops_today, bytes_used FROM account_usage_daily
                WHERE service_type = ? AND date_myt = ?
                """,
                (service_type, today),
            ).fetchall()
            for r in rows:
                usage[int(r["account_index"])] = {
                    "ops_today": int(r["ops_today"]),
                    "bytes_used": int(r["bytes_used"]),
                }
        return usage

    def get_total_b2_bytes_by_account(self) -> Dict[int, int]:
        """Mengira jumlah bait aktif merentasi semua fail .db untuk setiap akaun B2."""
        totals: Dict[int, int] = {acc["index"]: 0 for acc in cfg.B2_ACCOUNTS}
        for db_file in self.list_db_shards():
            with self._connect(db_file) as conn:
                rows = conn.execute(
                    """
                    SELECT b2_account_index, COALESCE(SUM(size_bytes), 0) AS total_b
                    FROM media_assets
                    WHERE status != 'deleted'
                    GROUP BY b2_account_index
                    """
                ).fetchall()
                for r in rows:
                    idx = int(r["b2_account_index"])
                    totals[idx] = totals.get(idx, 0) + int(r["total_b"])
        return totals

    def select_balanced_b2_account(self, required_bytes: int = 0) -> Optional[Dict[str, Any]]:
        """
        Formula Agihan Sekata Backblaze B2:
        Skor = 0.7 * (bait_digunakan / 9.0 GB) + 0.3 * (transaksi_hari_ini / had_harian)
        Memilih akaun dengan skor terendah yang masih mempunyai ruang selamat.
        """
        if not cfg.B2_ACCOUNTS:
            return None

        used_bytes_map = self.get_total_b2_bytes_by_account()
        daily_usage = self.get_today_usage_map("b2")

        candidates: List[Tuple[float, Dict[str, Any]]] = []
        for acc in cfg.B2_ACCOUNTS:
            idx = acc["index"]
            used_b = used_bytes_map.get(idx, 0)
            if (used_b + required_bytes) > cfg.B2_SAFE_BYTES_PER_ACCOUNT:
                continue

            ops_today = daily_usage.get(idx, {}).get("ops_today", 0)
            storage_ratio = used_b / max(1, cfg.B2_SAFE_BYTES_PER_ACCOUNT)
            ops_ratio = ops_today / max(1, cfg.B2_SAFE_DAILY_OPS_PER_ACCOUNT)
            score = (0.70 * storage_ratio) + (0.30 * ops_ratio)
            candidates.append((score, acc))

        if not candidates:
            return None
        candidates.sort(key=lambda pair: (pair[0], pair[1]["index"]))
        return candidates[0][1]

    def select_balanced_redis_shard(self, key_identifier: str) -> Dict[str, Any]:
        """
        Agihan Penentu CRC32 Modulo merentasi 5 Shard Upstash Redis.
        Jika shard utama hampir mencapai had harian selamat, alih ke shard paling rendah beban.
        """
        if not cfg.REDIS_ACCOUNTS:
            raise RuntimeError("Tiada akaun Upstash Redis dikonfigurasi.")

        total = len(cfg.REDIS_ACCOUNTS)
        checksum = zlib.crc32(key_identifier.encode("utf-8")) & 0xFFFFFFFF
        primary = cfg.REDIS_ACCOUNTS[checksum % total]

        daily_usage = self.get_today_usage_map("redis")
        primary_ops = daily_usage.get(primary["index"], {}).get("ops_today", 0)
        if primary_ops < cfg.REDIS_SAFE_DAILY_CMDS_PER_ACCOUNT:
            return primary

        # Sandaran jika shard utama melebihi ambang harian selamat
        return min(
            cfg.REDIS_ACCOUNTS,
            key=lambda a: daily_usage.get(a["index"], {}).get("ops_today", 0),
        )

    def select_balanced_vector_shard(self, game_title: str) -> Dict[str, Any]:
        """
        Agihan Semantik Mengikut Tajuk Game (CRC32 Modulo 5) supaya carian pendua
        hanya memanggil 1 indeks Vector sahaja dan tidak membakar 5x kuota carian.
        """
        if not cfg.VECTOR_ACCOUNTS:
            raise RuntimeError("Tiada akaun Upstash Vector dikonfigurasi.")

        normalized_game = " ".join(game_title.lower().strip().split())
        total = len(cfg.VECTOR_ACCOUNTS)
        checksum = zlib.crc32(normalized_game.encode("utf-8")) & 0xFFFFFFFF
        primary = cfg.VECTOR_ACCOUNTS[checksum % total]

        daily_usage = self.get_today_usage_map("vector")
        primary_ops = daily_usage.get(primary["index"], {}).get("ops_today", 0)
        if primary_ops < cfg.VECTOR_SAFE_DAILY_QUERIES_PER_ACCOUNT:
            return primary

        return min(
            cfg.VECTOR_ACCOUNTS,
            key=lambda a: daily_usage.get(a["index"], {}).get("ops_today", 0),
        )

    # --------------------------------------------------------------------------
    # REKOD ASET MEDIA, HANTARAN & PILIHAN PAUTAN SHOPEE
    # --------------------------------------------------------------------------
    def register_media_asset(self, asset: Dict[str, Any]) -> None:
        self.ensure_write_capacity()
        now_ts = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO media_assets (
                    asset_uid, game_title, media_type, source_provider,
                    b2_account_index, b2_bucket, b2_file_path, proxy_url,
                    size_bytes, sha256, status, created_at, last_used_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    asset["asset_uid"],
                    asset["game_title"],
                    asset["media_type"],
                    asset["source_provider"],
                    int(asset["b2_account_index"]),
                    asset["b2_bucket"],
                    asset["b2_file_path"],
                    asset["proxy_url"],
                    int(asset.get("size_bytes", 0)),
                    asset.get("sha256", ""),
                    asset.get("status", "ready"),
                    int(asset.get("created_at", now_ts)),
                    now_ts,
                ),
            )
            conn.commit()

    def record_published_post(self, post: Dict[str, Any]) -> None:
        self.ensure_write_capacity()
        now_ts = int(time.time())
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO published_posts (
                    post_uid, fb_post_id, fb_comment_id, asset_uid, game_title,
                    post_type, story_caption, shopee_product_type, shopee_url,
                    redis_shard_index, vector_shard_index, published_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    post["post_uid"],
                    post["fb_post_id"],
                    post.get("fb_comment_id", ""),
                    post.get("asset_uid", ""),
                    post["game_title"],
                    post["post_type"],
                    post["story_caption"],
                    post.get("shopee_product_type", "pendrive"),
                    post["shopee_url"],
                    int(post.get("redis_shard_index", 1)),
                    int(post.get("vector_shard_index", 1)),
                    int(post.get("published_at", now_ts)),
                ),
            )
            if post.get("asset_uid"):
                conn.execute(
                    "UPDATE media_assets SET status = 'published', last_used_at = ? WHERE asset_uid = ?",
                    (now_ts, post["asset_uid"]),
                )
            conn.commit()

    def pick_shopee_comment_offer(self, story_text: str) -> Dict[str, str]:
        """Memilih pautan produk Shopee paling relevan berdasarkan konteks cerita."""
        lower_story = story_text.lower()
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT product_code, product_name, keywords, shopee_url, cta_template FROM shopee_catalog WHERE active = 1"
            ).fetchall()

        best_row = None
        best_hits = 0
        for r in rows:
            kws = [k.strip().lower() for k in str(r["keywords"]).split(",") if k.strip()]
            hits = sum(1 for k in kws if k in lower_story)
            if hits > best_hits:
                best_hits = hits
                best_row = r

        if not best_row and rows:
            # Lalai bergilir antara pendrive dan memory_card
            preferred = "memory_card" if (int(time.time()) % 2 == 0) else "pendrive"
            best_row = next((r for r in rows if r["product_code"] == preferred), rows[0])

        if best_row:
            return {
                "product_code": str(best_row["product_code"]),
                "product_name": str(best_row["product_name"]),
                "shopee_url": str(best_row["shopee_url"]),
                "cta_template": str(best_row["cta_template"]),
            }

        return {
            "product_code": "store",
            "product_name": "Kedai Retro PS2 Shopee",
            "shopee_url": cfg.PS2N_SHOPEE_STORE_URL,
            "cta_template": "Rindu nak layan balik game klasik PS2? Boleh tengok koleksi kat sini:",
        }

    def get_ledger_summary(self) -> Dict[str, Any]:
        shards = self.list_db_shards()
        total_assets = 0
        total_posts = 0
        total_db_bytes = 0

        for s in shards:
            if s.exists():
                total_db_bytes += s.stat().st_size
            with self._connect(s) as conn:
                a_cnt = conn.execute("SELECT COUNT(*) FROM media_assets").fetchone()[0]
                p_cnt = conn.execute("SELECT COUNT(*) FROM published_posts").fetchone()[0]
                total_assets += int(a_cnt)
                total_posts += int(p_cnt)

        return {
            "active_db": self.active_db_path.name,
            "total_shards": len(shards),
            "total_db_bytes": total_db_bytes,
            "split_threshold_bytes": self.split_threshold_bytes,
            "total_assets": total_assets,
            "total_posts": total_posts,
        }


# Singleton Instance untuk diimport terus oleh modul X03..X09
ledger_db = PS2SQLiteLedger()


# ==============================================================================
# DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ==============================================================================
if __name__ == "__main__":
    summary = ledger_db.get_ledger_summary()
    chosen_b2 = ledger_db.select_balanced_b2_account(required_bytes=15 * 1024 * 1024)
    chosen_redis = ledger_db.select_balanced_redis_shard("gta-san-andreas-ps2")
    chosen_vector = ledger_db.select_balanced_vector_shard("GTA San Andreas")
    sample_offer = ledger_db.pick_shopee_comment_offer(
        "Dulu main GTA San Andreas takut memory card penuh sebab nak simpan save data misi akhir."
    )

    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: SQLITE3 LEDGER & BALANCED ROUTER (X02)[/bold cyan]\n"
            f"[dim]Fail Aktif: {summary['active_db']} ({format_bytes(summary['total_db_bytes'])}) | "
            f"Had Auto-Split: {format_bytes(summary['split_threshold_bytes'])}[/dim]",
            border_style="cyan",
        )
    )

    tbl = Table(box=box.ROUNDED, header_style="bold green")
    tbl.add_column("Ujian Fungsi Lejar & Routing", style="bold white")
    tbl.add_column("Keputusan", style="cyan")

    tbl.add_row(
        "Pangkalan Data SQLite3",
        f"{summary['total_shards']} fail .db | {summary['total_assets']} aset | {summary['total_posts']} hantaran",
    )
    tbl.add_row(
        "Pilihan B2 Sekata (Skor Terendah)",
        f"#{chosen_b2['index']:03d} ({chosen_b2['bucket_name']})" if chosen_b2 else "Tiada akaun",
    )
    tbl.add_row(
        "Pilihan Redis Shard (CRC32)",
        f"Shard #{chosen_redis['index']:03d} ({chosen_redis['url'].replace('https://', '')})",
    )
    tbl.add_row(
        "Pilihan Vector Indeks (Game Hash)",
        f"Indeks #{chosen_vector['index']:03d} ({chosen_vector['url'].replace('https://', '')})",
    )
    tbl.add_row(
        "Padanan Komen Shopee Automatik",
        f"[{sample_offer['product_code']}] {sample_offer['cta_template']} -> {sample_offer['shopee_url']}",
    )

    console.print(tbl)