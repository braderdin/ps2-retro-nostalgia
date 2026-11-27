#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - KLIEN STORAN B2 VIA CLOUDFLARE PROXY (X03)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/live-engine-01/X03_b2_proxy_client.py
#
# FUNGSI UTAMA (SERASI UNTUK SEMUA MODUL & PROJEK MASA HADAPAN):
# 1. Mengurus muat naik (PUT), muat turun (GET), semakan kewujudan (HEAD),
#    dan pemadaman (DELETE) fail media/JSON PS2 melalui Cloudflare Worker
#    (ps2n-b2-proxy) tanpa had egress.
# 2. Fleksibel: Menerima laluan fail dalam bentuk `Path` mahupun `str`, serta
#    alias parameter (`local_file`, `local_path`, `file_path`) supaya serasi
#    dengan X11, X13, Instagram cross-poster, dan modul masa hadapan.
# 3. Menyokong muat naik terus daripada memori (`upload_bytes` & `upload_json`)
#    tanpa perlu mencipta fail sementara terlebih dahulu.
# 4. Memilih baldi B2 secara automatik melalui formula agihan sekata X02 dan
#    merekod aktiviti harian serta inventori aset ke SQLite3 Ledger.
# ==============================================================================

import hashlib
import json
import mimetypes
import sys
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple, Union
from urllib.parse import quote

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
from X02_sqlite_ledger import format_bytes, ledger_db

# Had selamat muat naik per fail untuk pelan percuma Cloudflare Workers (had mutlak 100 MB)
CF_WORKER_MAX_UPLOAD_BYTES = 95 * 1024 * 1024  # 95.0 MB


def compute_file_sha256(file_path: Union[str, Path]) -> str:
    p = Path(file_path)
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def detect_mime_type(file_path: Union[str, Path]) -> str:
    p = Path(file_path)
    ext = p.suffix.lower()
    explicit_map = {
        ".mp4": "video/mp4",
        ".mkv": "video/x-matroska",
        ".webm": "video/webm",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".json": "application/json",
        ".txt": "text/plain; charset=utf-8",
    }
    if ext in explicit_map:
        return explicit_map[ext]
    guessed, _ = mimetypes.guess_type(str(p))
    return guessed or "application/octet-stream"


def encode_remote_path(remote_path: str) -> str:
    clean = remote_path.strip().lstrip("/")
    return "/".join(quote(seg, safe="") for seg in clean.split("/") if seg)


class B2ProxyStorageClient:
    """
    Klien Berpusat Backblaze B2 melalui Cloudflare Worker Proxy.
    Menyokong muat naik fail (`Path` / `str`), bait mentah, dan objek JSON
    merentasi kesemua baldi B2 secara seimbang.
    """

    def __init__(
        self,
        proxy_base: str = cfg.CF_WORKER_B2_PROXY_STORAGE,
        proxy_secret: str = cfg.PS2N_PROXY_SECRET,
    ) -> None:
        self.proxy_base = proxy_base.rstrip("/")
        self.proxy_secret = proxy_secret.strip()

    def _find_account_index(self, bucket_name: str) -> int:
        target = bucket_name.lower().strip()
        for acc in cfg.B2_ACCOUNTS:
            if acc["bucket_name"].lower() == target:
                return int(acc["index"])
        return 1

    def build_public_url(self, bucket_name: str, remote_path: str) -> str:
        """Membina URL awam Edge-Cached melalui Cloudflare Worker."""
        encoded = encode_remote_path(remote_path)
        return f"{self.proxy_base}/{bucket_name}/{encoded}"

    def check_health(self) -> Tuple[bool, Dict[str, Any]]:
        """Menyemak status kesihatan proksi Cloudflare Worker."""
        t0 = time.perf_counter()
        try:
            resp = requests.get(f"{self.proxy_base}/health", timeout=15)
            latency_ms = (time.perf_counter() - t0) * 1000
            if resp.status_code != 200:
                return False, {"latency_ms": latency_ms, "error": f"HTTP {resp.status_code}"}
            data = resp.json()
            data["latency_ms"] = latency_ms
            ok = data.get("status") == "ok" and bool(data.get("proxy_secret_configured"))
            return ok, data
        except Exception as exc:
            return False, {"latency_ms": (time.perf_counter() - t0) * 1000, "error": str(exc)}

    def file_exists(self, bucket_name: str, remote_path: str) -> bool:
        """
        Menyemak sama ada fail wujud di B2 / Cloudflare Edge Cache (HEAD/GET ringan),
        berguna sebelum menghantar `proxy_url` kepada Instagram Graph API.
        """
        target_url = self.build_public_url(bucket_name, remote_path)
        try:
            resp = requests.head(target_url, timeout=12, allow_redirects=True)
            if resp.status_code == 200:
                return True
            if resp.status_code == 405:
                r_get = requests.get(target_url, stream=True, timeout=12)
                ok = r_get.status_code == 200
                r_get.close()
                return ok
        except Exception:
            pass
        return False

    def _put_stream_to_worker(
        self,
        data_source: Any,
        file_size: int,
        content_type: str,
        file_sha256: str,
        remote_path: str,
        target_account: Optional[Dict[str, Any]] = None,
        asset_meta: Optional[Dict[str, Any]] = None,
        register_in_ledger: bool = True,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """Enjin dalaman penghantaran PUT ke Cloudflare Worker Proxy."""
        if not self.proxy_secret:
            return False, {}, "PS2N_PROXY_SECRET tiada dalam konfigurasi."

        if file_size <= 0:
            return False, {}, "Fail kosong (0 bait) tidak dibenarkan."
        if file_size > CF_WORKER_MAX_UPLOAD_BYTES:
            return (
                False,
                {},
                f"Saiz fail ({format_bytes(file_size)}) melebihi had selamat Worker (95.0 MB).",
            )

        chosen_acc = target_account or ledger_db.select_balanced_b2_account(required_bytes=file_size)
        if not chosen_acc:
            return False, {}, "Tiada baldi B2 yang mempunyai baki ruang selamat mencukupi."

        bucket_name = chosen_acc["bucket_name"]
        acc_index = int(chosen_acc["index"])
        clean_remote = remote_path.strip().lstrip("/")
        upload_url = self.build_public_url(bucket_name, clean_remote)

        headers = {
            "X-PS2N-Proxy-Key": self.proxy_secret,
            "Content-Type": content_type,
            "Content-Length": str(file_size),
        }

        t0 = time.perf_counter()
        try:
            resp = requests.put(upload_url, headers=headers, data=data_source, timeout=120)
            latency_ms = (time.perf_counter() - t0) * 1000

            if resp.status_code != 200:
                return (
                    False,
                    {"http_status": resp.status_code, "latency_ms": latency_ms},
                    f"Gagal muat naik melalui Proxy (HTTP {resp.status_code}): {resp.text[:180]}",
                )

            payload = resp.json()
            if not payload.get("success"):
                return False, payload, f"Worker memulangkan ralat: {payload}"

            public_url = payload.get("proxy_url") or upload_url
            used_map = ledger_db.get_total_b2_bytes_by_account()
            updated_bytes = used_map.get(acc_index, 0) + file_size
            ledger_db.record_account_activity(
                service_type="b2",
                account_index=acc_index,
                ops_delta=1,
                bytes_used=updated_bytes,
            )

            result_info = {
                "bucket_index": acc_index,
                "bucket_name": bucket_name,
                "file_path": clean_remote,
                "file_id": payload.get("file_id", ""),
                "size_bytes": file_size,
                "sha256": file_sha256,
                "content_type": content_type,
                "proxy_url": public_url,
                "latency_ms": round(latency_ms, 1),
            }

            if register_in_ledger and asset_meta:
                ledger_db.register_media_asset(
                    {
                        "asset_uid": asset_meta["asset_uid"],
                        "game_title": asset_meta.get("game_title", "PS2 Classic"),
                        "media_type": asset_meta.get(
                            "media_type",
                            "video" if content_type.startswith("video/") else "image",
                        ),
                        "source_provider": asset_meta.get("source_provider", "local_gameplay"),
                        "b2_account_index": acc_index,
                        "b2_bucket": bucket_name,
                        "b2_file_path": clean_remote,
                        "proxy_url": public_url,
                        "size_bytes": file_size,
                        "sha256": file_sha256,
                        "status": asset_meta.get("status", "ready"),
                    }
                )

            return True, result_info, "Muat naik B2 melalui Cloudflare Proxy berjaya."
        except Exception as exc:
            return False, {}, f"Ralat sambungan semasa muat naik B2 Proxy: {exc}"

    def upload_file(
        self,
        local_file: Optional[Union[str, Path]] = None,
        remote_path: str = "",
        target_account: Optional[Dict[str, Any]] = None,
        asset_meta: Optional[Dict[str, Any]] = None,
        register_in_ledger: bool = True,
        *,
        local_path: Optional[Union[str, Path]] = None,
        file_path: Optional[Union[str, Path]] = None,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """
        Memuat naik fail tempatan ke B2 melalui Cloudflare Worker Proxy (PUT).
        Menerima kedua-dua jenis `Path` dan `str`, serta alias `local_file`,
        `local_path`, atau `file_path` untuk keserasian penuh semua modul.
        """
        raw_src = local_file if local_file is not None else (local_path if local_path is not None else file_path)
        if raw_src is None:
            return False, {}, "Tiada laluan fail tempatan dibekalkan."

        src_path = Path(raw_src)
        if not src_path.exists() or not src_path.is_file():
            return False, {}, f"Fail tempatan tidak ditemui: {src_path}"

        dest_remote = remote_path.strip() or f"uploads/{src_path.name}"
        file_size = src_path.stat().st_size
        content_type = detect_mime_type(src_path)
        file_sha256 = compute_file_sha256(src_path)

        with open(src_path, "rb") as f_stream:
            return self._put_stream_to_worker(
                data_source=f_stream,
                file_size=file_size,
                content_type=content_type,
                file_sha256=file_sha256,
                remote_path=dest_remote,
                target_account=target_account,
                asset_meta=asset_meta,
                register_in_ledger=register_in_ledger,
            )

    def upload_bytes(
        self,
        data: bytes,
        remote_path: str,
        content_type: str = "application/octet-stream",
        target_account: Optional[Dict[str, Any]] = None,
        asset_meta: Optional[Dict[str, Any]] = None,
        register_in_ledger: bool = True,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """Memuat naik bait mentah terus dari memori ke B2 tanpa mencipta fail sementara."""
        if not isinstance(data, (bytes, bytearray)):
            return False, {}, "Data mestilah dalam format bytes."
        raw_bytes = bytes(data)
        sha256_hex = hashlib.sha256(raw_bytes).hexdigest()
        return self._put_stream_to_worker(
            data_source=raw_bytes,
            file_size=len(raw_bytes),
            content_type=content_type,
            file_sha256=sha256_hex,
            remote_path=remote_path,
            target_account=target_account,
            asset_meta=asset_meta,
            register_in_ledger=register_in_ledger,
        )

    def upload_json(
        self,
        payload: Any,
        remote_path: str,
        target_account: Optional[Dict[str, Any]] = None,
    ) -> Tuple[bool, Dict[str, Any], str]:
        """Menukar objek Python (dict/list) kepada JSON UTF-8 dan memuat naik terus ke B2."""
        raw_bytes = json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")
        return self.upload_bytes(
            data=raw_bytes,
            remote_path=remote_path,
            content_type="application/json",
            target_account=target_account,
            register_in_ledger=False,
        )

    def download_file(
        self,
        bucket_name: str,
        remote_path: str,
        local_dest: Union[str, Path],
    ) -> Tuple[bool, int, str]:
        """Memuat turun fail dari B2 melalui Edge Cache Cloudflare Worker (GET)."""
        dest_path = Path(local_dest)
        download_url = self.build_public_url(bucket_name, remote_path)
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with requests.get(download_url, stream=True, timeout=90) as resp:
                if resp.status_code != 200:
                    return False, 0, f"Gagal muat turun (HTTP {resp.status_code})"

                total_written = 0
                with open(dest_path, "wb") as out_f:
                    for chunk in resp.iter_content(chunk_size=1024 * 256):
                        if chunk:
                            out_f.write(chunk)
                            total_written += len(chunk)

            return True, total_written, "Muat turun selesai."
        except Exception as exc:
            return False, 0, f"Ralat muat turun B2 Proxy: {exc}"

    def delete_file(
        self,
        bucket_name: str,
        remote_path: str,
        acc_index: Optional[int] = None,
    ) -> Tuple[bool, str]:
        """Memadam fail daripada baldi B2 & membersihkan Edge Cache Cloudflare (DELETE)."""
        if not self.proxy_secret:
            return False, "PS2N_PROXY_SECRET tiada dalam konfigurasi."

        target_url = self.build_public_url(bucket_name, remote_path)
        resolved_idx = acc_index or self._find_account_index(bucket_name)

        try:
            resp = requests.delete(
                target_url,
                headers={"X-PS2N-Proxy-Key": self.proxy_secret},
                timeout=30,
            )
            if resp.status_code != 200:
                return False, f"Gagal padam (HTTP {resp.status_code}): {resp.text[:150]}"

            data = resp.json()
            if data.get("success"):
                ledger_db.record_account_activity("b2", resolved_idx, ops_delta=1)
                return True, f"Fail {remote_path} berjaya dipadam daripada {bucket_name}."
            return False, f"Respons padam tidak berjaya: {data}"
        except Exception as exc:
            return False, f"Ralat semasa memadam fail B2 Proxy: {exc}"


# Singleton Instance utama untuk diimport oleh semua modul projek
b2_proxy = B2ProxyStorageClient()


# ==============================================================================
# DIAGNOSTIK KENDIRI (APABILA DIJALANKAN SECARA TERUS)
# ==============================================================================
if __name__ == "__main__":
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: B2 PROXY STORAGE CLIENT SMART TEST (X03)[/bold cyan]\n"
            f"[dim]Endpoint: {b2_proxy.proxy_base}[/dim]",
            border_style="cyan",
        )
    )

    health_ok, health_info = b2_proxy.check_health()
    if not health_ok:
        console.print(f"[bold red]❌ Worker tidak sedia: {health_info}[/bold red]")
        sys.exit(1)

    probe_local = cfg.TEMP_DIR / f"x03_probe_{int(time.time())}.txt"
    probe_downloaded = cfg.TEMP_DIR / f"x03_probe_dl_{int(time.time())}.txt"
    probe_text = f"PS2N X03 SMART UPLOAD (STR & PATH) & EDGE STREAM TEST @ {int(time.time())}"
    probe_local.write_text(probe_text, encoding="utf-8")

    remote_probe_path = f"_self_test/{probe_local.name}"

    try:
        # Uji menggunakan laluan str(probe_local) untuk mengesahkan keserasian X11/X13
        up_ok, up_info, up_msg = b2_proxy.upload_file(
            str(probe_local),
            remote_probe_path,
            register_in_ledger=False,
        )

        exists_ok = False
        dl_ok, dl_bytes, dl_msg = (False, 0, "Dilangkau")
        content_match = False
        del_ok, del_msg = (False, "Dilangkau")

        if up_ok:
            exists_ok = b2_proxy.file_exists(
                bucket_name=up_info["bucket_name"],
                remote_path=remote_probe_path,
            )
            dl_ok, dl_bytes, dl_msg = b2_proxy.download_file(
                bucket_name=up_info["bucket_name"],
                remote_path=remote_probe_path,
                local_dest=str(probe_downloaded),
            )
            if dl_ok and probe_downloaded.exists():
                content_match = probe_downloaded.read_text(encoding="utf-8") == probe_text

            del_ok, del_msg = b2_proxy.delete_file(
                bucket_name=up_info["bucket_name"],
                remote_path=remote_probe_path,
                acc_index=up_info["bucket_index"],
            )

        tbl = Table(box=box.ROUNDED, header_style="bold green")
        tbl.add_column("Langkah Ujian Pintar X03", style="bold white")
        tbl.add_column("Butiran", style="cyan")
        tbl.add_column("Status", justify="center")

        tbl.add_row(
            "1. Worker Health",
            f"{health_info.get('buckets_loaded')} baldi aktif ({health_info.get('latency_ms', 0):.0f} ms)",
            "[bold green]OK[/bold green]",
        )
        tbl.add_row(
            "2. Balanced PUT (str & Path)",
            f"Baldi #{up_info.get('bucket_index', '-')} ({up_info.get('bucket_name', '-')}) | {up_info.get('latency_ms', 0):.0f} ms"
            if up_ok
            else up_msg,
            "[bold green]OK[/bold green]" if up_ok else "[bold red]GAGAL[/bold red]",
        )
        tbl.add_row(
            "3. Edge HEAD & GET Download",
            f"Wujud: {exists_ok} | {dl_bytes} B | Integriti: {'Sepadan' if content_match else 'Tidak Sepadan'}"
            if dl_ok
            else dl_msg,
            "[bold green]OK[/bold green]" if (dl_ok and content_match) else "[bold red]GAGAL[/bold red]",
        )
        tbl.add_row(
            "4. Proxy DELETE Clean",
            del_msg,
            "[bold green]OK[/bold green]" if del_ok else "[bold red]GAGAL[/bold red]",
        )
        console.print(tbl)

    finally:
        for p in (probe_local, probe_downloaded):
            if p.exists():
                p.unlink()