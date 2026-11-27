#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN METADATA & MEMORI UPSTASH REDIS 5-SHARD (X04)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X04_redis_sharded.py
#
# FUNGSI UTAMA (SERASI UNTUK SEMUA PERSONA & PROJEK MASA HADAPAN):
# 1. Pengagihan beban 5 shard Upstash Redis secara sekata (CRC32 + had kuota X02)
#    melalui titik hujung REST `/pipeline` untuk menjimatkan panggilan HTTP.
# 2. Penapis cap jari anti-duplikasi (`mark_dedup_fingerprint` & `is_duplicate_fingerprint`)
#    dengan sokongan `post_uid` pilihan (serasi panggilan 1 atau 2 argumen).
# 3. Enjin Memori Berputar 10-20 Post Terakhir (`push_rolling_history` &
#    `get_rolling_history`) merentas aliran ('global', 'text', 'photo', 'reels')
#    supaya setiap Persona AI sentiasa tahu 10 post terakhir sebelum menulis.
# 4. Pengawal Kuota Gelongsor (`check_sliding_quota` & `record_quota_event`)
#    untuk mengehadkan kekerapan siaran (contoh: 2 video Reels setiap 48 jam).
# 5. Penggiliran Adil (`pick_least_used_item` & `increment_rotation_score`)
#    untuk menggilirkan 10 link produk Shopee, variasi komen, atau kategori media.
# 6. Kunci Pelaksanaan (`acquire_lock` & `release_lock`) supaya larian cron dan
#    manual tidak bertindih menerbitkan post pada saat yang sama.
# ==============================================================================

import json
import random
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

console = Console()

CURRENT_DIR = Path(__file__).resolve().parent
if str(CURRENT_DIR) not in sys.path:
    sys.path.insert(0, str(CURRENT_DIR))

import X01_config as cfg
from X02_sqlite_ledger import ledger_db


class PS2RedisShardedManager:
    """
    Pengurus Berpusat Upstash Redis 5-Shard (Multi-Namespace & Pipeline-First).
    Boleh diimport oleh mana-mana modul persona atau projek lain melalui:
        from X04_redis_sharded import redis_sharded
    atau dengan namespace khusus untuk projek baharu:
        custom_redis = PS2RedisShardedManager(namespace="projek_baru")
    """

    def __init__(
        self,
        accounts: List[Dict[str, Any]] = cfg.REDIS_ACCOUNTS,
        namespace: str = "ps2n",
        timeout: float = 10.0,
    ) -> None:
        self.accounts = accounts
        self.namespace = re.sub(r"[^a-zA-Z0-9_-]", "", namespace).lower() or "ps2n"
        self.timeout = timeout

    def _k(self, suffix: str) -> str:
        """Membina kunci Redis berawalan namespace projek."""
        clean_suffix = suffix.lstrip(":")
        return f"{self.namespace}:{clean_suffix}"

    def _get_shard_by_index(self, shard_index: int) -> Optional[Dict[str, Any]]:
        for acc in self.accounts:
            if int(acc["index"]) == int(shard_index):
                return acc
        return None

    # --------------------------------------------------------------------------
    # TERAS KOMUNIKASI REST & PIPELINE UPSTASH REDIS
    # --------------------------------------------------------------------------
    def execute_command(
        self,
        shard: Dict[str, Any],
        command: str,
        *args: Any,
        track_ops: bool = True,
    ) -> Tuple[bool, Any]:
        """Melaksanakan satu arahan Upstash Redis REST API."""
        url = shard["url"].rstrip("/")
        headers = {
            "Authorization": f"Bearer {shard['token']}",
            "Content-Type": "application/json",
        }
        payload = [command] + [str(a) for a in args]

        try:
            resp = requests.post(url, headers=headers, json=payload, timeout=self.timeout)
            if resp.status_code == 200:
                if track_ops:
                    ledger_db.record_account_activity("redis", int(shard["index"]), ops_delta=1)
                return True, resp.json().get("result")
            return False, f"HTTP {resp.status_code}: {resp.text[:120]}"
        except Exception as exc:
            return False, str(exc)

    def execute_pipeline(
        self,
        shard: Dict[str, Any],
        commands: List[List[Any]],
        track_ops: bool = True,
    ) -> Tuple[bool, List[Any]]:
        """
        Melaksanakan beberapa arahan serentak melalui titik hujung /pipeline
        dalam satu panggilan rangkaian HTTP.
        """
        if not commands:
            return True, []

        url = f"{shard['url'].rstrip('/')}/pipeline"
        headers = {
            "Authorization": f"Bearer {shard['token']}",
            "Content-Type": "application/json",
        }
        serialized_cmds = [[str(part) for part in cmd] for cmd in commands]

        try:
            resp = requests.post(url, headers=headers, json=serialized_cmds, timeout=self.timeout)
            if resp.status_code != 200:
                return False, []

            raw_list = resp.json()
            if track_ops:
                ledger_db.record_account_activity(
                    "redis",
                    int(shard["index"]),
                    ops_delta=len(commands),
                )
            results = [item.get("result") if isinstance(item, dict) else None for item in raw_list]
            return True, results
        except Exception:
            return False, []

    # --------------------------------------------------------------------------
    # 1. PENGURUSAN METADATA HANTARAN & GARIS MASA
    # --------------------------------------------------------------------------
    def save_published_post(
        self,
        post_uid: str,
        post_meta: Dict[str, Any],
        dedup_fingerprint: Optional[str] = None,
        dedup_ttl_seconds: int = 90 * 86400,
    ) -> Tuple[bool, int]:
        """
        Menyimpan metadata hantaran siap ke shard pilihan secara berkelompok (pipeline)
        dan mendaftarkan kunci anti-duplikasi jika dibekalkan.
        Memulangkan: (berjaya, index_shard_digunakan)
        """
        shard = ledger_db.select_balanced_redis_shard(self._k(f"post:{post_uid}"))
        shard_idx = int(shard["index"])
        published_ts = int(post_meta.get("published_at") or post_meta.get("posted_epoch") or time.time())

        enriched_meta = dict(post_meta)
        enriched_meta["post_uid"] = post_uid
        enriched_meta["redis_shard_index"] = shard_idx
        enriched_meta["published_at"] = published_ts

        serialized_json = json.dumps(enriched_meta, ensure_ascii=False, separators=(",", ":"))

        pipeline_cmds: List[List[Any]] = [
            ["SET", self._k(f"post:{post_uid}"), serialized_json],
            ["ZADD", self._k("timeline"), published_ts, post_uid],
        ]

        ok, res_list = self.execute_pipeline(shard, pipeline_cmds, track_ops=True)
        if not ok or not res_list or res_list[0] != "OK":
            return False, shard_idx

        if dedup_fingerprint:
            self.mark_dedup_fingerprint(dedup_fingerprint, post_uid=post_uid, ttl_seconds=dedup_ttl_seconds)

        return True, shard_idx

    def get_published_post(self, post_uid: str) -> Optional[Dict[str, Any]]:
        """Membaca metadata hantaran daripada shard berkaitan."""
        shard = ledger_db.select_balanced_redis_shard(self._k(f"post:{post_uid}"))
        ok, raw = self.execute_command(shard, "GET", self._k(f"post:{post_uid}"), track_ops=True)
        if not ok or not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    def delete_published_post(self, post_uid: str, shard_index: Optional[int] = None) -> bool:
        """Memadam rekod hantaran dan membuangnya daripada garis masa shard."""
        shard = (
            self._get_shard_by_index(shard_index)
            if shard_index
            else ledger_db.select_balanced_redis_shard(self._k(f"post:{post_uid}"))
        )
        if not shard:
            return False

        cmds = [
            ["DEL", self._k(f"post:{post_uid}")],
            ["ZREM", self._k("timeline"), post_uid],
        ]
        ok, _ = self.execute_pipeline(shard, cmds, track_ops=True)
        return ok

    # --------------------------------------------------------------------------
    # 2. PENAPIS ANTI-DUPLIKASI (MEDIA HASH / TOPIK / AYAT)
    # --------------------------------------------------------------------------
    def mark_dedup_fingerprint(
        self,
        fingerprint: str,
        post_uid: Optional[str] = None,
        ttl_seconds: int = 90 * 86400,
    ) -> bool:
        """
        Merekodkan cap jari (hash ayat/media/produk) supaya tidak diulang dalam tempoh TTL.
        Parameter `post_uid` bersifat pilihan supaya serasi dengan panggilan 1 argumen.
        """
        clean_fp = fingerprint.strip().lower()
        if not clean_fp:
            return False
        owner_uid = (post_uid or f"fp_{int(time.time())}").strip()
        shard = ledger_db.select_balanced_redis_shard(self._k(f"dedup:{clean_fp}"))
        ok, res = self.execute_command(
            shard,
            "SET",
            self._k(f"dedup:{clean_fp}"),
            owner_uid,
            "EX",
            ttl_seconds,
            track_ops=True,
        )
        return ok and res == "OK"

    def is_duplicate_fingerprint(self, fingerprint: str) -> Tuple[bool, Optional[str]]:
        """Menyemak sama ada cincangan media atau topik sudah pernah diterbitkan."""
        clean_fp = fingerprint.strip().lower()
        if not clean_fp:
            return False, None
        shard = ledger_db.select_balanced_redis_shard(self._k(f"dedup:{clean_fp}"))
        ok, existing_post_uid = self.execute_command(
            shard,
            "GET",
            self._k(f"dedup:{clean_fp}"),
            track_ops=True,
        )
        if ok and existing_post_uid:
            return True, str(existing_post_uid)
        return False, None

    # --------------------------------------------------------------------------
    # 3. MEMORI BERPUTAR 10-20 POST TERAKHIR (UNTUK KONTEKS SEMUA PERSONA AI)
    # --------------------------------------------------------------------------
    def push_rolling_history(
        self,
        record: Dict[str, Any],
        stream: str = "global",
        max_items: int = 20,
        also_push_global: bool = True,
    ) -> bool:
        """
        Menyimpan ringkasan hantaran terbaharu ke senarai berputar (FIFO capped list)
        mengikut aliran (`global`, `text`, `photo`, `reels`) supaya mana-mana Persona AI
        dapat membaca 10 post terakhir sebelum menjana ayat baharu.
        """
        clean_stream = re.sub(r"[^a-zA-Z0-9_-]", "", stream).lower() or "global"
        enriched = dict(record)
        enriched.setdefault("posted_epoch", int(time.time()))
        enriched.setdefault("stream", clean_stream)
        payload_json = json.dumps(enriched, ensure_ascii=False, separators=(",", ":"))

        target_streams = [clean_stream]
        if also_push_global and clean_stream != "global":
            target_streams.append("global")

        all_ok = True
        for st in target_streams:
            list_key = self._k(f"rolling_history:{st}")
            shard = ledger_db.select_balanced_redis_shard(list_key)
            ok, _ = self.execute_pipeline(
                shard,
                [
                    ["LPUSH", list_key, payload_json],
                    ["LTRIM", list_key, 0, max(1, max_items - 1)],
                ],
                track_ops=True,
            )
            if not ok:
                all_ok = False
        return all_ok

    def get_rolling_history(
        self,
        stream: str = "global",
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """
        Mengambil sehingga `limit` (lalai 10) rekod hantaran terakhir daripada Redis
        untuk dibekalkan terus ke dalam prompt AI.
        """
        clean_stream = re.sub(r"[^a-zA-Z0-9_-]", "", stream).lower() or "global"
        list_key = self._k(f"rolling_history:{clean_stream}")
        shard = ledger_db.select_balanced_redis_shard(list_key)

        ok, raw_list = self.execute_command(
            shard,
            "LRANGE",
            list_key,
            0,
            max(0, limit - 1),
            track_ops=True,
        )
        if not ok or not isinstance(raw_list, list):
            return []

        items: List[Dict[str, Any]] = []
        for raw in raw_list:
            try:
                parsed = json.loads(raw) if isinstance(raw, str) else raw
                if isinstance(parsed, dict):
                    items.append(parsed)
            except Exception:
                continue
        return items

    # --------------------------------------------------------------------------
    # 4. PENGAWAL KUOTA GELONGSOR (SLIDING-WINDOW RATE LIMITER)
    # --------------------------------------------------------------------------
    def check_sliding_quota(
        self,
        quota_name: str,
        max_events: int,
        window_seconds: int,
    ) -> Tuple[bool, int, int]:
        """
        Menyemak sama ada kuota masa masih membenarkan tindakan baharu.
        Contoh: 2 video Reels setiap 2 hari (max_events=2, window_seconds=172800).
        Memulangkan: (dibenarkan, bilangan_digunakan_dalam_tetingkap, saat_tunggu_jika_penuh)
        """
        clean_q = re.sub(r"[^a-zA-Z0-9_-]", "", quota_name).lower() or "default"
        zset_key = self._k(f"quota:{clean_q}")
        shard = ledger_db.select_balanced_redis_shard(zset_key)

        now_ts = int(time.time())
        cutoff_ts = now_ts - max(1, window_seconds)

        ok, res = self.execute_pipeline(
            shard,
            [
                ["ZREMRANGEBYSCORE", zset_key, "-inf", cutoff_ts],
                ["ZCARD", zset_key],
                ["ZRANGE", zset_key, 0, 0, "WITHSCORES"],
            ],
            track_ops=True,
        )
        if not ok or len(res) < 3:
            # Fail-open jika rangkaian terganggu sementara
            return True, 0, 0

        current_count = int(res[1] or 0)
        if current_count < max_events:
            return True, current_count, 0

        oldest_pair = res[2]
        wait_sec = 0
        if isinstance(oldest_pair, list) and len(oldest_pair) >= 2:
            oldest_score = int(float(oldest_pair[1]))
            wait_sec = max(1, (oldest_score + window_seconds) - now_ts)

        return False, current_count, wait_sec

    def record_quota_event(
        self,
        quota_name: str,
        event_id: Optional[str] = None,
        window_seconds: int = 48 * 3600,
    ) -> bool:
        """Merekodkan satu penggunaan kuota baharu selepas hantaran berjaya diterbitkan."""
        clean_q = re.sub(r"[^a-zA-Z0-9_-]", "", quota_name).lower() or "default"
        zset_key = self._k(f"quota:{clean_q}")
        shard = ledger_db.select_balanced_redis_shard(zset_key)

        now_ts = int(time.time())
        member = (event_id or f"evt_{now_ts}_{random.randint(100, 999)}").strip()
        cutoff_ts = now_ts - max(1, window_seconds)

        ok, _ = self.execute_pipeline(
            shard,
            [
                ["ZREMRANGEBYSCORE", zset_key, "-inf", cutoff_ts],
                ["ZADD", zset_key, now_ts, member],
                ["EXPIRE", zset_key, max(60, window_seconds + 3600)],
            ],
            track_ops=True,
        )
        return ok

    # --------------------------------------------------------------------------
    # 5. PENGGILIRAN ADIL (LEAST-USED ROTATOR UNTUK LINK SHOPEE / TEMPLAT / KATEGORI)
    # --------------------------------------------------------------------------
    def pick_least_used_item(
        self,
        pool_name: str,
        candidate_ids: List[str],
        exclude_ids: Optional[List[str]] = None,
    ) -> Optional[str]:
        """
        Memilih item yang paling kurang digunakan daripada senarai `candidate_ids`
        berdasarkan skor dalam Redis Sorted Set (sangat sesuai untuk mengimbangi
        10 pautan Shopee dan variasi komen merentas larian GitHub Actions).
        """
        if not candidate_ids:
            return None

        excluded = set(exclude_ids or [])
        filtered = [c for c in candidate_ids if c not in excluded] or list(candidate_ids)

        clean_pool = re.sub(r"[^a-zA-Z0-9_-]", "", pool_name).lower() or "default"
        zset_key = self._k(f"rotate:{clean_pool}")
        shard = ledger_db.select_balanced_redis_shard(zset_key)

        # Pastikan semua calon wujud dalam ZSET dengan ZADD NX 0
        init_cmds: List[List[Any]] = [["ZADD", zset_key, "NX", 0, cid] for cid in filtered]
        init_cmds.append(["ZRANGE", zset_key, 0, -1, "WITHSCORES"])

        ok, res = self.execute_pipeline(shard, init_cmds, track_ops=True)
        if not ok or not res:
            return random.choice(filtered)

        raw_scores = res[-1]
        score_map: Dict[str, float] = {cid: 0.0 for cid in filtered}
        if isinstance(raw_scores, list):
            for i in range(0, len(raw_scores) - 1, 2):
                member = str(raw_scores[i])
                if member in score_map:
                    score_map[member] = float(raw_scores[i + 1])

        min_score = min(score_map.values())
        lowest_group = [cid for cid, sc in score_map.items() if sc <= min_score]
        return random.choice(lowest_group)

    def increment_rotation_score(
        self,
        pool_name: str,
        item_id: str,
        delta: float = 1.0,
    ) -> bool:
        """Menambah skor penggunaan item selepas dipilih dan diterbitkan."""
        clean_pool = re.sub(r"[^a-zA-Z0-9_-]", "", pool_name).lower() or "default"
        zset_key = self._k(f"rotate:{clean_pool}")
        shard = ledger_db.select_balanced_redis_shard(zset_key)
        ok, _ = self.execute_command(shard, "ZINCRBY", zset_key, delta, item_id, track_ops=True)
        return ok

    # --------------------------------------------------------------------------
    # 6. KUNCI PENYELARASAN (DISTRIBUTED LOCK) & CACHE JSON AM
    # --------------------------------------------------------------------------
    def acquire_lock(self, lock_name: str, ttl_seconds: int = 300) -> Optional[str]:
        """Mengunci tugas supaya dua workflow tidak berjalan serentak."""
        clean_lock = re.sub(r"[^a-zA-Z0-9_-]", "", lock_name).lower() or "default"
        lock_key = self._k(f"lock:{clean_lock}")
        shard = ledger_db.select_balanced_redis_shard(lock_key)
        token = f"lk_{int(time.time())}_{random.randint(1000, 9999)}"

        ok, res = self.execute_command(
            shard,
            "SET",
            lock_key,
            token,
            "NX",
            "EX",
            max(5, ttl_seconds),
            track_ops=True,
        )
        return token if (ok and res == "OK") else None

    def release_lock(self, lock_name: str, token: str) -> bool:
        """Melepaskan kunci tugas jika token sepadan."""
        clean_lock = re.sub(r"[^a-zA-Z0-9_-]", "", lock_name).lower() or "default"
        lock_key = self._k(f"lock:{clean_lock}")
        shard = ledger_db.select_balanced_redis_shard(lock_key)
        ok_get, current = self.execute_command(shard, "GET", lock_key, track_ops=True)
        if ok_get and str(current or "") == token:
            ok_del, _ = self.execute_command(shard, "DEL", lock_key, track_ops=True)
            return ok_del
        return False

    def set_json(self, key_name: str, payload: Any, ttl_seconds: Optional[int] = None) -> bool:
        """Menyimpan sebarang struktur data JSON ke shard seimbang."""
        full_key = self._k(f"kv:{key_name}")
        shard = ledger_db.select_balanced_redis_shard(full_key)
        raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if ttl_seconds and ttl_seconds > 0:
            ok, res = self.execute_command(shard, "SET", full_key, raw, "EX", ttl_seconds, track_ops=True)
        else:
            ok, res = self.execute_command(shard, "SET", full_key, raw, track_ops=True)
        return ok and res == "OK"

    def get_json(self, key_name: str) -> Any:
        """Membaca semula struktur data JSON daripada shard berkaitan."""
        full_key = self._k(f"kv:{key_name}")
        shard = ledger_db.select_balanced_redis_shard(full_key)
        ok, raw = self.execute_command(shard, "GET", full_key, track_ops=True)
        if not ok or not raw:
            return None
        try:
            return json.loads(raw)
        except Exception:
            return None

    # --------------------------------------------------------------------------
    # 7. SEMAKAN REKOD TERTUA & STATUS 5 SHARD
    # --------------------------------------------------------------------------
    def get_oldest_post_across_shards(self) -> Optional[Dict[str, Any]]:
        """Mencari hantaran paling lama merentasi kesemua 5 shard Redis."""
        oldest_candidate: Optional[Tuple[str, float, Dict[str, Any]]] = None

        for shard in self.accounts:
            ok, res = self.execute_command(
                shard,
                "ZRANGE",
                self._k("timeline"),
                0,
                0,
                "WITHSCORES",
                track_ops=True,
            )
            if ok and isinstance(res, list) and len(res) >= 2:
                cand_uid = str(res[0])
                cand_ts = float(res[1])
                if oldest_candidate is None or cand_ts < oldest_candidate[1]:
                    oldest_candidate = (cand_uid, cand_ts, shard)

        if not oldest_candidate:
            return None

        target_uid, _, target_shard = oldest_candidate
        ok_get, raw_json = self.execute_command(
            target_shard,
            "GET",
            self._k(f"post:{target_uid}"),
            track_ops=True,
        )
        if ok_get and raw_json:
            try:
                return json.loads(raw_json)
            except Exception:
                pass
        return {"post_uid": target_uid, "redis_shard_index": target_shard["index"]}

    def inspect_all_shards(self) -> List[Dict[str, Any]]:
        """Memeriksa status dan jumlah kunci terkini bagi semua 5 shard Redis."""
        summaries: List[Dict[str, Any]] = []
        for shard in self.accounts:
            t0 = time.perf_counter()
            ok, results = self.execute_pipeline(
                shard,
                [["PING"], ["DBSIZE"], ["ZCARD", self._k("timeline")]],
                track_ops=False,
            )
            latency_ms = (time.perf_counter() - t0) * 1000
            online = ok and len(results) >= 3 and results[0] == "PONG"
            summaries.append(
                {
                    "index": int(shard["index"]),
                    "host": shard["url"].replace("https://", ""),
                    "online": online,
                    "dbsize": int(results[1] or 0) if online else 0,
                    "timeline_count": int(results[2] or 0) if online else 0,
                    "latency_ms": latency_ms,
                }
            )
        return summaries


# Singleton Instance utama untuk diimport oleh semua modul projek
redis_sharded = PS2RedisShardedManager()


# ==============================================================================
# DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ==============================================================================
if __name__ == "__main__":
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: UPSTASH REDIS 5-SHARD SMART ENGINE TEST (X04)[/bold cyan]",
            border_style="cyan",
        )
    )

    test_uid = f"test_post_{int(time.time())}"
    test_fp = f"sha256_demo_gta_sa_{int(time.time())}"

    saved_ok, used_shard = redis_sharded.save_published_post(
        post_uid=test_uid,
        post_meta={
            "fb_post_id": "1439014409296665_999999",
            "game_title": "GTA San Andreas",
            "post_type": "text",
            "shopee_product_code": "SHP_001",
        },
        dedup_fingerprint=test_fp,
        dedup_ttl_seconds=60,
    )

    fetched = redis_sharded.get_published_post(test_uid) if saved_ok else None
    is_dup, dup_owner = redis_sharded.is_duplicate_fingerprint(test_fp)

    # Ujian panggilan 1 argumen pada mark_dedup_fingerprint (keserasian X11)
    single_arg_fp = f"sha256_single_arg_{int(time.time())}"
    single_ok = redis_sharded.mark_dedup_fingerprint(single_arg_fp, ttl_seconds=30)

    # Ujian pengawal kuota gelongsor (contoh: 2 post setiap 2 hari)
    quota_ok, q_used, _ = redis_sharded.check_sliding_quota("test_reels_48h", max_events=2, window_seconds=172800)

    # Ujian penggiliran adil produk
    picked_prod = redis_sharded.pick_least_used_item("test_shopee_pool", ["SHP_001", "SHP_002", "SHP_003"])

    # Bersihkan rekod ujian sementara
    del_ok = redis_sharded.delete_published_post(test_uid, shard_index=used_shard)
    for fp_clean in (test_fp, single_arg_fp):
        fp_shard = ledger_db.select_balanced_redis_shard(redis_sharded._k(f"dedup:{fp_clean}"))
        redis_sharded.execute_command(fp_shard, "DEL", redis_sharded._k(f"dedup:{fp_clean}"), track_ops=False)
    q_shard = ledger_db.select_balanced_redis_shard(redis_sharded._k("quota:test_reels_48h"))
    redis_sharded.execute_command(q_shard, "DEL", redis_sharded._k("quota:test_reels_48h"), track_ops=False)
    r_shard = ledger_db.select_balanced_redis_shard(redis_sharded._k("rotate:test_shopee_pool"))
    redis_sharded.execute_command(r_shard, "DEL", redis_sharded._k("rotate:test_shopee_pool"), track_ops=False)

    shard_stats = redis_sharded.inspect_all_shards()

    tbl = Table(box=box.ROUNDED, header_style="bold cyan")
    tbl.add_column("Shard", justify="center", style="cyan")
    tbl.add_column("Host Upstash", style="white")
    tbl.add_column("Kunci (DBSIZE)", justify="right", style="yellow")
    tbl.add_column("Timeline", justify="right", style="magenta")
    tbl.add_column("Latensi", justify="right", style="dim")
    tbl.add_column("Status", justify="center")

    for s in shard_stats:
        tbl.add_row(
            f"#{s['index']:03d}",
            s["host"],
            str(s["dbsize"]),
            str(s["timeline_count"]),
            f"{s['latency_ms']:.0f} ms",
            "[bold green]ONLINE[/bold green]" if s["online"] else "[bold red]OFFLINE[/bold red]",
        )
    console.print(tbl)

    console.print(
        Panel(
            f"• Pipeline Write (Shard #{used_shard:03d}): [bold green]{'BERJAYA' if saved_ok else 'GAGAL'}[/bold green]\n"
            f"• Baca Semula Metadata: [bold green]{fetched.get('game_title') if fetched else 'GAGAL'}[/bold green]\n"
            f"• Anti-Duplikasi (1 & 2 Argumen): [bold green]{'LULUS (' + str(dup_owner) + ')' if (is_dup and single_ok) else 'GAGAL'}[/bold green]\n"
            f"• Kuota Gelongsor 48 Jam: [bold green]{'DIBENARKAN (Diguna: ' + str(q_used) + '/2)' if quota_ok else 'PENUH'}[/bold green]\n"
            f"• Penggiliran Adil Produk: [bold cyan]{picked_prod}[/bold cyan] | Pembersihan: [bold green]{'BERSIH' if del_ok else 'RALAT'}[/bold green]",
            title="Ringkasan Ujian Pintar X04",
            border_style="green",
        )
    )