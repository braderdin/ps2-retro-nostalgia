#!/usr/bin/env python3
# ==============================================================================
# PROJEK: PS2-RETRO-NOSTALGIA - DIAGNOSTIK INFRASTRUKTUR AWAN & B2 PROXY (EXP02)
# LOKASI: /home/braderdin/ps2-retro-nostalgia/exp/exp02_cloud_proxy_infra_debug.py
#
# FUNGSI UJIAN:
# 1. Audit semua kunci B2 (5), Upstash Redis (5), Upstash Vector (5), & CF Proxy
#    daripada /home/braderdin/ps2-retro-nostalgia/.env.local.
# 2. Semak status /health Cloudflare Worker (ps2n-b2-proxy).
# 3. Ujian kitaran penuh Backblaze B2 melalui Cloudflare Proxy bagi semua 5 akaun:
#    Direct Auth/Kapasiti -> Proxy PUT (Upload) -> Proxy GET (Verify) -> Proxy DELETE.
# 4. Ujian PING, DBSIZE, RAM, dan Baca/Tulis sementara pada 5 shard Upstash Redis.
# 5. Ujian Dimensi, Metrik, dan Upsert/Delete sementara pada 5 indeks Upstash Vector.
# ==============================================================================

import base64
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from dotenv import dotenv_values
from rich import box
from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from upstash_vector import Index as VectorIndex

console = Console()

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_LOCAL_FILE = PROJECT_ROOT / ".env.local"
MYT = timezone(timedelta(hours=8))

B2_SAFE_LIMIT_BYTES = int(9.0 * 1024 * 1024 * 1024)      # Had selamat: 9.0 GB / akaun
B2_CEILING_LIMIT_BYTES = int(10.0 * 1024 * 1024 * 1024)   # Had percuma: 10.0 GB / akaun
REDIS_MAX_MEMORY_BYTES = 256 * 1024 * 1024                # Had RAM: 256 MB / akaun


def clean_val(val: Optional[str]) -> str:
    if not val:
        return ""
    return str(val).strip().strip('"').strip("'")


def mask_secret(val: str, start: int = 6, end: int = 5) -> str:
    if not val:
        return "-"
    if len(val) <= (start + end):
        return "*" * len(val)
    return f"{val[:start]}...{val[-end:]}"


def format_size(bytes_val: int) -> str:
    if bytes_val < 1024:
        return f"{bytes_val} B"
    if bytes_val < 1024 * 1024:
        return f"{bytes_val / 1024:.1f} KB"
    if bytes_val < 1024 * 1024 * 1024:
        return f"{bytes_val / (1024 * 1024):.2f} MB"
    return f"{bytes_val / (1024 * 1024 * 1024):.3f} GB"


def load_all_configs(env_path: Path) -> Tuple[str, str, List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    raw_env = dotenv_values(str(env_path))

    proxy_url = clean_val(raw_env.get("CF_WORKER_B2_PROXY_STORAGE")).rstrip("/")
    proxy_secret = clean_val(raw_env.get("PS2N_PROXY_SECRET"))

    # 1. Ekstrak 5 Akaun Backblaze B2
    b2_accounts: List[Dict[str, Any]] = []
    for idx in range(1, 11):
        for fmt in [f"{idx}", f"{idx:02d}", f"{idx:03d}"]:
            pfx = f"B2_ACC{fmt}_"
            key_id = clean_val(raw_env.get(f"{pfx}KEY_ID"))
            app_key = clean_val(raw_env.get(f"{pfx}APP_KEY"))
            bucket_name = clean_val(raw_env.get(f"{pfx}BUCKET_NAME"))
            if key_id and app_key and bucket_name:
                b2_accounts.append({
                    "index": len(b2_accounts) + 1,
                    "key_id": key_id,
                    "app_key": app_key,
                    "bucket_name": bucket_name,
                    "bucket_id": clean_val(raw_env.get(f"{pfx}BUCKET_ID")),
                    "endpoint": clean_val(raw_env.get(f"{pfx}S3_API_ENDPOINT")) or "s3.us-west-004.backblazeb2.com",
                })
                break

    # 2. Ekstrak 5 Akaun Upstash Redis
    redis_accounts: List[Dict[str, Any]] = []
    for idx in range(1, 11):
        for fmt in [f"{idx:03d}", f"{idx:02d}", f"{idx}"]:
            url = clean_val(raw_env.get(f"UPSTASH_REDIS_{fmt}_REST_URL"))
            token = clean_val(raw_env.get(f"UPSTASH_REDIS_{fmt}_REST_TOKEN"))
            if url and token:
                redis_accounts.append({
                    "index": len(redis_accounts) + 1,
                    "url": url.rstrip("/"),
                    "token": token,
                })
                break

    # 3. Ekstrak 5 Akaun Upstash Vector
    vector_accounts: List[Dict[str, Any]] = []
    for idx in range(1, 11):
        for fmt in [f"{idx:03d}", f"{idx:02d}", f"{idx}"]:
            url = clean_val(raw_env.get(f"UPSTASH_VECTOR_{fmt}_REST_URL"))
            token = clean_val(raw_env.get(f"UPSTASH_VECTOR_{fmt}_REST_TOKEN"))
            if url and token:
                vector_accounts.append({
                    "index": len(vector_accounts) + 1,
                    "url": url.rstrip("/"),
                    "token": token,
                })
                break

    return proxy_url, proxy_secret, b2_accounts, redis_accounts, vector_accounts


# ==============================================================================
# UJIAN 1: CLOUDFLARE WORKER HEALTH CHECK
# ==============================================================================
def check_cf_worker_health(proxy_url: str) -> Tuple[bool, Dict[str, Any]]:
    start_t = time.perf_counter()
    try:
        resp = requests.get(f"{proxy_url}/health", timeout=15)
        latency = (time.perf_counter() - start_t) * 1000
        if resp.status_code != 200:
            return False, {"latency": latency, "error": f"HTTP {resp.status_code}: {resp.text[:150]}"}
        data = resp.json()
        data["latency"] = latency
        ok = (
            data.get("status") == "ok"
            and int(data.get("buckets_loaded", 0)) >= 5
            and bool(data.get("proxy_secret_configured"))
        )
        return ok, data
    except Exception as exc:
        latency = (time.perf_counter() - start_t) * 1000
        return False, {"latency": latency, "error": str(exc)}


# ==============================================================================
# UJIAN 2: BACKBLAZE B2 (DIRECT CAPACITY + PROXY UPLOAD / DOWNLOAD / DELETE)
# ==============================================================================
def get_direct_b2_stats(acc: Dict[str, Any]) -> Tuple[bool, int, int, str]:
    """Mengesahkan kunci B2 terus dengan Backblaze dan mengira fail/storan semasa."""
    try:
        pair = f"{acc['key_id']}:{acc['app_key']}"
        b64_auth = base64.b64encode(pair.encode("utf-8")).decode("utf-8")
        auth_resp = requests.get(
            "https://api.backblazeb2.com/b2api/v2/b2_authorize_account",
            headers={"Authorization": f"Basic {b64_auth}"},
            timeout=12,
        )
        if auth_resp.status_code != 200:
            return False, 0, 0, f"Auth B2 HTTP {auth_resp.status_code}"

        auth_data = auth_resp.json()
        api_url = auth_data["apiUrl"]
        token = auth_data["authorizationToken"]
        bucket_id = acc.get("bucket_id", "")

        if not bucket_id:
            b_resp = requests.post(
                f"{api_url}/b2api/v2/b2_list_buckets",
                headers={"Authorization": token},
                json={"accountId": auth_data["accountId"], "bucketName": acc["bucket_name"]},
                timeout=12,
            )
            if b_resp.status_code == 200:
                for b in b_resp.json().get("buckets", []):
                    if b.get("bucketName") == acc["bucket_name"]:
                        bucket_id = b.get("bucketId", "")
                        break

        if not bucket_id:
            return False, 0, 0, "Bucket ID tidak ditemui"

        list_resp = requests.post(
            f"{api_url}/b2api/v2/b2_list_file_names",
            headers={"Authorization": token},
            json={"bucketId": bucket_id, "maxFileCount": 1000},
            timeout=15,
        )
        if list_resp.status_code != 200:
            return False, 0, 0, f"List HTTP {list_resp.status_code}"

        files = list_resp.json().get("files", [])
        total_bytes = sum(int(f.get("contentLength", 0)) for f in files)
        return True, len(files), total_bytes, ""
    except Exception as exc:
        return False, 0, 0, str(exc)


def test_single_b2_full_cycle(
    acc: Dict[str, Any],
    proxy_url: str,
    proxy_secret: str,
) -> Dict[str, Any]:
    idx = acc["index"]
    bucket_name = acc["bucket_name"]
    result: Dict[str, Any] = {
        "index": idx,
        "bucket_name": bucket_name,
        "direct_ok": False,
        "files": 0,
        "bytes": 0,
        "put_ok": False,
        "get_ok": False,
        "del_ok": False,
        "latency_ms": 0.0,
        "error": "",
    }

    # 1. Direct B2 Check
    direct_ok, f_count, u_bytes, direct_err = get_direct_b2_stats(acc)
    result["direct_ok"] = direct_ok
    result["files"] = f_count
    result["bytes"] = u_bytes
    if not direct_ok:
        result["error"] = f"Direct B2: {direct_err}"
        return result

    # 2. Proxy Round-Trip (PUT -> GET -> DELETE)
    ts_nonce = int(time.time() * 1000)
    probe_path = f"_diag_test/exp02_probe_acc{idx}_{ts_nonce}.txt"
    probe_content = f"PS2N_B2_PROXY_TEST|BUCKET={bucket_name}|TS={ts_nonce}".encode("utf-8")
    target_url = f"{proxy_url}/{bucket_name}/{probe_path}"
    write_headers = {
        "X-PS2N-Proxy-Key": proxy_secret,
        "Content-Type": "text/plain; charset=utf-8",
    }

    t0 = time.perf_counter()
    try:
        # A. PUT (Upload melalui Cloudflare Worker)
        put_resp = requests.put(target_url, headers=write_headers, data=probe_content, timeout=25)
        if put_resp.status_code != 200 or not put_resp.json().get("success"):
            result["error"] = f"PUT Gagal (HTTP {put_resp.status_code}): {put_resp.text[:120]}"
            result["latency_ms"] = (time.perf_counter() - t0) * 1000
            return result
        result["put_ok"] = True

        # B. GET (Download awam melalui Cloudflare Worker tanpa header rahsia)
        get_resp = requests.get(target_url, timeout=20)
        if get_resp.status_code != 200 or get_resp.content != probe_content:
            result["error"] = f"GET Tidak Sepadan (HTTP {get_resp.status_code})"
            # Cuba bersihkan fail jika GET gagal
            requests.delete(target_url, headers={"X-PS2N-Proxy-Key": proxy_secret}, timeout=15)
            result["latency_ms"] = (time.perf_counter() - t0) * 1000
            return result
        result["get_ok"] = True

        # C. DELETE (Padam fail ujian melalui Cloudflare Worker)
        del_resp = requests.delete(
            target_url,
            headers={"X-PS2N-Proxy-Key": proxy_secret},
            timeout=20,
        )
        if del_resp.status_code != 200 or not del_resp.json().get("success"):
            result["error"] = f"DELETE Gagal (HTTP {del_resp.status_code}): {del_resp.text[:120]}"
            result["latency_ms"] = (time.perf_counter() - t0) * 1000
            return result
        result["del_ok"] = True

    except Exception as exc:
        result["error"] = str(exc)
    finally:
        result["latency_ms"] = (time.perf_counter() - t0) * 1000

    return result


# ==============================================================================
# UJIAN 3: UPSTASH REDIS (PING, INFO, SET -> GET -> DEL)
# ==============================================================================
def redis_cmd(url: str, token: str, command: str, *args: Any) -> Tuple[bool, Any]:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = [command] + [str(a) for a in args]
    resp = requests.post(url, headers=headers, json=payload, timeout=10)
    if resp.status_code != 200:
        return False, f"HTTP {resp.status_code}: {resp.text[:100]}"
    return True, resp.json().get("result")


def test_single_redis(acc: Dict[str, Any]) -> Dict[str, Any]:
    idx = acc["index"]
    url = acc["url"]
    token = acc["token"]
    host = url.replace("https://", "").split("/")[0]

    res: Dict[str, Any] = {
        "index": idx,
        "host": host,
        "online": False,
        "rw_ok": False,
        "keys": 0,
        "mem_str": "-",
        "mem_pct": 0.0,
        "total_cmds": 0,
        "latency_ms": 0.0,
        "error": "",
    }

    t0 = time.perf_counter()
    try:
        ok_ping, pong = redis_cmd(url, token, "PING")
        if not ok_ping or pong != "PONG":
            res["error"] = f"PING gagal: {pong}"
            return res
        res["online"] = True

        ok_db, dbsize = redis_cmd(url, token, "DBSIZE")
        res["keys"] = int(dbsize) if ok_db and dbsize is not None else 0

        ok_info, raw_info = redis_cmd(url, token, "INFO")
        if ok_info and isinstance(raw_info, str):
            used_mem = 0
            for line in raw_info.splitlines():
                if line.startswith("used_memory:"):
                    used_mem = int(line.split(":", 1)[1].strip())
                elif line.startswith("used_memory_human:"):
                    res["mem_str"] = line.split(":", 1)[1].strip()
                elif line.startswith("total_commands_processed:"):
                    res["total_cmds"] = int(line.split(":", 1)[1].strip())
            res["mem_pct"] = (used_mem / REDIS_MAX_MEMORY_BYTES) * 100

        # Ujian baca/tulis/padam kunci sementara
        probe_key = f"ps2n:exp02:probe:{idx}"
        probe_val = f"ok_{int(time.time())}"
        ok_set, _ = redis_cmd(url, token, "SET", probe_key, probe_val, "EX", 30)
        ok_get, got_val = redis_cmd(url, token, "GET", probe_key)
        ok_del, _ = redis_cmd(url, token, "DEL", probe_key)

        if ok_set and ok_get and got_val == probe_val and ok_del:
            res["rw_ok"] = True
        else:
            res["error"] = "Kunci baca-sahaja atau gagal SET/GET/DEL"

    except Exception as exc:
        res["error"] = str(exc)
    finally:
        res["latency_ms"] = (time.perf_counter() - t0) * 1000

    return res


# ==============================================================================
# UJIAN 4: UPSTASH VECTOR (INFO, UPSERT -> DELETE PROBE)
# ==============================================================================
def test_single_vector(acc: Dict[str, Any]) -> Dict[str, Any]:
    idx = acc["index"]
    url = acc["url"]
    token = acc["token"]
    host = url.replace("https://", "").split("/")[0]

    res: Dict[str, Any] = {
        "index": idx,
        "host": host,
        "online": False,
        "rw_ok": False,
        "dimension": 0,
        "metric": "-",
        "vector_count": 0,
        "latency_ms": 0.0,
        "error": "",
    }

    t0 = time.perf_counter()
    try:
        v_index = VectorIndex(url=url, token=token)
        info = v_index.info()

        dim = int(getattr(info, "dimension", 0) or 0)
        metric = str(getattr(info, "similarity_function", "-") or "-")
        v_count = int(getattr(info, "vector_count", 0) or 0)

        res["online"] = True
        res["dimension"] = dim
        res["metric"] = metric
        res["vector_count"] = v_count

        # Ujian upsert & delete vektor sementara (dan padam serta-merta)
        probe_id = f"__ps2n_exp02_probe_{idx}__"
        try:
            if dim > 0:
                dummy_vec = [0.001] * dim
                v_index.upsert(vectors=[{"id": probe_id, "vector": dummy_vec, "metadata": {"diag": True}}])
            else:
                v_index.upsert(vectors=[{"id": probe_id, "data": "ps2 retro nostalgia probe", "metadata": {"diag": True}}])
        except Exception:
            # Sandaran jika indeks dikonfigurasi khusus untuk input teks (hosted embedding)
            v_index.upsert(vectors=[{"id": probe_id, "data": "ps2 retro nostalgia probe", "metadata": {"diag": True}}])

        v_index.delete(ids=[probe_id])
        res["rw_ok"] = True

    except Exception as exc:
        res["error"] = str(exc)
    finally:
        res["latency_ms"] = (time.perf_counter() - t0) * 1000

    return res


def main() -> None:
    now_myt = datetime.now(tz=MYT).strftime("%Y-%m-%d %H:%M:%S MYT")
    console.print(
        Panel.fit(
            "[bold cyan]PS2-RETRO-NOSTALGIA :: CLOUD INFRASTRUCTURE & B2 PROXY DIAGNOSTIC (EXP02)[/bold cyan]\n"
            f"[dim]Sumber Konfigurasi: {ENV_LOCAL_FILE} | Masa: {now_myt}[/dim]",
            border_style="cyan",
        )
    )

    if not ENV_LOCAL_FILE.exists():
        console.print(f"[bold red]❌ Fail .env.local tidak ditemui di {ENV_LOCAL_FILE}[/bold red]")
        sys.exit(1)

    proxy_url, proxy_secret, b2_accs, redis_accs, vector_accs = load_all_configs(ENV_LOCAL_FILE)

    # --------------------------------------------------------------------------
    # 1. AUDIT RINGKAS KONFIGURASI .env.local & CLOUDFLARE WORKER
    # --------------------------------------------------------------------------
    cfg_table = Table(title="1. Audit Kunci Persekitaran & Cloudflare Worker", box=box.ROUNDED, header_style="bold magenta")
    cfg_table.add_column("Komponen", style="bold white")
    cfg_table.add_column("Butiran / Nilai Bertopeng", style="cyan")
    cfg_table.add_column("Bilangan", justify="center")
    cfg_table.add_column("Status", justify="center")

    cfg_table.add_row(
        "CF_WORKER_B2_PROXY_STORAGE",
        proxy_url or "-",
        "1 Endpoint",
        "[bold green]ADA[/bold green]" if proxy_url else "[bold red]TIADA[/bold red]",
    )
    cfg_table.add_row(
        "PS2N_PROXY_SECRET",
        mask_secret(proxy_secret, 10, 6),
        f"{len(proxy_secret)} aksara",
        "[bold green]ADA[/bold green]" if proxy_secret else "[bold red]TIADA[/bold red]",
    )
    cfg_table.add_row(
        "Backblaze B2 Accounts",
        f"{b2_accs[0]['bucket_name']} .. {b2_accs[-1]['bucket_name']}" if b2_accs else "-",
        f"{len(b2_accs)} Akaun",
        "[bold green]5/5 LENGKAP[/bold green]" if len(b2_accs) == 5 else f"[bold yellow]{len(b2_accs)} Akaun[/bold yellow]",
    )
    cfg_table.add_row(
        "Upstash Redis Shards",
        f"{len(redis_accs)} REST Endpoints",
        f"{len(redis_accs)} Akaun",
        "[bold green]5/5 LENGKAP[/bold green]" if len(redis_accs) == 5 else f"[bold yellow]{len(redis_accs)} Akaun[/bold yellow]",
    )
    cfg_table.add_row(
        "Upstash Vector Indexes",
        f"{len(vector_accs)} REST Endpoints",
        f"{len(vector_accs)} Akaun",
        "[bold green]5/5 LENGKAP[/bold green]" if len(vector_accs) == 5 else f"[bold yellow]{len(vector_accs)} Akaun[/bold yellow]",
    )
    console.print(cfg_table)

    if not proxy_url or not proxy_secret or not b2_accs or not redis_accs or not vector_accs:
        console.print("[bold red]❌ Sebahagian kunci wajib masih tiada di .env.local. Proses dihentikan.[/bold red]")
        sys.exit(1)

    all_passed = True

    # --------------------------------------------------------------------------
    # 2. SEMAKAN KESIHATAN CLOUDFLARE WORKER (/health)
    # --------------------------------------------------------------------------
    with console.status("[bold cyan]Menghubungi Cloudflare Worker /health...[/bold cyan]"):
        worker_ok, worker_info = check_cf_worker_health(proxy_url)

    if worker_ok:
        console.print(
            Panel(
                f"[bold green]Worker Aktif ({worker_info['latency']:.0f} ms):[/bold green] {proxy_url}\n"
                f"Baldi B2 Dimuatkan: [bold cyan]{worker_info.get('buckets_loaded')}[/bold cyan] | "
                f"PS2N_PROXY_SECRET: [bold green]Dikonfigurasi[/bold green]",
                title="2. Pengesahan Cloudflare Worker Proxy",
                border_style="green",
            )
        )
    else:
        all_passed = False
        console.print(
            Panel(
                f"[bold red]Ralat Cloudflare Worker:[/bold red] {worker_info}\n"
                "Pastikan B2_ACCOUNTS_JSON dan PS2N_PROXY_SECRET telah disimpan di Cloudflare Env.",
                title="2. Ralat Cloudflare Worker Proxy",
                border_style="red",
            )
        )
        sys.exit(1)

    # --------------------------------------------------------------------------
    # 3. UJIAN PENUH 5 AKAUN BACKBLAZE B2 MELALUI CLOUDFLARE PROXY
    # --------------------------------------------------------------------------
    b2_results = []
    with console.status("[bold cyan]Menjalankan ujian kitaran penuh (Auth + Proxy PUT/GET/DELETE) pada 5 akaun B2...[/bold cyan]"):
        for acc in b2_accs:
            b2_results.append(test_single_b2_full_cycle(acc, proxy_url, proxy_secret))

    b2_table = Table(
        title="3. Ujian Kitaran Penuh 5 Akaun Backblaze B2 via Cloudflare Proxy",
        box=box.ROUNDED,
        header_style="bold green",
    )
    b2_table.add_column("Akaun", justify="center", style="cyan")
    b2_table.add_column("Nama Baldi", style="white")
    b2_table.add_column("Storan Semasa", justify="right", style="yellow")
    b2_table.add_column("PUT (Upload)", justify="center")
    b2_table.add_column("GET (Verify)", justify="center")
    b2_table.add_column("DELETE (Clean)", justify="center")
    b2_table.add_column("Latensi", justify="right", style="dim")
    b2_table.add_column("Status", justify="center")

    total_b2_bytes = 0
    total_b2_files = 0
    for r in b2_results:
        total_b2_bytes += r["bytes"]
        total_b2_files += r["files"]
        cycle_ok = r["direct_ok"] and r["put_ok"] and r["get_ok"] and r["del_ok"]
        if not cycle_ok:
            all_passed = False

        b2_table.add_row(
            f"#{r['index']:03d}",
            r["bucket_name"],
            f"{format_size(r['bytes'])} ({r['files']} fail)",
            "[bold green]OK[/bold green]" if r["put_ok"] else "[bold red]GAGAL[/bold red]",
            "[bold green]OK[/bold green]" if r["get_ok"] else "[bold red]GAGAL[/bold red]",
            "[bold green]OK[/bold green]" if r["del_ok"] else "[bold red]GAGAL[/bold red]",
            f"{r['latency_ms']:.0f} ms",
            "[bold green]LULUS 100%[/bold green]" if cycle_ok else f"[bold red]{r['error'][:28]}[/bold red]",
        )
    console.print(b2_table)

    # --------------------------------------------------------------------------
    # 4. UJIAN PENUH 5 SHARD UPSTASH REDIS
    # --------------------------------------------------------------------------
    redis_results = []
    with console.status("[bold cyan]Menguji sambungan & baca/tulis pada 5 shard Upstash Redis...[/bold cyan]"):
        for acc in redis_accs:
            redis_results.append(test_single_redis(acc))

    redis_table = Table(
        title="4. Ujian Sambungan & Baca/Tulis 5 Akaun Upstash Redis",
        box=box.ROUNDED,
        header_style="bold cyan",
    )
    redis_table.add_column("Shard", justify="center", style="cyan")
    redis_table.add_column("Host Upstash", style="white")
    redis_table.add_column("Kunci (DBSIZE)", justify="right", style="yellow")
    redis_table.add_column("RAM Digunakan", justify="right", style="white")
    redis_table.add_column("Jumlah Arahan", justify="right", style="dim")
    redis_table.add_column("SET/GET/DEL", justify="center")
    redis_table.add_column("Latensi", justify="right", style="dim")
    redis_table.add_column("Status", justify="center")

    for r in redis_results:
        ok = r["online"] and r["rw_ok"]
        if not ok:
            all_passed = False
        redis_table.add_row(
            f"#{r['index']:03d}",
            r["host"],
            f"{r['keys']:,}",
            f"{r['mem_str']} ({r['mem_pct']:.1f}%)",
            f"{r['total_cmds']:,}",
            "[bold green]OK[/bold green]" if r["rw_ok"] else "[bold red]GAGAL[/bold red]",
            f"{r['latency_ms']:.0f} ms",
            "[bold green]ONLINE[/bold green]" if ok else f"[bold red]{r['error'][:25]}[/bold red]",
        )
    console.print(redis_table)

    # --------------------------------------------------------------------------
    # 5. UJIAN PENUH 5 INDEKS UPSTASH VECTOR
    # --------------------------------------------------------------------------
    vector_results = []
    with console.status("[bold cyan]Menguji indeks semantik pada 5 akaun Upstash Vector...[/bold cyan]"):
        for acc in vector_accs:
            vector_results.append(test_single_vector(acc))

    vec_table = Table(
        title="5. Ujian Semantik & Upsert/Delete 5 Akaun Upstash Vector",
        box=box.ROUNDED,
        header_style="bold yellow",
    )
    vec_table.add_column("Indeks", justify="center", style="cyan")
    vec_table.add_column("Host Upstash Vector", style="white")
    vec_table.add_column("Dimensi", justify="right", style="yellow")
    vec_table.add_column("Metrik", justify="center", style="magenta")
    vec_table.add_column("Bil. Vektor", justify="right", style="white")
    vec_table.add_column("UPSERT/DEL", justify="center")
    vec_table.add_column("Latensi", justify="right", style="dim")
    vec_table.add_column("Status", justify="center")

    for r in vector_results:
        ok = r["online"] and r["rw_ok"]
        if not ok:
            all_passed = False
        vec_table.add_row(
            f"#{r['index']:03d}",
            r["host"],
            str(r["dimension"]) if r["dimension"] else "Auto/Text",
            r["metric"],
            f"{r['vector_count']:,}",
            "[bold green]OK[/bold green]" if r["rw_ok"] else "[bold red]GAGAL[/bold red]",
            f"{r['latency_ms']:.0f} ms",
            "[bold green]ONLINE[/bold green]" if ok else f"[bold red]{r['error'][:25]}[/bold red]",
        )
    console.print(vec_table)

    # --------------------------------------------------------------------------
    # 6. RINGKASAN AGREGAT & STATUS AKHIR
    # --------------------------------------------------------------------------
    total_safe_b2 = len(b2_accs) * B2_SAFE_LIMIT_BYTES
    total_max_b2 = len(b2_accs) * B2_CEILING_LIMIT_BYTES

    if all_passed:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold green]SEMUA 15 AKAUN AWAN & CLOUDFLARE B2 PROXY BERFUNGSI 100%![/bold green]\n"
                    f"• Backblaze B2 (5 Baldi): {format_size(total_b2_bytes)} / {format_size(total_safe_b2)} had selamat "
                    f"({format_size(total_max_b2)} maks percuma) — PUT/GET/DELETE via Worker lulus.\n"
                    f"• Upstash Redis (5 Shard): 50,000 arahan/hari & 1.28 GB RAM gabungan aktif.\n"
                    f"• Upstash Vector (5 Indeks): Carian & penulisan semantik anti-duplikasi aktif.\n"
                    "[dim]Sedia untuk membina modul pengeluaran di live-engine-01 (maksimum 2 fail setiap sesi).[/dim]"
                ),
                title="Status Akhir Diagnostik Infrastruktur",
                border_style="bold green",
            )
        )
    else:
        console.print(
            Panel(
                Text.from_markup(
                    "[bold red]DIAGNOSTIK MENGESAN RALAT PADA SALAH SATU SERVIS DI ATAS[/bold red]\n"
                    "Sila semak lajur bertanda merah pada jadual di atas sebelum meneruskan ke live-engine-01."
                ),
                title="Status Akhir Diagnostik Infrastruktur",
                border_style="bold red",
            )
        )


if __name__ == "__main__":
    main()