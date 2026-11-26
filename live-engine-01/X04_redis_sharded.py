#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - ENJIN METADATA UPSTASH REDIS 5-SHARD (X04)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X04_redis_sharded.py
#
# FUNGSI:
# 1. Mengagihkan rekod hantaran & kunci anti-duplikasi secara sekata merentasi
#    5 shard Upstash Redis menggunakan CRC32 + pelindung kuota harian dari X02.
# 2. Menggunakan Upstash REST /pipeline untuk menggabungkan beberapa arahan
#    dalam satu panggilan HTTP bagi mengurangkan latensi.
# 3. Menyokong penjejakan garis masa siaran (timeline) & semakan kesihatan
#    kesemua 5 shard.
# ==============================================================================

import json
import sys
import time
from typing import Any, Dict, List, Optional, Tuple
from pathlib import Path

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
    def __init__(self, accounts: List[Dict[str, Any]] = cfg.REDIS_ACCOUNTS):
        self.accounts = accounts
        self.timeout = 10.0

    def _get_shard_by_index(self, shard_index: int) -> Optional[Dict[str, Any]]:
        for acc in self.accounts:
            if int(acc["index"]) == int(shard_index):
                return acc
        return None

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
    # PENGURUSAN METADATA HANTARAN & GARIS MASA
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
        shard = ledger_db.select_balanced_redis_shard(post_uid)
        shard_idx = int(shard["index"])
        published_ts = int(post_meta.get("published_at", time.time()))

        enriched_meta = dict(post_meta)
        enriched_meta["post_uid"] = post_uid
        enriched_meta["redis_shard_index"] = shard_idx
        enriched_meta["published_at"] = published_ts

        serialized_json = json.dumps(enriched_meta, ensure_ascii=False, separators=(",", ":"))

        pipeline_cmds: List[List[Any]] = [
            ["SET", f"ps2n:post:{post_uid}", serialized_json],
            ["ZADD", "ps2n:timeline", published_ts, post_uid],
        ]

        ok, res_list = self.execute_pipeline(shard, pipeline_cmds, track_ops=True)
        if not ok or not res_list or res_list[0] != "OK":
            return False, shard_idx

        if dedup_fingerprint:
            self.mark_dedup_fingerprint(dedup_fingerprint, post_uid, ttl_seconds=dedup_ttl_seconds)

        return True, shard_idx

    def get_published_post(self, post_uid: str) -> Optional[Dict[str, Any]]:
        """Membaca metadata hantaran daripada shard berkaitan."""
        shard = ledger_db.select_balanced_redis_shard(post_uid)
        ok, raw = self.execute_command(shard, "GET", f"ps2n:post:{post_uid}", track_ops=True)
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
            else ledger_db.select_balanced_redis_shard(post_uid)
        )
        if not shard:
            return False

        cmds = [
            ["DEL", f"ps2n:post:{post_uid}"],
            ["ZREM", "ps2n:timeline", post_uid],
        ]
        ok, _ = self.execute_pipeline(shard, cmds, track_ops=True)
        return ok

    # --------------------------------------------------------------------------
    # PENAPIS ANTI-DUPLIKASI (MEDIA HASH / TOPIK)
    # --------------------------------------------------------------------------
    def mark_dedup_fingerprint(
        self,
        fingerprint: str,
        post_uid: str,
        ttl_seconds: int = 90 * 86400,
    ) -> bool:
        clean_fp = fingerprint.strip().lower()
        shard = ledger_db.select_balanced_redis_shard(f"dedup:{clean_fp}")
        ok, res = self.execute_command(
            shard,
            "SET",
            f"ps2n:dedup:{clean_fp}",
            post_uid,
            "EX",
            ttl_seconds,
            track_ops=True,
        )
        return ok and res == "OK"

    def is_duplicate_fingerprint(self, fingerprint: str) -> Tuple[bool, Optional[str]]:
        """Menyemak sama ada cincangan media atau topik sudah pernah diterbitkan."""
        clean_fp = fingerprint.strip().lower()
        shard = ledger_db.select_balanced_redis_shard(f"dedup:{clean_fp}")
        ok, existing_post_uid = self.execute_command(
            shard,
            "GET",
            f"ps2n:dedup:{clean_fp}",
            track_ops=True,
        )
        if ok and existing_post_uid:
            return True, str(existing_post_uid)
        return False, None

    # --------------------------------------------------------------------------
    # SEMAKAN REKOD TERTUA & STATUS 5 SHARD
    # --------------------------------------------------------------------------
    def get_oldest_post_across_shards(self) -> Optional[Dict[str, Any]]:
        """Mencari hantaran paling lama merentasi kesemua 5 shard Redis."""
        oldest_candidate: Optional[Tuple[str, float, Dict[str, Any]]] = None

        for shard in self.accounts:
            ok, res = self.execute_command(
                shard,
                "ZRANGE",
                "ps2n:timeline",
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
            f"ps2n:post:{target_uid}",
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
                [["PING"], ["DBSIZE"], ["ZCARD", "ps2n:timeline"]],
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


# Singleton Instance untuk diimport oleh modul lain
redis_sharded = PS2RedisShardedManager()


# ==============================================================================
# DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ==============================================================================
if __name__ == "__main__":
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: UPSTASH REDIS 5-SHARD PIPELINE TEST (X04)[/bold cyan]",
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
            "post_type": "reel",
            "shopee_product_type": "pendrive",
        },
        dedup_fingerprint=test_fp,
        dedup_ttl_seconds=60,
    )

    fetched = redis_sharded.get_published_post(test_uid) if saved_ok else None
    is_dup, dup_owner = redis_sharded.is_duplicate_fingerprint(test_fp)

    # Bersihkan rekod ujian sementara
    del_ok = redis_sharded.delete_published_post(test_uid, shard_index=used_shard)
    fp_shard = ledger_db.select_balanced_redis_shard(f"dedup:{test_fp}")
    redis_sharded.execute_command(fp_shard, "DEL", f"ps2n:dedup:{test_fp}", track_ops=False)

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
            f"• Semakan Anti-Duplikasi: [bold green]{'DIKESAN (' + str(dup_owner) + ')' if is_dup else 'GAGAL'}[/bold green]\n"
            f"• Pembersihan Kunci Ujian: [bold green]{'BERSIH' if del_ok else 'RALAT'}[/bold green]",
            title="Ringkasan Ujian Pipeline X04",
            border_style="green",
        )
    )