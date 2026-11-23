#!/usr/bin/env python3
"""EnderHub Telegram Docker Controller (zero dipendenze esterne, solo stdlib Python)."""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime
import fcntl
import html
import http.client
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import ssl
import subprocess
import sys
import threading
import time
import traceback

BASE_DIR_INIT = Path(__file__).resolve().parent
_env_init = BASE_DIR_INIT / ".env"
if _env_init.exists():
    for _ln in _env_init.read_text(encoding="utf-8").splitlines():
        _s = _ln.strip()
        if _s and not _s.startswith("#") and "=" in _s:
            _k, _v = _s.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip().strip('"').strip("'"))

from stacks import CATEGORIES, CATEGORY_BY_ID, STACK_BY_ID, STACKS, Stack, stacks_in_category


BASE_DIR = Path(__file__).resolve().parent
STATE_DIR = BASE_DIR / "state"
STOPPED_DIR = STATE_DIR / "stopped"
BOOT_ID_FILE = STATE_DIR / "last_boot_id"
POWER_STATE_FILE = STATE_DIR / "last_power_counters.json"
LOCK_FILE = STATE_DIR / "bot.lock"
ENV_FILE = BASE_DIR / ".env"
SDA_ATTRLOG_GLOB = os.environ.get("SMART_ATTRLOG_GLOB", "/var/lib/smartmontools/attrlog.*.csv")

TELEGRAM_HOST = "api.telegram.org"
MAX_TEXT_LEN = 3900

STOP_EVENT = threading.Event()
EXECUTOR = ThreadPoolExecutor(max_workers=4, thread_name_prefix="enderhub-bot")
STACK_LOCKS: dict[str, threading.Lock] = {s.id: threading.Lock() for s in STACKS}
CPU_CACHE_LOCK = threading.Lock()
# cid -> (last_ts, last_usage_usec, last_cpu_pct)
CPU_SAMPLES: dict[str, tuple[float, int, float]] = {}
LOCK_FD = None


@dataclass(frozen=True)
class ContainerRuntime:
    cid: str
    state: str
    status: str


def submit_safe(fn, *args) -> None:
    """Esegue un task nel pool registrando eventuali eccezioni impreviste."""

    def _runner() -> None:
        try:
            fn(*args)
        except Exception:
            traceback.print_exc(file=sys.stderr)
            sys.stderr.flush()

    EXECUTOR.submit(_runner)


def load_env(path: Path) -> dict[str, str]:
    """Carica variabili chiave=valore dal file .env locale."""
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        env[key.strip()] = val.strip().strip('"').strip("'")
    return env


def run_cmd(
    args: list[str], cwd: str | None = None, timeout: int = 30
) -> tuple[int, str, str]:
    """Esegue un comando senza shell e restituisce (returncode, stdout, stderr)."""
    try:
        proc = subprocess.run(
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return proc.returncode, proc.stdout.strip(), proc.stderr.strip()
    except subprocess.TimeoutExpired:
        return 124, "", f"Timeout dopo {timeout}s"
    except Exception as exc:
        return 1, "", str(exc)


class IPv4HTTPSConnection(http.client.HTTPSConnection):
    """Connessione HTTPS forzata su IPv4 con TCP_NODELAY per ridurre la latenza."""

    def connect(self) -> None:
        infos = socket.getaddrinfo(
            self.host, self.port, socket.AF_INET, socket.SOCK_STREAM
        )
        last_err: OSError | None = None
        for _, socktype, proto, _, sa in infos:
            sock = None
            try:
                sock = socket.socket(socket.AF_INET, socktype, proto)
                sock.settimeout(self.timeout)
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
                sock.connect(sa)
                self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
                return
            except OSError as err:
                last_err = err
                if sock is not None:
                    sock.close()
        if last_err is not None:
            raise last_err


class TelegramClient:
    """Client HTTP/HTTPS persistente (keep-alive per thread) per Telegram Bot API."""

    def __init__(self, token: str) -> None:
        self.base_path = f"/bot{token}"
        self._tls = threading.local()
        self._ssl_ctx = ssl.create_default_context()

    def _get_conn(self, timeout: int) -> IPv4HTTPSConnection:
        conn: IPv4HTTPSConnection | None = getattr(self._tls, "conn", None)
        if conn is not None and conn.timeout != timeout:
            try:
                conn.close()
            except Exception:
                pass
            conn = None
        if conn is None:
            conn = IPv4HTTPSConnection(
                TELEGRAM_HOST, timeout=timeout, context=self._ssl_ctx
            )
            self._tls.conn = conn
        return conn

    def _reset_conn(self) -> None:
        conn: IPv4HTTPSConnection | None = getattr(self._tls, "conn", None)
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass
            self._tls.conn = None

    def call(
        self, method: str, payload: dict | None = None, timeout: int = 12
    ) -> dict:
        body = json.dumps(payload or {}).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Connection": "keep-alive",
        }
        attempts = 2 if method != "getUpdates" else 1
        last_exc: Exception | None = None

        for _ in range(attempts):
            conn = self._get_conn(timeout)
            try:
                conn.request("POST", f"{self.base_path}/{method}", body, headers)
                resp = conn.getresponse()
                raw = resp.read().decode("utf-8", errors="replace")
                if not raw:
                    return {"ok": False, "description": f"HTTP {resp.status}"}
                return json.loads(raw)
            except (
                http.client.RemoteDisconnected,
                http.client.BadStatusLine,
                ConnectionResetError,
                BrokenPipeError,
            ) as exc:
                last_exc = exc
                self._reset_conn()
            except (TimeoutError, socket.timeout, OSError):
                self._reset_conn()
                raise

        if last_exc is not None:
            raise last_exc
        return {"ok": False, "description": "Connessione chiusa"}

    def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        payload: dict = {
            "chat_id": chat_id,
            "text": text[:MAX_TEXT_LEN],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        return self.call("sendMessage", payload)

    def edit_message(
        self,
        chat_id: int,
        message_id: int,
        text: str,
        reply_markup: dict | None = None,
        parse_mode: str = "HTML",
    ) -> dict:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text[:MAX_TEXT_LEN],
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        res = self.call("editMessageText", payload)
        desc = str(res.get("description", ""))
        if not res.get("ok") and "message is not modified" in desc:
            return {"ok": True}
        return res

    def answer_callback(
        self, callback_id: str, text: str = "", show_alert: bool = False
    ) -> None:
        payload: dict = {"callback_query_id": callback_id, "show_alert": show_alert}
        if text:
            payload["text"] = text[:190]
        self.call("answerCallbackQuery", payload)

    def set_commands(self) -> None:
        commands = [
            {"command": "menu", "description": "Pannello principale EnderHub"},
            {"command": "stato", "description": "Stato hardware e dischi server"},
            {"command": "utility", "description": "Utility & Cloud (Nextcloud, Grafana...)"},
            {"command": "ai", "description": "AI Stack & KaraScore ML"},
            {"command": "nodi", "description": "Nodi passivi (Grass & Bitping)"},
            {"command": "gaming", "description": "Gaming & Server (PokéMMO, MC, Ninja)"},
        ]
        self.call("setMyCommands", {"commands": commands})


def get_docker_snapshot() -> dict[str, ContainerRuntime]:
    """Ottiene ID, stato e uptime di tutti i container Docker con una sola chiamata (~17ms)."""
    rc, out, _ = run_cmd(
        [
            "docker",
            "ps",
            "-a",
            "--no-trunc",
            "--format",
            "{{.ID}}\t{{.Names}}\t{{.State}}\t{{.Status}}",
        ],
        timeout=10,
    )
    snapshot: dict[str, ContainerRuntime] = {}
    if rc != 0 or not out:
        return snapshot
    for line in out.splitlines():
        parts = line.split("\t", 3)
        if len(parts) == 4:
            cid, name, state, status = parts
            snapshot[name] = ContainerRuntime(cid=cid, state=state, status=status)
    return snapshot


def _read_cgroup_usage_usec(cg_dir: Path) -> int | None:
    cpu_stat = cg_dir / "cpu.stat"
    if not cpu_stat.exists():
        return None
    for line in cpu_stat.read_text(encoding="utf-8").splitlines():
        if line.startswith("usage_usec "):
            return int(line.split()[1])
    return None


def get_docker_stats_map(snapshot: dict[str, ContainerRuntime] | None = None) -> dict[str, str]:
    """Legge RAM e CPU live dai cgroup v2 in <1ms (zero attesa 'docker stats')."""
    if snapshot is None:
        snapshot = get_docker_snapshot()

    now = time.monotonic()
    stats: dict[str, str] = {}
    for name, rt in snapshot.items():
        if rt.state != "running":
            continue
        cg_dir = Path(f"/sys/fs/cgroup/system.slice/docker-{rt.cid}.scope")
        mem_cur_file = cg_dir / "memory.current"
        if not mem_cur_file.exists():
            continue
        try:
            mem_cur = int(mem_cur_file.read_text(encoding="utf-8").strip())
            inactive_file = 0
            mem_stat_file = cg_dir / "memory.stat"
            if mem_stat_file.exists():
                for line in mem_stat_file.read_text(encoding="utf-8").splitlines():
                    if line.startswith("inactive_file "):
                        inactive_file = int(line.split()[1])
                        break
            rss_bytes = max(0, mem_cur - inactive_file)
            if rss_bytes >= 1024**3:
                mem_str = f"{rss_bytes / (1024**3):.2f}GiB"
            else:
                mem_str = f"{rss_bytes / (1024**2):.1f}MiB"

            cpu_str = ""
            usage_usec = _read_cgroup_usage_usec(cg_dir)
            if usage_usec is not None:
                with CPU_CACHE_LOCK:
                    prev = CPU_SAMPLES.get(rt.cid)
                    if prev is None:
                        CPU_SAMPLES[rt.cid] = (now, usage_usec, 0.0)
                    else:
                        prev_ts, prev_use, prev_pct = prev
                        dt = now - prev_ts
                        if dt >= 0.5:
                            pct = max(0.0, ((usage_usec - prev_use) / 1_000_000.0) / dt * 100.0)
                            CPU_SAMPLES[rt.cid] = (now, usage_usec, pct)
                            cpu_str = f"{pct:.2f}% | "
                        elif dt >= 0 and prev_pct > 0:
                            cpu_str = f"{prev_pct:.2f}% | "
            stats[name] = f"{cpu_str}{mem_str}"
        except Exception:
            continue
    return stats


def stack_state(
    stack: Stack, snapshot: dict[str, ContainerRuntime]
) -> tuple[str, int, int]:
    """Restituisce (icona_stato, container_attivi, container_totali)."""
    total = len(stack.containers)
    running = sum(
        1
        for cname in stack.containers
        if cname in snapshot and snapshot[cname].state == "running"
    )
    if running == total:
        return "🟢", running, total
    if running > 0:
        return "🟡", running, total
    return "⚪", 0, total


def _format_uptime() -> str:
    try:
        secs = int(float(Path("/proc/uptime").read_text(encoding="utf-8").split()[0]))
        days, rem = divmod(secs, 86400)
        hours, rem = divmod(rem, 3600)
        mins = rem // 60
        parts = []
        if days:
            parts.append(f"{days}g")
        if hours or days:
            parts.append(f"{hours}h")
        parts.append(f"{mins}m")
        return " ".join(parts)
    except Exception:
        return "n/d"


def read_host_metrics(include_gpu: bool = True, include_disks: bool = True) -> dict[str, str]:
    """Raccoglie metriche hardware essenziali di EnderHub in poche decine di ms."""
    metrics: dict[str, str] = {"uptime": _format_uptime()}

    load1, load5, _ = os.getloadavg()
    metrics["load"] = f"{load1:.2f}, {load5:.2f}"

    try:
        meminfo: dict[str, int] = {}
        for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                meminfo[parts[0].rstrip(":")] = int(parts[1])
        total_gb = meminfo.get("MemTotal", 0) / (1024 * 1024)
        avail_gb = meminfo.get("MemAvailable", 0) / (1024 * 1024)
        used_gb = max(0.0, total_gb - avail_gb)
        metrics["ram"] = f"{used_gb:.1f} / {total_gb:.1f} GiB ({avail_gb:.1f} GiB liberi)"
    except Exception:
        metrics["ram"] = "n/d"

    cpu_temp = "n/d"
    try:
        for tz in sorted(Path("/sys/class/thermal").glob("thermal_zone*")):
            t_type = (tz / "type").read_text(encoding="utf-8").strip()
            if "pkg" in t_type or "x86" in t_type or cpu_temp == "n/d":
                raw_temp = int((tz / "temp").read_text(encoding="utf-8").strip())
                cpu_temp = f"{raw_temp / 1000:.0f}°C"
                if "pkg" in t_type or "x86" in t_type:
                    break
    except Exception:
        pass
    metrics["cpu_temp"] = cpu_temp

    if include_gpu:
        rc, gpu_out, _ = run_cmd(
            [
                "nvidia-smi",
                "--query-gpu=temperature.gpu,utilization.gpu,memory.used,memory.total,power.draw",
                "--format=csv,noheader,nounits",
            ],
            timeout=5,
        )
        if rc == 0 and gpu_out:
            cols = [c.strip() for c in gpu_out.splitlines()[0].split(",")]
            if len(cols) >= 5:
                metrics["gpu"] = (
                    f"{cols[0]}°C | Uso {cols[1]}% | VRAM {cols[2]}/{cols[3]} MiB | {cols[4]} W"
                )
            else:
                metrics["gpu"] = gpu_out
        else:
            metrics["gpu"] = "n/d"
    else:
        metrics["gpu"] = "n/d"

    if include_disks:
        disk_lines: list[str] = []
        for mp, label in [("/", "/ (Sistema)"), ("/mnt", "/mnt (Dati)")]:
            try:
                usage = shutil.disk_usage(mp)
                used_g = usage.used / (1024**3)
                tot_g = usage.total / (1024**3)
                free_g = usage.free / (1024**3)
                pct = (usage.used / usage.total) * 100
                disk_lines.append(
                    f"• <b>{label}</b>: {used_g:.0f}/{tot_g:.0f} GB ({pct:.0f}% — {free_g:.0f} GB liberi)"
                )
            except Exception:
                pass
        metrics["disks"] = "\n".join(disk_lines) if disk_lines else "n/d"

    return metrics


def read_smart_power_counters() -> tuple[int, int] | None:
    """Legge Power_Cycle_Count (ID 12) e Unsafe_Shutdown_Count (ID 192) di /dev/sda dall'ultimo CSV smartd."""
    try:
        csv_files = sorted(
            Path("/var/lib/smartmontools").glob(os.environ.get("SMART_ATTRLOG_GLOB", "attrlog.*.csv").split("/")[-1])
        )
        if not csv_files:
            return None
        path = csv_files[0]
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - 4096), os.SEEK_SET)
            tail = fh.read().replace(b"\x00", b"").decode("utf-8", errors="ignore")
        lines = [ln.strip() for ln in tail.splitlines() if ln.strip()]
        if not lines:
            return None
        cols = [c.strip() for c in lines[-1].split(";") if c.strip()]
        attrs = {cols[i]: cols[i + 2] for i in range(1, len(cols) - 2, 3)}
        if "12" in attrs and "192" in attrs:
            return int(attrs["12"]), int(attrs["192"])
    except Exception:
        return None
    return None


def read_mobo_voltages() -> str:
    """Legge le tensioni principali dal chip SuperIO nct6796d/nct6775 in <1ms."""
    try:
        for hwmon_dir in sorted(Path("/sys/devices/platform/nct6775.656/hwmon").glob("hwmon*")):
            parts: list[str] = []
            for fname, label in [
                ("in3_input", "+3.3V"),
                ("in7_input", "3VSB"),
                ("in8_input", "Vbat"),
                ("in0_input", "Vcore"),
            ]:
                fpath = hwmon_dir / fname
                if fpath.exists():
                    mv = int(fpath.read_text(encoding="utf-8").strip())
                    parts.append(f"{label} {mv / 1000:.2f}V")
            if parts:
                return " | ".join(parts)
    except Exception:
        pass
    return "n/d"


def read_ras_status() -> str:
    """Verifica lo stato di rasdaemon ed eventuali errori MCE/AER/RAM nel boot corrente."""
    rc_act, act_out, _ = run_cmd(["systemctl", "is-active", "rasdaemon"], timeout=3)
    if rc_act != 0 or act_out != "active":
        return f"inattivo ({act_out or 'n/d'})"
    rc_log, log_out, _ = run_cmd(
        ["journalctl", "-b", "0", "-u", "rasdaemon", "--no-pager", "-q", "-o", "cat"],
        timeout=3,
    )
    if rc_log != 0:
        return "attivo (log n/d)"
    ignore_tokens = (
        "enabled",
        "disabled",
        "listening to events",
        "recording ",
        "only decoding",
        "starting rasdaemon",
        "started rasdaemon",
        "stopping rasdaemon",
        "stopped rasdaemon",
        "deactivated",
        "trace_clock",
        "timestamp for tracing",
        "calling ras_mc_event_",
        "signal=",
        "register inserted at db",
        "aborting",
        "huh!",
    )
    err_lines = [
        ln for ln in log_out.splitlines()
        if ln.strip() and not any(tok in ln.lower() for tok in ignore_tokens)
    ]
    return f"attivo ({len(err_lines)} errori HW)"


def build_home_view() -> tuple[str, dict]:
    """Costruisce testo e tastiera inline della schermata Home."""
    snapshot = get_docker_snapshot()
    # Aggiorna il campionamento CPU in background senza bloccare
    get_docker_stats_map(snapshot)
    metrics = read_host_metrics(include_gpu=True, include_disks=False)
    total_running = sum(1 for c in snapshot.values() if c.state == "running")

    cat_summary_lines: list[str] = []
    keyboard: list[list[dict]] = []
    row: list[dict] = []

    for cat in CATEGORIES:
        cat_stacks = stacks_in_category(cat.id)
        active_stacks = sum(
            1 for s in cat_stacks if stack_state(s, snapshot)[1] == len(s.containers)
        )
        cat_summary_lines.append(
            f"{cat.label}: <b>{active_stacks}/{len(cat_stacks)}</b> attivi"
        )
        btn = {
            "text": f"{cat.label} ({active_stacks}/{len(cat_stacks)})",
            "callback_data": f"cat:{cat.id}",
        }
        row.append(btn)
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append(
        [
            {"text": "📊 Stato Server", "callback_data": "sys:status"},
            {"text": "🔄 Aggiorna", "callback_data": "home"},
        ]
    )

    now_str = datetime.now().strftime("%H:%M:%S")
    text = (
        f"🖥️ <b>EnderHub Control Center</b> <i>({now_str})</i>\n"
        f"⏱️ Uptime: <code>{html.escape(metrics['uptime'])}</code>\n"
        f"🧠 RAM: <code>{html.escape(metrics['ram'])}</code>\n"
        f"🌡️ CPU: <code>{html.escape(metrics['cpu_temp'])}</code> | GPU: <code>{html.escape(metrics['gpu'].split('|')[0].strip())}</code>\n"
        f"🐳 Container attivi: <b>{total_running}</b>\n\n"
        + "\n".join(cat_summary_lines)
        + "\n\nScegli una categoria:"
    )
    return text, {"inline_keyboard": keyboard}


def build_category_view(cat_id: str) -> tuple[str, dict]:
    """Costruisce il menu di una categoria con tutti gli stack (attivi e spenti)."""
    cat = CATEGORY_BY_ID[cat_id]
    snapshot = get_docker_snapshot()
    cat_stacks = stacks_in_category(cat_id)

    lines = [f"<b>{html.escape(cat.label)}</b>", f"<i>{html.escape(cat.description)}</i>\n"]
    keyboard: list[list[dict]] = []

    for s in cat_stacks:
        icon, run_cnt, tot_cnt = stack_state(s, snapshot)
        stopped_flag = (STOPPED_DIR / s.id).exists()
        suffix = " (spento via bot)" if stopped_flag and run_cnt == 0 else ""
        lines.append(
            f"{icon} <b>{html.escape(s.name)}</b> — {run_cnt}/{tot_cnt}{suffix}"
        )
        action_cb = f"act:stop:{s.id}" if run_cnt > 0 else f"act:start:{s.id}"
        action_txt = "⏹️ Ferma" if run_cnt > 0 else "▶️ Avvia"
        keyboard.append(
            [
                {"text": f"{icon} {s.name}", "callback_data": f"svc:{s.id}"},
                {"text": action_txt, "callback_data": action_cb},
            ]
        )

    if cat_id == "ai":
        keyboard.append(
            [{"text": "⚽ Stato KaraScore ML & Modelli", "callback_data": "ai:info"}]
        )
    elif cat_id == "gaming":
        keyboard.append(
            [{"text": "🛑 Spegni TUTTI i Gaming", "callback_data": "act:stop_gaming"}]
        )

    keyboard.append(
        [
            {"text": "🔄 Aggiorna", "callback_data": f"cat:{cat_id}"},
            {"text": "🏠 Home", "callback_data": "home"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": keyboard}


def build_service_view(stack_id: str, banner: str = "") -> tuple[str, dict]:
    """Costruisce la scheda di dettaglio per un singolo stack Docker."""
    s = STACK_BY_ID[stack_id]
    snapshot = get_docker_snapshot()
    stats_map = get_docker_stats_map(snapshot)
    icon, run_cnt, tot_cnt = stack_state(s, snapshot)

    lines: list[str] = []
    if banner:
        lines.append(f"{banner}\n")

    lines.extend(
        [
            f"{icon} <b>{html.escape(s.name)}</b>",
            f"📝 {html.escape(s.description)}",
            f"🌐 Endpoint: <code>{html.escape(s.endpoint)}</code>",
            f"📁 Path: <code>{html.escape(s.path)}</code>",
        ]
    )
    if s.ram_note:
        lines.append(f"⚠️ Nota: <i>{html.escape(s.ram_note)}</i>")

    lines.append("\n<b>Container:</b>")
    for cname in s.containers:
        rt = snapshot.get(cname)
        if rt is None:
            lines.append(f"• <code>{html.escape(cname)}</code>: ⚫ non creato")
        elif rt.state == "running":
            stat_txt = stats_map.get(cname, "")
            extra = f" | <code>{html.escape(stat_txt)}</code>" if stat_txt else ""
            lines.append(
                f"• <code>{html.escape(cname)}</code>: 🟢 {html.escape(rt.status)}{extra}"
            )
        else:
            lines.append(
                f"• <code>{html.escape(cname)}</code>: ⚪ {html.escape(rt.status)}"
            )

    keyboard: list[list[dict]] = []
    if run_cnt == 0:
        keyboard.append([{"text": "▶️ Avvia", "callback_data": f"svcact:start:{s.id}"}])
    elif run_cnt < tot_cnt:
        keyboard.append(
            [
                {"text": "▶️ Completa Avvio", "callback_data": f"svcact:start:{s.id}"},
                {"text": "⏹️ Ferma", "callback_data": f"svcact:stop:{s.id}"},
            ]
        )
    else:
        keyboard.append(
            [
                {"text": "🔄 Riavvia", "callback_data": f"svcact:restart:{s.id}"},
                {"text": "⏹️ Ferma", "callback_data": f"svcact:stop:{s.id}"},
            ]
        )

    keyboard.append(
        [
            {"text": "📜 Log (25 righe)", "callback_data": f"svclogs:{s.id}"},
            {"text": "🔄 Aggiorna", "callback_data": f"svc:{s.id}"},
        ]
    )
    keyboard.append(
        [
            {"text": "⬅️ Categoria", "callback_data": f"cat:{s.category}"},
            {"text": "🏠 Home", "callback_data": "home"},
        ]
    )
    return "\n".join(lines), {"inline_keyboard": keyboard}


def build_server_status_view(banner: str = "") -> tuple[str, dict]:
    """Costruisce la scheda completa di salute hardware e container attivi."""
    snapshot = get_docker_snapshot()
    stats_map = get_docker_stats_map(snapshot)
    metrics = read_host_metrics(include_gpu=True, include_disks=True)

    lines: list[str] = []
    if banner:
        lines.append(f"{banner}\n")

    lines.extend(
        [
            f"📊 <b>Stato Hardware — EnderHub</b> <i>({datetime.now().strftime('%H:%M:%S')})</i>\n",
            f"⏱️ <b>Uptime</b>: <code>{html.escape(metrics['uptime'])}</code>",
            f"⚙️ <b>Load CPU</b>: <code>{html.escape(metrics['load'])}</code> | Temp: <code>{html.escape(metrics['cpu_temp'])}</code>",
            f"🧠 <b>RAM</b>: <code>{html.escape(metrics['ram'])}</code>",
            f"🎮 <b>GTX 1650</b>: <code>{html.escape(metrics['gpu'])}</code>\n",
            "<b>💾 Dischi:</b>",
            metrics["disks"],
        ]
    )
    voltages = read_mobo_voltages()
    ras_status = read_ras_status()
    smart_counters = read_smart_power_counters()
    hw_lines = [
        f"\n🔌 <b>Tensioni MobO</b>: <code>{html.escape(voltages)}</code>",
        f"🛡️ <b>Hardware Errors</b>: <code>{html.escape(ras_status)}</code>",
    ]
    if smart_counters is not None:
        cycles, unsafe = smart_counters
        hw_lines.append(
            f"⚡ <b>SSD SMART</b>: <code>Power Cycles {cycles}</code> | <code>Unsafe Shutdown {unsafe}</code>"
        )
    lines.extend(hw_lines)

    if stats_map:
        lines.append("\n<b>🐳 Consumi Container Attivi:</b>")
        for cname in sorted(stats_map):
            lines.append(
                f"• <code>{html.escape(cname)}</code>: <code>{html.escape(stats_map[cname])}</code>"
            )

    keyboard = [
        [
            {"text": "🩹 Self-Heal Porte Tailscale", "callback_data": "sys:heal"},
            {"text": "🔄 Aggiorna", "callback_data": "sys:status"},
        ],
        [{"text": "🏠 Home", "callback_data": "home"}],
    ]
    return "\n".join(lines), {"inline_keyboard": keyboard}


def build_ai_info_view() -> tuple[str, dict]:
    """Mostra modelli Ollama installati e ultimi log di KaraScore ML."""
    snapshot = get_docker_snapshot()
    ollama_up = snapshot.get("ollama") and snapshot["ollama"].state == "running"

    lines = ["🤖 <b>AI Stack & KaraScore ML</b>\n"]
    if ollama_up:
        rc, out, _ = run_cmd(["docker", "exec", "ollama", "ollama", "list"], timeout=10)
        if rc == 0 and out:
            lines.append("<b>📦 Modelli Ollama:</b>")
            lines.append(f"<pre>{html.escape(out[:1000])}</pre>")
    else:
        lines.append("⚪ <b>Ollama</b> è attualmente spento.\n")

    ml_base = Path(os.environ.get("ML_PIPELINE_DIR", "/home/server/ml-pipeline"))
    _, pg_out, _ = run_cmd(["pgrep", "-af", "machineLearningSoccer"], timeout=5)
    lines.append(
        f"⚙️ <b>Job Python attivi ora</b>: {'🟢 Sì' if pg_out else '⚪ Nessuno (in attesa del cron)'}"
    )

    for log_name, label in [
        ("cron_daily_sniper.log", "Daily Sniper (06:00)"),
        ("cron.log", "Calibrazione & xG (Lun/Ven)"),
    ]:
        p = ml_base / log_name
        if p.exists():
            tail = "\n".join(p.read_text(encoding="utf-8", errors="replace").splitlines()[-5:])
            lines.append(f"\n<b>⚽ {label}:</b>")
            lines.append(f"<pre>{html.escape(tail[-700:])}</pre>")

    keyboard = [
        [
            {"text": "🔄 Aggiorna", "callback_data": "ai:info"},
            {"text": "⬅️ AI & ML", "callback_data": "cat:ai"},
            {"text": "🏠 Home", "callback_data": "home"},
        ]
    ]
    return "\n".join(lines), {"inline_keyboard": keyboard}


def find_exclusive_conflict(stack: Stack, snapshot: dict[str, ContainerRuntime]) -> Stack | None:
    """Verifica se un altro stack dello stesso gruppo esclusivo (es. porta 25565) è attivo."""
    if not stack.exclusive_group:
        return None
    for other in STACKS:
        if other.id == stack.id or other.exclusive_group != stack.exclusive_group:
            continue
        _, run_cnt, _ = stack_state(other, snapshot)
        if run_cnt > 0:
            return other
    return None


def execute_stack_action(stack_id: str, action: str) -> tuple[bool, str]:
    """Esegue start/stop/restart sullo stack gestendo lock, flag manuali e conflitti."""
    s = STACK_BY_ID[stack_id]
    lock = STACK_LOCKS[stack_id]
    if not lock.acquire(blocking=False):
        return False, f"⏳ Operazione già in corso su {s.name}."

    try:
        STOPPED_DIR.mkdir(parents=True, exist_ok=True)
        stop_marker = STOPPED_DIR / s.id

        if action == "start":
            snapshot = get_docker_snapshot()
            conflict = find_exclusive_conflict(s, snapshot)
            if conflict is not None:
                return (
                    False,
                    f"⚠️ Porta 25565 già occupata da <b>{html.escape(conflict.name)}</b>! Fermalo prima di avviare {html.escape(s.name)}.",
                )
            stop_marker.unlink(missing_ok=True)
            rc, _, err = run_cmd(["docker", "compose", "up", "-d"], cwd=s.path, timeout=300)
            if rc != 0:
                return False, f"❌ Errore avvio {html.escape(s.name)}: <code>{html.escape(err[-350:])}</code>"
            return True, f"✅ <b>{html.escape(s.name)}</b> avviato!"

        if action == "stop":
            stop_marker.touch()
            rc, _, err = run_cmd(["docker", "compose", "stop"], cwd=s.path, timeout=90)
            if rc != 0:
                return False, f"❌ Errore stop {html.escape(s.name)}: <code>{html.escape(err[-350:])}</code>"
            return True, f"⏹️ <b>{html.escape(s.name)}</b> fermato!"

        if action == "restart":
            stop_marker.unlink(missing_ok=True)
            rc, _, err = run_cmd(["docker", "compose", "restart"], cwd=s.path, timeout=120)
            if rc != 0:
                return False, f"❌ Errore riavvio {html.escape(s.name)}: <code>{html.escape(err[-350:])}</code>"
            return True, f"🔄 <b>{html.escape(s.name)}</b> riavviato!"

        return False, "❌ Azione sconosciuta."
    finally:
        lock.release()


def handle_callback(
    client: TelegramClient,
    allowed_ids: set[int],
    cb: dict,
) -> None:
    """Gestisce il click su un bottone inline con latenza minima."""
    cb_id = cb["id"]
    from_user = cb.get("from", {})
    uid = from_user.get("id")
    msg = cb.get("message") or {}
    chat_id = (msg.get("chat") or {}).get("id")
    msg_id = msg.get("message_id")
    data = cb.get("data", "")

    if uid not in allowed_ids or chat_id is None or msg_id is None:
        client.answer_callback(cb_id, "⛔ Accesso negato.", show_alert=True)
        return

    # Navigazione rapida: costruisce e aggiorna prima il messaggio, poi chiude lo spinner in parallelo
    if data == "home":
        text, kb = build_home_view()
        client.edit_message(chat_id, msg_id, text, kb)
        submit_safe(client.answer_callback, cb_id)
        return

    if data.startswith("cat:"):
        cat_id = data.split(":", 1)[1]
        if cat_id not in CATEGORY_BY_ID:
            client.answer_callback(cb_id, "Categoria non trovata.")
            return
        text, kb = build_category_view(cat_id)
        client.edit_message(chat_id, msg_id, text, kb)
        submit_safe(client.answer_callback, cb_id)
        return

    if data.startswith("svc:"):
        stack_id = data.split(":", 1)[1]
        if stack_id not in STACK_BY_ID:
            client.answer_callback(cb_id, "Servizio non trovato.")
            return
        text, kb = build_service_view(stack_id)
        client.edit_message(chat_id, msg_id, text, kb)
        submit_safe(client.answer_callback, cb_id)
        return

    if data.startswith("svclogs:"):
        stack_id = data.split(":", 1)[1]
        s = STACK_BY_ID.get(stack_id)
        if not s:
            client.answer_callback(cb_id, "Servizio non trovato.")
            return
        rc, out, err = run_cmd(
            ["docker", "compose", "logs", "--no-color", "--tail=25"],
            cwd=s.path,
            timeout=15,
        )
        raw = out or err or "(nessun log disponibile)"
        snippet = raw[-2800:]
        text = (
            f"📜 <b>Log — {html.escape(s.name)}</b>\n\n"
            f"<pre>{html.escape(snippet)}</pre>"
        )
        kb = {
            "inline_keyboard": [
                [
                    {"text": "🔄 Aggiorna Log", "callback_data": f"svclogs:{s.id}"},
                    {"text": "⬅️ Servizio", "callback_data": f"svc:{s.id}"},
                ],
                [{"text": "🏠 Home", "callback_data": "home"}],
            ]
        }
        client.edit_message(chat_id, msg_id, text, kb)
        submit_safe(client.answer_callback, cb_id)
        return

    if data == "sys:status":
        text, kb = build_server_status_view()
        client.edit_message(chat_id, msg_id, text, kb)
        submit_safe(client.answer_callback, cb_id)
        return

    if data == "sys:heal":
        submit_safe(client.answer_callback, cb_id, "🩹 Controllo porte Tailscale...")
        rc, _, err = run_cmd([os.environ.get("SELF_HEAL_SCRIPT", "/home/server/scripts/self_heal_ports.sh")], timeout=60)
        banner = (
            "✅ Controllo porte Tailscale completato."
            if rc == 0
            else f"⚠️ Self-heal exit {rc}: <code>{html.escape(err[:200])}</code>"
        )
        text, kb = build_server_status_view(banner=banner)
        client.edit_message(chat_id, msg_id, text, kb)
        return

    if data == "ai:info":
        text, kb = build_ai_info_view()
        client.edit_message(chat_id, msg_id, text, kb)
        submit_safe(client.answer_callback, cb_id)
        return

    if data == "act:stop_gaming":
        submit_safe(client.answer_callback, cb_id, "🛑 Fermo tutti i server gaming...")
        stopped_names: list[str] = []
        snapshot = get_docker_snapshot()
        for s in stacks_in_category("gaming"):
            _, run_cnt, _ = stack_state(s, snapshot)
            if run_cnt > 0:
                ok, _ = execute_stack_action(s.id, "stop")
                if ok:
                    stopped_names.append(s.name)
        text, kb = build_category_view("gaming")
        banner = (
            f"🛑 Fermati: {', '.join(stopped_names)}\n\n"
            if stopped_names
            else "ℹ️ Nessun container gaming era attivo.\n\n"
        )
        client.edit_message(chat_id, msg_id, banner + text, kb)
        return

    if data.startswith(("act:", "svcact:")):
        prefix, action, stack_id = data.split(":", 2)
        if stack_id not in STACK_BY_ID or action not in {"start", "stop", "restart"}:
            client.answer_callback(cb_id, "Comando non valido.")
            return

        s = STACK_BY_ID[stack_id]
        verb = {"start": "Avvio", "stop": "Arresto", "restart": "Riavvio"}[action]
        submit_safe(client.answer_callback, cb_id, f"⏳ {verb} {s.name}...")

        if action == "start":
            client.edit_message(
                chat_id,
                msg_id,
                f"⏳ <b>Avvio di {html.escape(s.name)} in corso...</b>\n<i>Se è il primo avvio può richiedere 1-2 minuti per immagine/build.</i>",
                {"inline_keyboard": [[{"text": "⏳ Attendere...", "callback_data": f"svc:{s.id}"}]]},
            )

        ok, msg_banner = execute_stack_action(stack_id, action)
        if prefix == "act":
            cat_text, cat_kb = build_category_view(s.category)
            client.edit_message(chat_id, msg_id, f"{msg_banner}\n\n{cat_text}", cat_kb)
        else:
            svc_text, svc_kb = build_service_view(stack_id, banner=msg_banner)
            client.edit_message(chat_id, msg_id, svc_text, svc_kb)
        return

    submit_safe(client.answer_callback, cb_id)


def handle_message(
    client: TelegramClient, allowed_ids: set[int], message: dict
) -> None:
    """Gestisce comandi testuali solo in chat privata dall'utente autorizzato."""
    from_user = message.get("from", {})
    uid = from_user.get("id")
    chat = message.get("chat") or {}
    chat_id = chat.get("id")
    chat_type = chat.get("type")
    text = (message.get("text") or "").strip()

    if uid not in allowed_ids or chat_type != "private":
        username = from_user.get("username") or from_user.get("first_name") or "sconosciuto"
        for admin_id in allowed_ids:
            client.send_message(
                admin_id,
                f"⚠️ Tentativo di accesso bloccato da <code>{html.escape(str(uid))}</code> (@{html.escape(str(username))}, chat={html.escape(str(chat_type))})",
            )
        return

    cmd = text.split()[0].split("@")[0].lower() if text.startswith("/") else "/menu"

    if cmd in {"/start", "/menu"}:
        view_text, kb = build_home_view()
        client.send_message(chat_id, view_text, kb)
    elif cmd in {"/stato", "/status"}:
        view_text, kb = build_server_status_view()
        client.send_message(chat_id, view_text, kb)
    elif cmd == "/utility":
        view_text, kb = build_category_view("utility")
        client.send_message(chat_id, view_text, kb)
    elif cmd == "/ai":
        view_text, kb = build_category_view("ai")
        client.send_message(chat_id, view_text, kb)
    elif cmd in {"/nodi", "/earn"}:
        view_text, kb = build_category_view("earn")
        client.send_message(chat_id, view_text, kb)
    elif cmd in {"/gaming", "/game"}:
        view_text, kb = build_category_view("gaming")
        client.send_message(chat_id, view_text, kb)
    elif cmd == "/id":
        client.send_message(chat_id, f"🪪 Il tuo Telegram ID è: <code>{uid}</code>")
    else:
        view_text, kb = build_home_view()
        client.send_message(chat_id, view_text, kb)


def _load_previous_power_counters() -> tuple[int, int] | None:
    """Legge gli ultimi contatori SMART salvati prima dell'attuale boot."""
    if not POWER_STATE_FILE.exists():
        return None
    try:
        data = json.loads(POWER_STATE_FILE.read_text(encoding="utf-8"))
        return int(data["power_cycles"]), int(data["unsafe_shutdowns"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _save_power_counters(cycles: int, unsafe: int) -> None:
    """Salva su disco i contatori SMART correnti per il confronto al prossimo riavvio."""
    POWER_STATE_FILE.write_text(
        json.dumps({"power_cycles": cycles, "unsafe_shutdowns": unsafe}),
        encoding="utf-8",
    )


def _format_boot_power_diagnosis() -> str:
    """Classifica il riavvio dai contatori SMART (+5V loss vs warm reset) e aggiorna lo stato."""
    current = read_smart_power_counters()
    if current is None:
        return "⚡ <b>Diagnostica riavvio</b>: <code>SMART non disponibile</code>"

    new_cycles, new_unsafe = current
    previous = _load_previous_power_counters()
    _save_power_counters(new_cycles, new_unsafe)

    if previous is None:
        return (
            "⚡ <b>Diagnostica riavvio</b>: "
            f"<code>Baseline SSD {new_cycles}/{new_unsafe}</code>"
        )

    prev_cycles, prev_unsafe = previous
    dc = new_cycles - prev_cycles
    du = new_unsafe - prev_unsafe
    if dc > 0 or du > 0:
        return (
            f"⚡ <b>Perdita +5V SSD</b> "
            f"(<code>{dc:+d} Power Cycle</code> | <code>{du:+d} Unsafe Shutdown</code> → <code>{new_cycles}/{new_unsafe}</code>)"
        )
    if dc == 0 and du == 0:
        return f"🔄 <b>Warm Reset</b> (<code>+0 Power Cycle</code> su SSD: <code>{new_cycles}/{new_unsafe}</code>)"
    return (
        f"⚡ <b>Contatori SSD variati</b> "
        f"(<code>{dc:+d}/{du:+d}</code> → <code>{new_cycles}/{new_unsafe}</code>)"
    )


def notify_if_new_boot(client: TelegramClient, allowed_ids: set[int]) -> None:
    """Invia una notifica Telegram una sola volta per ogni vero riavvio della macchina."""
    boot_id_path = Path("/proc/sys/kernel/random/boot_id")
    if not boot_id_path.exists():
        return
    current_boot = boot_id_path.read_text(encoding="utf-8").strip()
    STATE_DIR.mkdir(parents=True, exist_ok=True)

    if not BOOT_ID_FILE.exists():
        BOOT_ID_FILE.write_text(current_boot, encoding="utf-8")
        if not POWER_STATE_FILE.exists():
            counters = read_smart_power_counters()
            if counters is not None:
                _save_power_counters(*counters)
        return

    previous_boot = BOOT_ID_FILE.read_text(encoding="utf-8").strip()
    if current_boot == previous_boot:
        return

    time.sleep(15)
    snapshot = get_docker_snapshot()
    running = sum(1 for c in snapshot.values() if c.state == "running")
    metrics = read_host_metrics(include_gpu=False, include_disks=False)
    power_diag = _format_boot_power_diagnosis()
    voltages = read_mobo_voltages()
    ras_status = read_ras_status()
    msg = (
        "🟢 <b>EnderHub è tornato online!</b>\n"
        f"{power_diag}\n"
        f"🔌 Tensioni: <code>{html.escape(voltages)}</code>\n"
        f"🛡️ HW Errors: <code>{html.escape(ras_status)}</code>\n"
        f"⏱️ Uptime: <code>{html.escape(metrics['uptime'])}</code>\n"
        f"🐳 Container attivi: <b>{running}</b>\n"
        f"🧠 RAM: <code>{html.escape(metrics['ram'])}</code>\n\n"
        "Usa /menu per aprire i controlli."
    )
    for uid in allowed_ids:
        client.send_message(uid, msg)
    BOOT_ID_FILE.write_text(current_boot, encoding="utf-8")


def acquire_single_instance_lock() -> None:
    """Acquisisce un lock esclusivo sul file di stato per evitare duplicati."""
    global LOCK_FD
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    LOCK_FD = open(LOCK_FILE, "a+", encoding="utf-8")
    try:
        fcntl.flock(LOCK_FD.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("[enderhub-bot] Un'altra istanza è già attiva, uscita.", file=sys.stderr)
        sys.exit(0)
    LOCK_FD.seek(0)
    LOCK_FD.truncate(0)
    LOCK_FD.write(f"{os.getpid()}\n")
    LOCK_FD.flush()


def main() -> None:
    acquire_single_instance_lock()
    env = load_env(ENV_FILE)
    token = env.get("TELEGRAM_BOT_TOKEN") or os.environ.get("TELEGRAM_BOT_TOKEN", "")
    raw_ids = env.get("ALLOWED_USER_IDS") or os.environ.get("ALLOWED_USER_IDS", "")
    allowed_ids = {
        int(x.strip()) for x in raw_ids.split(",") if x.strip().isdigit()
    }

    if not token or not allowed_ids:
        print(
            "Errore: TELEGRAM_BOT_TOKEN o ALLOWED_USER_IDS mancanti in .env",
            file=sys.stderr,
        )
        sys.exit(1)

    STOPPED_DIR.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(token)

    def _on_signal(signum: int, _frame: object) -> None:
        STOP_EVENT.set()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    # Pre-riscalda campionamento CPU per i container già attivi
    get_docker_stats_map()

    for _ in range(10):
        try:
            client.set_commands()
            break
        except Exception:
            if STOP_EVENT.is_set():
                return
            time.sleep(5)

    submit_safe(notify_if_new_boot, client, allowed_ids)

    offset = 0
    backoff = 2
    while not STOP_EVENT.is_set():
        try:
            res = client.call(
                "getUpdates",
                {
                    "offset": offset,
                    "timeout": 25,
                    "allowed_updates": ["message", "callback_query"],
                },
                timeout=35,
            )
            if not res.get("ok"):
                time.sleep(backoff)
                backoff = min(backoff * 2, 30)
                continue

            backoff = 2
            for upd in res.get("result", []):
                offset = upd["update_id"] + 1
                if "callback_query" in upd:
                    submit_safe(
                        handle_callback, client, allowed_ids, upd["callback_query"]
                    )
                elif "message" in upd:
                    submit_safe(handle_message, client, allowed_ids, upd["message"])
        except (TimeoutError, socket.timeout, OSError):
            if STOP_EVENT.is_set():
                break
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        except Exception as exc:
            print(f"[enderhub-bot] errore loop: {exc}", file=sys.stderr, flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
