"""
Python Script Hosting Bot  --  v2 (button-controlled)
Token / owner id neeche CONFIG section me hardcoded hain.
"""
import asyncio
import json
import logging
import os
import platform
import re
import shutil
import signal
import sys
import time
from html import escape as _html_escape
from pathlib import Path

import psutil
from aiogram import Bot, Dispatcher, Router, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import BaseFilter, Command, CommandObject
from aiogram.types import (
    BotCommand, CallbackQuery, ErrorEvent, FSInputFile,
    InlineKeyboardButton, InlineKeyboardMarkup,
    KeyboardButton, Message, ReplyKeyboardMarkup,
)

# ============================================================
#                         CONFIG
# ============================================================
BOT_TOKEN = "8705659753:AAF_qH7AY8Tow76UvUpSI8RyYSo6gkVcG9E"
OWNER_ID = 7885352768

DATA_DIR = Path("./data").resolve()
SCRIPTS_DIR = DATA_DIR / "scripts"
LOGS_DIR = DATA_DIR / "logs"
STATE_FILE = DATA_DIR / "state.json"

MAX_LOG_LINES = 60            # lines shown in chat on "Logs"
MAX_LOG_BYTES = 2_000_000     # log rotates after this size
MAX_UPLOAD_BYTES = 5 * 1024 * 1024
AUTORESTART_DELAY = 3         # base delay (seconds)
FAST_EXIT_SECS = 20           # exit sooner than this = "fast exit"
MAX_FAST_EXITS = 5            # this many fast exits in a row = crash loop, give up
PIP_TIMEOUT = 600
BOOT_TIME = time.time()

NAME_RE = re.compile(r"^[A-Za-z0-9_\-]{1,40}$")
PKG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-\.\[\]=<>!~,]{0,99}$")
PIP_PREFIX = re.compile(r"^\s*(?:python3?\s+-m\s+)?pip3?\s+install\s+", re.I)

for _d in (SCRIPTS_DIR, LOGS_DIR):
    _d.mkdir(parents=True, exist_ok=True)

PREINSTALLED_DEPS = [
    "Kurigram==2.2.26",
    "basiccalls==2.2.9.2",
    "static-ffmpeg==3.0",
    "aiogram>=3.4,<4",
    "aiohttp>=3.9",
    "psutil",
    "ffmpeg (system binary)",
]


# ============================================================
#                        HELPERS
# ============================================================
def esc(s: str) -> str:
    return _html_escape(str(s), quote=False)


def fmt_bytes(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def fmt_dur(secs: float) -> str:
    secs = int(secs)
    d, r = divmod(secs, 86400)
    h, r = divmod(r, 3600)
    m, s = divmod(r, 60)
    if d:
        return f"{d}d {h}h {m}m"
    if h:
        return f"{h}h {m}m {s}s"
    return f"{m}m {s}s"


def pre_block(text: str, limit: int = 3500) -> str:
    text = text[-limit:]
    while len(esc(text)) > limit + 300 and len(text) > 200:
        text = text[200:]
    return f"<pre>{esc(text)}</pre>"


def req_key(spec: str) -> str:
    return re.split(r"[\[=<>!~,;\s]", spec, maxsplit=1)[0].lower().replace("_", "-")


def merge_requirements(path: Path, new_specs: list[str]) -> None:
    merged: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(errors="ignore").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                merged[req_key(line)] = line
    for spec in new_specs:
        merged[req_key(spec)] = spec
    path.write_text("\n".join(merged.values()) + "\n")


def parse_packages(text: str) -> list[str]:
    pkgs: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        line = PIP_PREFIX.sub("", line)
        for tok in line.split():
            tok = tok.strip(",")
            if not tok:
                continue
            if "," in tok and not re.search(r"[<>=!~]", tok):
                pkgs.extend(t for t in tok.split(",") if t)
            else:
                pkgs.append(tok)
    seen: set[str] = set()
    out: list[str] = []
    for p in pkgs:
        if p.lower() not in seen:
            seen.add(p.lower())
            out.append(p)
    return out


def check_syntax(content: bytes, fname: str) -> str | None:
    """Compile only (never executes). Returns an error string or None."""
    try:
        compile(content, fname, "exec", dont_inherit=True)
        return None
    except SyntaxError as e:
        return f"line {e.lineno}: {e.msg}"
    except (ValueError, MemoryError, RecursionError) as e:
        return str(e)


async def pip_run(args: list[str], timeout: int = PIP_TIMEOUT) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "pip", "install",
        "--no-cache-dir", "--disable-pip-version-check", *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return -1, "timeout"
    return proc.returncode or 0, out.decode(errors="replace")


def proc_memory(pid: int) -> int | None:
    try:
        p = psutil.Process(pid)
        rss = p.memory_info().rss
        for c in p.children(recursive=True):
            try:
                rss += c.memory_info().rss
            except psutil.Error:
                pass
        return rss
    except psutil.Error:
        return None


# ============================================================
#                     SCRIPT MANAGER
# ============================================================
class ScriptInfo:
    def __init__(self, name: str):
        self.name = name
        self.process: asyncio.subprocess.Process | None = None
        self.task: asyncio.Task | None = None
        self.started_at: float | None = None
        self.restarts = 0
        self.autorestart = True
        self.manually_stopped = False
        self.was_running = False      # persisted: auto-start after bot reboot
        self.crashed = False          # supervisor gave up (crash loop)
        self.fast_streak = 0
        self.last_exit: int | None = None

    @property
    def running(self) -> bool:
        return self.process is not None and self.process.returncode is None

    @property
    def active(self) -> bool:
        """Supervisor task alive (running OR waiting to restart)."""
        return self.task is not None and not self.task.done()


class ScriptManager:
    def __init__(self):
        self.scripts: dict[str, ScriptInfo] = {}
        self.notify = None  # async (text, name=None)
        self.pip_lock = asyncio.Lock()
        self._bg: set[asyncio.Task] = set()
        self._load_existing()

    # ---------- paths ----------
    def script_dir(self, name):  return SCRIPTS_DIR / name
    def script_path(self, name): return SCRIPTS_DIR / name / "main.py"
    def log_path(self, name):    return LOGS_DIR / f"{name}.log"

    def exists(self, name: str) -> bool:
        return bool(NAME_RE.match(name or "")) and self.script_path(name).exists()

    def info(self, name: str) -> ScriptInfo:
        return self.scripts.setdefault(name, ScriptInfo(name))

    # ---------- persistence ----------
    def _load_existing(self):
        state: dict = {}
        try:
            state = json.loads(STATE_FILE.read_text())
        except (OSError, ValueError):
            pass
        for d in sorted(SCRIPTS_DIR.iterdir()):
            if d.is_dir() and NAME_RE.match(d.name) and (d / "main.py").exists():
                info = ScriptInfo(d.name)
                st = state.get(d.name, {})
                info.autorestart = bool(st.get("autorestart", True))
                info.was_running = bool(st.get("was_running", False))
                self.scripts[d.name] = info

    def save_state(self):
        data = {
            n: {"autorestart": i.autorestart, "was_running": i.was_running}
            for n, i in self.scripts.items()
        }
        tmp = STATE_FILE.with_suffix(".tmp")
        try:
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(STATE_FILE)
        except OSError as e:
            logging.warning("state save failed: %s", e)

    # ---------- script files ----------
    async def save_script(self, name: str, content: bytes) -> bool:
        """Returns True if this overwrote an existing script."""
        existed = self.exists(name)
        d = self.script_dir(name)
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / "main.py.tmp"
        tmp.write_bytes(content)
        tmp.replace(self.script_path(name))
        self.info(name)
        self.save_state()
        return existed

    async def delete_script(self, name: str):
        await self.stop(name)
        d = self.script_dir(name)
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)
        for p in (self.log_path(name), self.log_path(name).with_suffix(".log.1")):
            try:
                p.unlink()
            except FileNotFoundError:
                pass
        self.scripts.pop(name, None)
        self.save_state()

    # ---------- process control ----------
    async def start(self, name: str, settle: float = 0.8) -> bool:
        if not self.exists(name):
            raise FileNotFoundError(name)
        info = self.info(name)
        if info.active:
            return False
        info.manually_stopped = False
        info.crashed = False
        info.fast_streak = 0
        info.was_running = True
        self.save_state()
        info.task = asyncio.create_task(self._supervise(info))
        if settle:
            await asyncio.sleep(settle)
        return True

    async def stop(self, name: str) -> bool:
        info = self.scripts.get(name)
        if not info:
            return False
        was = info.running or info.active
        await self._halt(info)
        info.was_running = False
        info.crashed = False
        self.save_state()
        return was

    async def restart(self, name: str) -> bool:
        await self.stop(name)
        await asyncio.sleep(0.4)
        return await self.start(name)

    async def shutdown(self):
        """Bot is exiting: stop children but keep was_running so they auto-start next boot."""
        for t in list(self._bg):
            t.cancel()
        live = [i for i in self.scripts.values() if i.active or i.running]
        await asyncio.gather(*(self._halt(i) for i in live), return_exceptions=True)

    async def _halt(self, info: ScriptInfo):
        info.manually_stopped = True
        proc = info.process
        if proc is not None and proc.returncode is None:
            await self._kill(proc)
        task = info.task
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        info.task = None
        info.process = None

    @staticmethod
    async def _kill(proc: asyncio.subprocess.Process):
        """SIGTERM -> SIGKILL on the whole process group (script + its children)."""
        for sig, wait in ((signal.SIGTERM, 5), (signal.SIGKILL, 3)):
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
                break
            except OSError:
                try:
                    proc.send_signal(sig)
                except ProcessLookupError:
                    break
            try:
                await asyncio.wait_for(proc.wait(), timeout=wait)
                break
            except asyncio.TimeoutError:
                continue
        # clean up any stray children left in the group
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass

    def _rotate_log(self, name: str):
        p = self.log_path(name)
        try:
            if p.exists() and p.stat().st_size > MAX_LOG_BYTES:
                p.replace(p.with_suffix(".log.1"))
        except OSError:
            pass

    async def _notify(self, text: str, name: str | None = None):
        if self.notify:
            try:
                await self.notify(text, name)
            except Exception:
                logging.exception("notify failed")

    async def _run_once(self, info: ScriptInfo) -> int:
        log = self.log_path(info.name)
        log.parent.mkdir(parents=True, exist_ok=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1"}
        env.pop("BOT_TOKEN", None)  # hosted scripts must not inherit the host bot's token
        with open(log, "ab") as f:
            f.write(f"\n\n===== [{time.ctime()}] START (restart #{info.restarts}) =====\n".encode())
            f.flush()
            try:
                proc = await asyncio.create_subprocess_exec(
                    sys.executable, "-u", str(self.script_path(info.name)),
                    cwd=str(self.script_dir(info.name)),
                    stdout=f, stderr=f, env=env,
                    start_new_session=True,
                )
            except Exception as e:
                f.write(f"SPAWN ERROR: {e}\n".encode())
                return -1
            info.process = proc
            info.started_at = time.time()
            try:
                rc = await proc.wait()
            finally:
                info.process = None
            f.write(f"\n===== [{time.ctime()}] EXIT code={rc} =====\n".encode())
            return rc

    async def _supervise(self, info: ScriptInfo):
        name = info.name
        try:
            while not info.manually_stopped:
                self._rotate_log(name)
                started = time.time()
                rc = await self._run_once(info)
                if info.manually_stopped:
                    break
                ran = time.time() - started
                info.last_exit = rc

                if not info.autorestart:
                    await self._notify(
                        f"⏹ <code>{esc(name)}</code> exit ho gayi (code <b>{rc}</b>). "
                        "Auto-restart OFF hai.", name)
                    break

                info.fast_streak = info.fast_streak + 1 if ran < FAST_EXIT_SECS else 0
                if info.fast_streak >= MAX_FAST_EXITS:
                    info.crashed = True
                    why = ("jaldi khatam ho jaati hai (exit code 0)" if rc == 0
                           else f"baar-baar crash ho rahi hai (last exit code {rc})")
                    await self._notify(
                        f"💥 <code>{esc(name)}</code> {why}.\n"
                        f"{MAX_FAST_EXITS} baar try kiya — auto-restart band kar diya. "
                        "Logs dekho, fix karke ▶️ Run dabao.", name)
                    break

                delay = min(AUTORESTART_DELAY * 2 ** max(info.fast_streak - 1, 0), 60)
                info.restarts += 1
                if info.fast_streak <= 1:
                    await self._notify(
                        f"⚠️ <code>{esc(name)}</code> exit hui (code <b>{rc}</b>). "
                        f"{delay}s me auto-restart…", name)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        except Exception:
            logging.exception("supervisor crashed for %s", name)
            info.crashed = True

    # ---------- boot ----------
    async def autostart_all(self):
        names = [n for n, i in self.scripts.items() if i.was_running and self.exists(n)]
        if not names:
            return
        started, warns = [], []
        for n in names:
            req = self.script_dir(n) / "requirements.txt"
            if req.exists() and req.read_text(errors="ignore").strip():
                async with self.pip_lock:
                    rc, _ = await pip_run(["-r", str(req)])
                if rc != 0:
                    warns.append(f"📦 <code>{esc(n)}</code>: requirements install fail")
            try:
                await self.start(n, settle=0)
                started.append(n)
            except Exception:
                logging.exception("autostart failed for %s", n)
                warns.append(f"❌ <code>{esc(n)}</code>: start nahi hui")
        text = "♻️ <b>Auto-start:</b> " + ", ".join(f"<code>{esc(n)}</code>" for n in started)
        if warns:
            text += "\n" + "\n".join(warns)
        await self._notify(text)

    # ---------- info ----------
    def status(self, name: str) -> str:
        info = self.scripts.get(name)
        if not info:
            return "stopped"
        if info.running:
            return "running"
        if info.crashed:
            return "crashed"
        if info.active:
            return "restarting"
        return "stopped"

    def uptime(self, name: str) -> str:
        info = self.scripts.get(name)
        if not info or not info.running or not info.started_at:
            return "-"
        return fmt_dur(time.time() - info.started_at)

    def tail_log(self, name: str, n: int = 60) -> str:
        p = self.log_path(name)
        if not p.exists():
            return "(abhi koi log nahi)"
        data = p.read_bytes()
        if len(data) > 200_000:
            data = data[-200_000:]
        lines = data.decode(errors="replace").splitlines()
        return "\n".join(lines[-n:]) or "(log khali hai)"


# ============================================================
#                      UI: KEYBOARDS & TEXT
# ============================================================
BTN_SCRIPTS = "📂 My Scripts"
BTN_UPLOAD = "➕ Upload Script"
BTN_PKGS = "📦 Install Packages"
BTN_STATUS = "📊 Server Status"
BTN_RUNALL = "▶️ Run All"
BTN_STOPALL = "⏹ Stop All"
BTN_HELP = "❓ Help"

ICON = {"running": "🟢", "restarting": "🟡", "crashed": "💥", "stopped": "🔴"}
LABEL = {
    "running": "Running",
    "restarting": "Restarting…",
    "crashed": "Crashed — auto-restart band",
    "stopped": "Stopped",
}

WELCOME = (
    "👋 <b>Welcome!</b>\n\n"
    "Main aapka <b>Python Script Hosting Bot</b> hoon. "
    "Neeche wale <b>menu buttons</b> se sab kuch control karo — commands yaad rakhne ki zaroorat nahi.\n\n"
    "1️⃣ <b>➕ Upload Script</b> → <code>.py</code> file bhejo\n"
    "2️⃣ <b>📂 My Scripts</b> → Run / Stop / Logs / Delete\n"
    "3️⃣ <b>📦 Install Packages</b> → extra pip packages"
)

HELP = (
    "<b>🐍 Python Script Hosting Bot</b>\n\n"
    "<b>Kaise use kare:</b>\n"
    "• <b>➕ Upload Script</b> dabao → <code>.py</code> file bhejo (caption me naam likh sakte ho)\n"
    "• Same naam se dobara file bhejoge to script update hogi (chal rahi thi to auto-restart)\n"
    "• <b>📂 My Scripts</b> → script chuno → Run / Stop / Restart / Logs / Delete\n"
    "• <b>📦 Install Packages</b> → package naam bhejo ya <code>requirements.txt</code> file\n\n"
    "<b>Reliability:</b>\n"
    "• Script crash ho to auto-restart (backoff ke saath)\n"
    "• Crash-loop ho to band + aapko alert\n"
    "• Bot restart ho to pehle se chal rahi scripts apne aap start\n"
    "• Requirements bot boot par dobara install hoti hain\n\n"
    "<b>Commands (optional):</b> /start /list /status /cancel /help\n\n"
    "<b>Pre-installed:</b> " + ", ".join(PREINSTALLED_DEPS)
)


def btn(text: str, data: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(text=text, callback_data=data)


def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_SCRIPTS), KeyboardButton(text=BTN_UPLOAD)],
            [KeyboardButton(text=BTN_PKGS), KeyboardButton(text=BTN_STATUS)],
            [KeyboardButton(text=BTN_RUNALL), KeyboardButton(text=BTN_STOPALL)],
            [KeyboardButton(text=BTN_HELP)],
        ],
        resize_keyboard=True,
        is_persistent=True,
        input_field_placeholder="👇 Menu se option chuno",
    )


def kb_cancel() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("❌ Cancel", "cancel")]])


def dashboard(manager: ScriptManager, note: str = ""):
    names = sorted(manager.scripts)
    running = sum(1 for n in names if manager.status(n) == "running")
    head = (note + "\n\n") if note else ""
    if names:
        text = f"{head}🏠 <b>Dashboard</b>\n\n🟢 Running: <b>{running}</b> / {len(names)}\n\nScript chuno 👇"
    else:
        text = (f"{head}🏠 <b>Dashboard</b>\n\nAbhi koi script nahi hai.\n"
                "<b>➕ Upload Script</b> dabao aur <code>.py</code> file bhejo.")
    rows, row = [], []
    for n in names[:90]:
        row.append(btn(f"{ICON[manager.status(n)]} {n}", f"s:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    if names:
        rows.append([btn("▶️ Run All", "runall"), btn("⏹ Stop All", "stopall")])
    rows.append([btn("🔄 Refresh", "dash"), btn("📊 Server", "srv")])
    return text, InlineKeyboardMarkup(inline_keyboard=rows)


def panel(manager: ScriptManager, name: str, note: str = ""):
    if not manager.exists(name):
        return None
    info = manager.info(name)
    st = manager.status(name)
    lines = []
    if note:
        lines += [note, ""]
    lines.append(f"📄 <b>{esc(name)}</b>")
    lines.append(f"{ICON[st]} <b>{LABEL[st]}</b>")
    if st == "running":
        lines.append(f"⏱ Uptime: {manager.uptime(name)}")
        mem = proc_memory(info.process.pid) if info.process else None
        if mem is not None:
            lines.append(f"🧠 RAM: {fmt_bytes(mem)}  •  PID {info.process.pid}")
    lines.append(f"🔁 Auto-restart: {'ON' if info.autorestart else 'OFF'}  •  Restarts: {info.restarts}")
    if info.last_exit is not None and st != "running":
        lines.append(f"🚪 Last exit code: {info.last_exit}")
    try:
        sp = manager.script_path(name)
        stt = sp.stat()
        lines.append(f"📏 {fmt_bytes(stt.st_size)}  •  Updated: {time.strftime('%d %b %H:%M', time.localtime(stt.st_mtime))}")
    except OSError:
        pass

    rows = []
    if st in ("running", "restarting"):
        rows.append([btn("⏹ Stop", f"stop:{name}"), btn("🔁 Restart", f"rst:{name}")])
    else:
        rows.append([btn("▶️ Run", f"run:{name}")])
    rows.append([btn("📋 Logs", f"log:{name}"), btn("📥 Full Log", f"logf:{name}"), btn("🧹 Clear", f"logc:{name}")])
    rows.append([
        btn("📦 Requirements", f"req:{name}"),
        btn(f"🔁 Auto: {'ON ✅' if info.autorestart else 'OFF ❌'}", f"ar:{name}"),
    ])
    rows.append([btn("📄 Get File", f"get:{name}"), btn("🗑 Delete", f"del:{name}")])
    rows.append([btn("🔄 Refresh", f"s:{name}"), btn("⬅️ Back", "dash")])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def server_text(manager: ScriptManager) -> str:
    cpu = await asyncio.to_thread(psutil.cpu_percent, 0.4)
    vm = psutil.virtual_memory()
    du = shutil.disk_usage(DATA_DIR)
    total = len(manager.scripts)
    running = sum(1 for n in manager.scripts if manager.status(n) == "running")
    return (
        "📊 <b>Server Status</b>\n\n"
        f"🖥 CPU: {cpu:.0f}%\n"
        f"🧠 RAM: {fmt_bytes(vm.used)} / {fmt_bytes(vm.total)} ({vm.percent:.0f}%)\n"
        f"💾 Disk: {fmt_bytes(du.free)} free / {fmt_bytes(du.total)}\n"
        f"⏱ Bot uptime: {fmt_dur(time.time() - BOOT_TIME)}\n"
        f"🐍 Python {platform.python_version()}\n"
        f"📂 Scripts: <b>{running}</b> running / {total} total"
    )


def kb_server() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("🔄 Refresh", "srv"), btn("📂 Scripts", "dash")]])


def kb_stopall_confirm() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[btn("✅ Haan, Stop All", "stopall_ok"), btn("❌ Nahi", "dash")]])


async def safe_edit(message, text: str, kb: InlineKeyboardMarkup | None = None):
    """Edit a message in place; ignore 'not modified', fall back to a new message."""
    if not isinstance(message, Message):
        return
    try:
        await message.edit_text(text, reply_markup=kb)
    except TelegramBadRequest as e:
        if "not modified" in str(e).lower():
            return
        try:
            await message.answer(text, reply_markup=kb)
        except Exception:
            logging.exception("safe_edit fallback failed")


async def edit_panel(message, manager: ScriptManager, name: str, note: str = ""):
    res = panel(manager, name, note)
    if res is None:
        text, kb = dashboard(manager, "❌ Script nahi mili.")
        await safe_edit(message, text, kb)
        return
    await safe_edit(message, *res)


# ============================================================
#                          ROUTER
# ============================================================
router = Router()


class IsOwner(BaseFilter):
    async def __call__(self, event) -> bool:
        u = getattr(event, "from_user", None)
        return u is not None and u.id == OWNER_ID


router.message.filter(IsOwner())
router.callback_query.filter(IsOwner())

# uid -> ("req", script_name | None)
WAITING: dict[int, tuple[str, str | None]] = {}


def arg(cb: CallbackQuery) -> str:
    return (cb.data or "").split(":", 1)[1]


def req_prompt(name: str | None) -> str:
    who = f"<code>{esc(name)}</code>" if name else "global (kisi script se linked nahi)"
    return (
        f"<b>📦 Install Packages — {who}</b>\n\n"
        "Package naam bhejo (ek line me ek, ya space/comma se alag) "
        "ya <code>requirements.txt</code> file bhejo.\n"
        "<pre>requests\nbeautifulsoup4==4.12.3\ntelethon</pre>"
    )


async def do_run_all(manager: ScriptManager) -> int:
    n = 0
    for name in sorted(manager.scripts):
        if manager.exists(name) and not manager.info(name).active:
            await manager.start(name, settle=0)
            n += 1
    if n:
        await asyncio.sleep(1)
    return n


async def do_stop_all(manager: ScriptManager) -> int:
    live = [n for n in list(manager.scripts) if manager.info(n).running or manager.info(n).active]
    for n in live:
        await manager.stop(n)
    return len(live)


# ---------- Commands ----------
@router.message(Command("start", "menu"))
async def cmd_start(msg: Message, manager: ScriptManager):
    WAITING.pop(msg.from_user.id, None)
    await msg.answer(WELCOME, reply_markup=main_menu())
    text, kb = dashboard(manager)
    await msg.answer(text, reply_markup=kb)


@router.message(Command("help"))
@router.message(F.text == BTN_HELP)
async def cmd_help(msg: Message):
    WAITING.pop(msg.from_user.id, None)
    await msg.answer(HELP, reply_markup=main_menu())


@router.message(Command("list"))
@router.message(F.text == BTN_SCRIPTS)
async def cmd_list(msg: Message, manager: ScriptManager):
    WAITING.pop(msg.from_user.id, None)
    text, kb = dashboard(manager)
    await msg.answer(text, reply_markup=kb)


@router.message(Command("status"))
async def cmd_status(msg: Message, manager: ScriptManager, command: CommandObject):
    name = (command.args or "").strip()
    if name:
        res = panel(manager, name)
        if res is None:
            await msg.answer("❌ Script nahi mili.")
            return
        await msg.answer(*res)
        return
    await msg.answer(await server_text(manager), reply_markup=kb_server())


@router.message(F.text == BTN_STATUS)
async def menu_status(msg: Message, manager: ScriptManager):
    WAITING.pop(msg.from_user.id, None)
    await msg.answer(await server_text(manager), reply_markup=kb_server())


@router.message(Command("cancel"))
async def cmd_cancel(msg: Message):
    WAITING.pop(msg.from_user.id, None)
    await msg.answer("✅ Cancel ho gaya.", reply_markup=main_menu())


@router.message(F.text == BTN_UPLOAD)
async def menu_upload(msg: Message):
    WAITING.pop(msg.from_user.id, None)
    await msg.answer(
        "📤 <b>Upload Script</b>\n\n"
        "Ab apni <code>.py</code> file bhejo.\n"
        "Caption me naam likhoge to wahi naam use hoga (optional).\n"
        "Same naam ki script already hai to update ho jayegi."
    )


@router.message(F.text == BTN_PKGS)
async def menu_pkgs(msg: Message, manager: ScriptManager):
    WAITING.pop(msg.from_user.id, None)
    names = sorted(manager.scripts)
    if not names:
        WAITING[msg.from_user.id] = ("req", None)
        await msg.answer(req_prompt(None), reply_markup=kb_cancel())
        return
    rows, row = [], []
    for n in names[:90]:
        row.append(btn(f"📄 {n}", f"req:{n}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([btn("🌐 Sirf install (script se link nahi)", "reqg")])
    rows.append([btn("❌ Cancel", "cancel")])
    await msg.answer(
        "📦 <b>Install Packages</b>\n\nKis script ke liye? "
        "(Link karoge to bot restart/redeploy ke baad packages apne aap wapas install honge.)",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
    )


@router.message(F.text == BTN_RUNALL)
async def menu_runall(msg: Message, manager: ScriptManager):
    WAITING.pop(msg.from_user.id, None)
    n = await do_run_all(manager)
    text, kb = dashboard(manager, f"▶️ {n} script(s) start ki gayi." if n else "ℹ️ Sab scripts pehle se chal rahi hain (ya koi script nahi).")
    await msg.answer(text, reply_markup=kb)


@router.message(F.text == BTN_STOPALL)
async def menu_stopall(msg: Message, manager: ScriptManager):
    WAITING.pop(msg.from_user.id, None)
    live = sum(1 for n in manager.scripts if manager.info(n).running or manager.info(n).active)
    if not live:
        await msg.answer("ℹ️ Koi script chal nahi rahi.")
        return
    await msg.answer(f"⚠️ <b>{live}</b> script(s) stop karni hain?", reply_markup=kb_stopall_confirm())


# ---------- File upload ----------
@router.message(F.document)
async def on_document(msg: Message, bot: Bot, manager: ScriptManager):
    uid = msg.from_user.id
    doc = msg.document
    fname = doc.file_name or ""
    low = fname.lower()

    if doc.file_size and doc.file_size > MAX_UPLOAD_BYTES:
        await msg.answer("❌ File 5MB se badi hai.")
        return

    # requirements.txt (only when waiting for packages)
    if low.endswith(".txt"):
        wait = WAITING.get(uid)
        if not wait or wait[0] != "req":
            await msg.answer(
                "ℹ️ Requirements bhejne ke liye pehle <b>📦 Install Packages</b> dabao, phir file bhejo.\n"
                "Script upload ke liye <code>.py</code> file bhejo."
            )
            return
        buf = await bot.download(doc)
        text = buf.read().decode("utf-8", errors="ignore") if buf else ""
        WAITING.pop(uid, None)
        await do_requirements(msg, manager, wait[1], parse_packages(text))
        return

    if not low.endswith(".py"):
        await msg.answer("❌ Sirf <code>.py</code> file bhejo (requirements ke liye <code>.txt</code>).")
        return

    name = None
    if msg.caption:
        first = msg.caption.strip().split()
        if first:
            name = first[0]
    if not name:
        name = re.sub(r"\.py$", "", fname, flags=re.I)
    name = re.sub(r"[^A-Za-z0-9_\-]", "_", name)[:40]
    if not name.strip("_"):
        await msg.answer("❌ Naam valid nahi. Caption me simple naam likho (a-z, 0-9, _ , -).")
        return

    WAITING.pop(uid, None)
    buf = await bot.download(doc)
    content = buf.read() if buf else b""
    if not content:
        await msg.answer("❌ File download nahi ho paayi, dobara bhejo.")
        return

    was_active = manager.exists(name) and manager.info(name).active
    existed = await manager.save_script(name, content)

    notes = [f"✅ {'Updated' if existed else 'Saved'}: <code>{esc(name)}</code> ({fmt_bytes(len(content))})"]
    err = check_syntax(content, fname or f"{name}.py")
    if err:
        notes.append(f"⚠️ <b>Syntax error</b> — {esc(err)}\nFix karke file dobara bhejo, warna script crash hogi.")
    elif was_active:
        await manager.restart(name)
        notes.append("♻️ Script chal rahi thi — nayi file ke saath restart kar di.")
    res = panel(manager, name, "\n".join(notes))
    await msg.answer(*res)


# ---------- Text (package install flow) ----------
@router.message(F.text)
async def on_text(msg: Message, manager: ScriptManager):
    uid = msg.from_user.id
    wait = WAITING.pop(uid, None)
    if wait and wait[0] == "req":
        await do_requirements(msg, manager, wait[1], parse_packages(msg.text or ""))
        return
    await msg.answer("🤔 Samajh nahi aaya. Neeche menu buttons use karo 👇", reply_markup=main_menu())


@router.message()
async def on_other(msg: Message):
    await msg.answer("🤔 Menu buttons use karo 👇  (script ke liye <code>.py</code> file bhejo)", reply_markup=main_menu())


async def do_requirements(msg: Message, manager: ScriptManager, name: str | None, pkgs: list[str]):
    if name and not manager.exists(name):
        await msg.answer("❌ Script nahi mili.")
        return
    if not pkgs:
        await msg.answer("❌ Koi package nahi mila.")
        return
    extra = ""
    if len(pkgs) > 30:
        extra = f"\n⚠️ Sirf pehle 30 packages liye gaye ({len(pkgs)} the)."
        pkgs = pkgs[:30]

    note = await msg.answer(f"⏳ Installing 0/{len(pkgs)}…")
    results, ok_specs = [], []
    async with manager.pip_lock:
        for i, pkg in enumerate(pkgs, 1):
            if not PKG_RE.match(pkg):
                results.append(f"❌ <code>{esc(pkg)}</code> → invalid naam")
                continue
            try:
                await note.edit_text(f"⏳ Installing {i}/{len(pkgs)}: <code>{esc(pkg)}</code>")
            except Exception:
                pass
            try:
                rc, out = await pip_run([pkg])
            except Exception as e:
                results.append(f"❌ <code>{esc(pkg)}</code> → {esc(e)}")
                continue
            if rc == 0:
                results.append(f"✅ <code>{esc(pkg)}</code>")
                ok_specs.append(pkg)
            elif out == "timeout":
                results.append(f"⏰ <code>{esc(pkg)}</code> → timeout (10 min)")
            else:
                tail = "\n".join(out.splitlines()[-6:])
                results.append(f"❌ <code>{esc(pkg)}</code>\n<pre>{esc(tail)}</pre>")

    if name and ok_specs:
        try:
            merge_requirements(manager.script_dir(name) / "requirements.txt", ok_specs)
        except OSError:
            logging.exception("requirements write failed")

    report = "<b>📦 Install report</b>\n\n" + "\n".join(results) + extra
    if name and ok_specs and manager.status(name) in ("running", "restarting"):
        report += "\n\nℹ️ Script chal rahi hai — naye packages ke liye <b>Restart</b> karo."
    if len(report) > 3900:
        report = report[:3900] + "\n…(truncated)"
    kb = InlineKeyboardMarkup(inline_keyboard=[[btn("📄 Script Panel", f"s:{name}")]]) if name else None
    try:
        await note.delete()
    except Exception:
        pass
    await msg.answer(report, reply_markup=kb)


# ============================================================
#                        CALLBACKS
# ============================================================
@router.callback_query(F.data == "noop")
async def cb_noop(cb: CallbackQuery):
    await cb.answer()


@router.callback_query(F.data == "dash")
async def cb_dash(cb: CallbackQuery, manager: ScriptManager):
    await cb.answer()
    text, kb = dashboard(manager)
    await safe_edit(cb.message, text, kb)


@router.callback_query(F.data == "srv")
async def cb_srv(cb: CallbackQuery, manager: ScriptManager):
    await cb.answer()
    await safe_edit(cb.message, await server_text(manager), kb_server())


@router.callback_query(F.data == "cancel")
async def cb_cancel(cb: CallbackQuery):
    WAITING.pop(cb.from_user.id, None)
    await cb.answer("Cancelled")
    await safe_edit(cb.message, "✅ Cancel ho gaya.")


@router.callback_query(F.data == "runall")
async def cb_runall(cb: CallbackQuery, manager: ScriptManager):
    await cb.answer("⏳ Starting all…")
    n = await do_run_all(manager)
    text, kb = dashboard(manager, f"▶️ {n} script(s) start ki gayi." if n else "ℹ️ Sab pehle se chal rahi hain.")
    await safe_edit(cb.message, text, kb)


@router.callback_query(F.data == "stopall")
async def cb_stopall(cb: CallbackQuery, manager: ScriptManager):
    await cb.answer()
    live = sum(1 for n in manager.scripts if manager.info(n).running or manager.info(n).active)
    if not live:
        text, kb = dashboard(manager, "ℹ️ Koi script chal nahi rahi.")
        await safe_edit(cb.message, text, kb)
        return
    await safe_edit(cb.message, f"⚠️ <b>{live}</b> script(s) stop karni hain?", kb_stopall_confirm())


@router.callback_query(F.data == "stopall_ok")
async def cb_stopall_ok(cb: CallbackQuery, manager: ScriptManager):
    await cb.answer("⏳ Stopping all…")
    n = await do_stop_all(manager)
    text, kb = dashboard(manager, f"⏹ {n} script(s) stop ho gayi.")
    await safe_edit(cb.message, text, kb)


@router.callback_query(F.data == "reqg")
async def cb_reqg(cb: CallbackQuery, bot: Bot):
    WAITING[cb.from_user.id] = ("req", None)
    await cb.answer()
    await safe_edit(cb.message, req_prompt(None), kb_cancel())


@router.callback_query(F.data.startswith("s:"))
async def cb_open(cb: CallbackQuery, manager: ScriptManager):
    await cb.answer()
    await edit_panel(cb.message, manager, arg(cb))


@router.callback_query(F.data.startswith("run:"))
async def cb_run(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    await cb.answer("⏳ Starting…")
    ok = await manager.start(name)
    await edit_panel(cb.message, manager, name, "✅ Start ho gayi." if ok else "ℹ️ Pehle se chal rahi hai.")


@router.callback_query(F.data.startswith("stop:"))
async def cb_stop(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    await cb.answer("⏳ Stopping…")
    ok = await manager.stop(name)
    await edit_panel(cb.message, manager, name, "⏹ Stop ho gayi." if ok else "ℹ️ Pehle se band hai.")


@router.callback_query(F.data.startswith("rst:"))
async def cb_restart(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    await cb.answer("⏳ Restarting…")
    await manager.restart(name)
    await edit_panel(cb.message, manager, name, "🔁 Restart ho gayi.")


@router.callback_query(F.data.startswith("ar:"))
async def cb_autorestart(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    info = manager.info(name)
    info.autorestart = not info.autorestart
    manager.save_state()
    await cb.answer(f"Auto-restart {'ON' if info.autorestart else 'OFF'}")
    await edit_panel(cb.message, manager, name)


@router.callback_query(F.data.startswith("log:"))
async def cb_logs(cb: CallbackQuery, bot: Bot, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    await cb.answer()
    text = manager.tail_log(name, MAX_LOG_LINES)
    await bot.send_message(cb.from_user.id, f"<b>📋 Logs — {esc(name)}</b>\n{pre_block(text)}")


@router.callback_query(F.data.startswith("logf:"))
async def cb_logfile(cb: CallbackQuery, bot: Bot, manager: ScriptManager):
    name = arg(cb)
    p = manager.log_path(name)
    if not manager.exists(name) or not p.exists() or p.stat().st_size == 0:
        await cb.answer("Log khali hai", show_alert=True)
        return
    await cb.answer("📥 Bhej raha hoon…")
    await bot.send_document(cb.from_user.id, FSInputFile(p, filename=f"{name}.log"))


@router.callback_query(F.data.startswith("logc:"))
async def cb_logclear(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    try:
        with open(manager.log_path(name), "wb"):
            pass
    except OSError:
        pass
    await cb.answer("🧹 Log clear ho gaya", show_alert=False)


@router.callback_query(F.data.startswith("get:"))
async def cb_getfile(cb: CallbackQuery, bot: Bot, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    await cb.answer("📄 Bhej raha hoon…")
    await bot.send_document(cb.from_user.id, FSInputFile(manager.script_path(name), filename=f"{name}.py"))


@router.callback_query(F.data.startswith("req:"))
async def cb_req(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    WAITING[cb.from_user.id] = ("req", name)
    await cb.answer()
    await safe_edit(cb.message, req_prompt(name), kb_cancel())


@router.callback_query(F.data.startswith("del:"))
async def cb_del(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    await cb.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        btn("✅ Haan, delete", f"delok:{name}"),
        btn("❌ Nahi", f"s:{name}"),
    ]])
    await safe_edit(
        cb.message,
        f"🗑 <code>{esc(name)}</code> delete karni hai?\nScript, logs aur requirements sab hat jayenge.",
        kb,
    )


@router.callback_query(F.data.startswith("delok:"))
async def cb_delok(cb: CallbackQuery, manager: ScriptManager):
    name = arg(cb)
    if not manager.exists(name):
        await cb.answer("Script nahi mili", show_alert=True)
        return
    await cb.answer("⏳ Deleting…")
    await manager.delete_script(name)
    text, kb = dashboard(manager, f"🗑 <code>{esc(name)}</code> delete ho gayi.")
    await safe_edit(cb.message, text, kb)


# ============================================================
#                      ERROR HANDLER
# ============================================================
async def on_error(event: ErrorEvent) -> bool:
    exc = event.exception
    if isinstance(exc, TelegramBadRequest) and (
        "query is too old" in str(exc).lower() or "not modified" in str(exc).lower()
    ):
        return True
    logging.error("Handler error", exc_info=exc)
    upd = event.update
    try:
        if upd.callback_query:
            await upd.callback_query.answer("⚠️ Error aaya, dobara try karo", show_alert=True)
        elif upd.message:
            await upd.message.answer("⚠️ Kuch gadbad ho gayi, dobara try karo. (/start)")
    except Exception:
        pass
    return True


router.errors.register(on_error)


# ============================================================
#                           MAIN
# ============================================================
def make_notifier(bot: Bot):
    async def notify(text: str, name: str | None = None):
        kb = None
        if name:
            kb = InlineKeyboardMarkup(inline_keyboard=[[
                btn("📋 Logs", f"log:{name}"), btn("📄 Panel", f"s:{name}"),
            ]])
        try:
            await bot.send_message(OWNER_ID, text, reply_markup=kb)
        except Exception as e:
            logging.warning("Could not notify owner: %s", e)
    return notify


async def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    if not BOT_TOKEN:
        logging.error("BOT_TOKEN khali hai. bot.py ke CONFIG me token daalo.")
        sys.exit(1)

    bot = Bot(BOT_TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    try:
        me = await bot.get_me()
    except Exception as e:
        logging.error("Token invalid ya network issue: %s", e)
        await bot.session.close()
        sys.exit(1)

    dp = Dispatcher()
    dp.include_router(router)
    manager = ScriptManager()
    manager.notify = make_notifier(bot)
    psutil.cpu_percent(None)  # prime CPU counter
    logging.info("Bot @%s starting... owner=%s scripts=%d", me.username, OWNER_ID, len(manager.scripts))

    try:
        await bot.set_my_commands([
            BotCommand(command="start", description="Menu + dashboard kholo"),
            BotCommand(command="list", description="Saari scripts"),
            BotCommand(command="status", description="Server status"),
            BotCommand(command="cancel", description="Cancel current action"),
            BotCommand(command="help", description="Help"),
        ])
    except Exception as e:
        logging.warning("set_my_commands failed: %s", e)

    await bot.delete_webhook(drop_pending_updates=True)

    to_autostart = sum(1 for i in manager.scripts.values() if i.was_running)
    try:
        await bot.send_message(
            OWNER_ID,
            "🚀 <b>Bot start ho gaya!</b>\n\n"
            f"📂 Scripts: {len(manager.scripts)}  •  ♻️ Auto-start: {to_autostart}\n"
            "Neeche menu buttons se sab control karo 👇",
            reply_markup=main_menu(),
        )
    except Exception as e:
        logging.warning("Could not send startup message (pehle bot me /start karo): %s", e)

    task = asyncio.create_task(manager.autostart_all())
    manager._bg.add(task)
    task.add_done_callback(manager._bg.discard)

    try:
        await dp.start_polling(bot, manager=manager)
    finally:
        await manager.shutdown()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        pass
