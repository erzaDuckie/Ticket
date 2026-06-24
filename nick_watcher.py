import discord
from discord import ui
import re
import os
import sys
import signal
import secrets
import asyncio
import threading
import json
import logging
from collections import OrderedDict
from datetime import datetime, timezone, timedelta
from flask import Flask, jsonify, request
from flask_cors import CORS
from form_config import FORM_ITEMS, CLAIM_TIERS, FORM_TIMEOUT, get_panel, get_form_items

# ============================================================
#  FIX: Paksa stdout/stderr ke UTF-8 (Windows cp1252 safe)
# ============================================================
if sys.stdout and hasattr(sys.stdout, 'reconfigure'):
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
if sys.stderr and hasattr(sys.stderr, 'reconfigure'):
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')

# ============================================================
#  LOGGING — ke file (/tmp, BUKAN volume) dan konsol sekaligus
#  FIX: _log_file dipindah ke /tmp agar tidak memakan Railway volume.
#  /tmp bersifat sementara (reset tiap redeploy) tapi cukup untuk
#  keperluan ekstensi yang baca log real-time.
# ============================================================
_BASE_DIR = os.path.dirname(os.path.abspath(__file__))
_log_file = "/tmp/nick_watcher.log"  # <-- FIX: pakai /tmp, bukan _BASE_DIR (volume)
from logging.handlers import RotatingFileHandler
# maxBytes=5MB, simpan 3 file backup (nick_watcher.log.1, .2, .3) lalu yang lama dibuang otomatis
_log_max_bytes    = int(os.environ.get("LOG_MAX_BYTES", str(5 * 1024 * 1024)))
_log_backup_count = int(os.environ.get("LOG_BACKUP_COUNT", "3"))
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        RotatingFileHandler(_log_file, maxBytes=_log_max_bytes, backupCount=_log_backup_count, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)
# FIX: Matikan log verbose dari discord.py internal (RESUMED session, dll)
# Log ini sangat banyak dan tidak berguna untuk bot ini
logging.getLogger("discord").setLevel(logging.WARNING)
logging.getLogger("discord.gateway").setLevel(logging.WARNING)
logging.getLogger("discord.http").setLevel(logging.WARNING)

# ============================================================
#  LOAD CONFIG
# ============================================================
def _load_env(path="config.env"):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                k, _, v = line.partition("=")
                os.environ.setdefault(k.strip(), v.strip())

_load_env()

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN tidak ditemukan!\n"
        "Buat file config.env dan isi:\n"
        "  BOT_TOKEN=token_discord_kamu\n"
        "  FLASK_SECRET=<string random>\n"
    )

FLASK_SECRET = os.environ.get("FLASK_SECRET", "")
if not FLASK_SECRET:
    FLASK_SECRET = secrets.token_hex(32)
    log.warning(f"[WARN] FLASK_SECRET belum diset — generated sementara: FLASK_SECRET={FLASK_SECRET}")

CLAIM_CHANNEL    = int(os.environ.get("CLAIM_CHANNEL", "0"))
LOG_CHANNEL_HIGH = int(os.environ.get("LOG_CHANNEL_HIGH", "0"))
LOG_CHANNEL_MED  = int(os.environ.get("LOG_CHANNEL_MED", "0"))

_bl_raw       = os.environ.get("BLACKLIST_IDS", "")
BLACKLIST_IDS = set(int(x.strip()) for x in _bl_raw.split(",") if x.strip().isdigit())

_wl_raw       = os.environ.get("WHITELIST_IDS", "")
WHITELIST_IDS = set(int(x.strip()) for x in _wl_raw.split(",") if x.strip().isdigit())

MAX_CLAIM  = int(os.environ.get("MAX_CLAIM", "5"))
FLASK_PORT = int(os.environ.get("FLASK_PORT", "5678"))
GUILD_ID   = int(os.environ.get("GUILD_ID", "0"))

AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", "60"))

# ============================================================
#  DATA DIR — persistent volume Railway di-mount ke /data
# ============================================================
DATA_DIR = os.environ.get("DATA_DIR", "/data")
try:
    os.makedirs(DATA_DIR, exist_ok=True)
    # test write permission
    _test_path = os.path.join(DATA_DIR, ".write_test")
    with open(_test_path, "w") as _tf:
        _tf.write("ok")
    os.remove(_test_path)
    log.info(f"[DATA] Menggunakan DATA_DIR: {DATA_DIR}")
except Exception as _e:
    # fallback ke direktori script jika /data tidak bisa ditulis
    DATA_DIR = _BASE_DIR
    log.warning(f"[DATA] /data tidak dapat ditulis ({_e}), fallback ke: {DATA_DIR}")

def _data_path(filename: str) -> str:
    return os.path.join(DATA_DIR, filename)

def _safe_save(filepath: str, data, *, is_json=True, json_kwargs=None):
    """
    Write langsung ke filepath (tanpa atomic tmp) untuk menghindari
    cross-device os.replace error pada Railway persistent volume.
    Railway volume sudah di-mount dengan fsync guarantee, jadi
    direct write sudah cukup aman untuk use-case ini.
    """
    import tempfile, shutil
    # Coba atomic: tulis ke /tmp (bukan di /data) lalu copy ke target
    try:
        fd, tmp_path = tempfile.mkstemp(prefix="nw_", suffix=".tmp", dir="/tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                if is_json:
                    json.dump(data, f, **(json_kwargs or {"indent": 2}))
                else:
                    f.write(data)
            shutil.copy2(tmp_path, filepath)
        finally:
            try:
                os.remove(tmp_path)
            except Exception:
                pass
        return
    except Exception as e:
        log.debug(f"[SAVE] /tmp atomic gagal ({e}), direct write: {filepath}")

    # Fallback: direct write langsung ke filepath
    try:
        with open(filepath, "w", encoding="utf-8") as f:
            if is_json:
                json.dump(data, f, **(json_kwargs or {"indent": 2}))
            else:
                f.write(data)
    except Exception as e2:
        log.error(f"[SAVE] Gagal simpan {filepath}: {e2}")

# ============================================================
#  FILE PATHS — semua persistent data ke DATA_DIR
# ============================================================
DM_NICK_FILE      = _data_path("dm_nick_counts.json")
CLAIMS_FILE       = _data_path("claims.json")
PENDING_FILE      = _data_path("pending_queue.json")
PROCESSED_FILE    = _data_path("processed_queue.json")
GM_REGISTRY_FILE  = _data_path("gm_registry.json")
PANEL_FILE        = _data_path("panel_msg.json")
REQUIREMENTS_FILE = os.path.join(_BASE_DIR, "DM.json")  # config statis, di source

# ============================================================
#  DM NICK COUNT — batasi berapa kali DM dikirim per nick
# ============================================================
DM_NICK_LIMIT    = int(os.environ.get("DM_NICK_LIMIT", "2"))
# Auto-bersih entry dm_nick_counts yang sudah lebih tua dari sekian hari (0 = nonaktif)
DM_NICK_TTL_DAYS = int(os.environ.get("DM_NICK_TTL_DAYS", "30"))
# Interval pengecekan cleanup berkala (jam)
DM_NICK_CLEANUP_INTERVAL_HOURS = int(os.environ.get("DM_NICK_CLEANUP_INTERVAL_HOURS", "24"))

def _load_dm_nick_counts() -> dict:
    try:
        with open(DM_NICK_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}
    # Migrasi format lama {"nick:id": 2} -> {"nick:id": {"count": 2, "ts": "<iso>"}}
    # Entry lama tanpa timestamp dikasih ts=sekarang supaya tidak langsung kebuang
    # saat cleanup pertama kali jalan setelah update ini.
    now_iso  = datetime.now(timezone.utc).isoformat()
    migrated = {}
    for k, v in raw.items():
        if isinstance(v, dict):
            migrated[k] = {"count": v.get("count", 0), "ts": v.get("ts") or now_iso}
        else:
            migrated[k] = {"count": v, "ts": now_iso}
    return migrated

def _save_dm_nick_counts():
    _safe_save(DM_NICK_FILE, dm_nick_counts)

def _cleanup_dm_nick_counts() -> int:
    """Hapus entry dm_nick_counts yang sudah lebih tua dari DM_NICK_TTL_DAYS.
    Return jumlah entry yang dihapus."""
    if DM_NICK_TTL_DAYS <= 0:
        return 0
    cutoff  = datetime.now(timezone.utc) - timedelta(days=DM_NICK_TTL_DAYS)
    removed = 0
    with lock:
        for k in list(dm_nick_counts.keys()):
            ts_raw = dm_nick_counts[k].get("ts")
            try:
                ts = datetime.fromisoformat(ts_raw) if ts_raw else None
            except (TypeError, ValueError):
                ts = None
            if ts is None or ts < cutoff:
                del dm_nick_counts[k]
                removed += 1
        if removed:
            _save_dm_nick_counts()
    return removed

dm_nick_counts: dict = _load_dm_nick_counts()

# ============================================================
#  REQUIREMENTS & DM TEMPLATE — dibaca dari DM.json
# ============================================================
def _load_requirements():
    try:
        with open(REQUIREMENTS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        log.warning("[WARN] DM.json tidak ditemukan — fitur DM dinonaktifkan.")
        return {}
    except json.JSONDecodeError as e:
        log.warning(f"[WARN] DM.json tidak valid: {e} — fitur DM dinonaktifkan.")
        return {}

REQUIREMENTS = _load_requirements()

# ============================================================
#  ADMIN IDS
# ============================================================
_OWNER_ID  = 334621293125304331  # JANGAN HAPUS — owner permanen
_admin_raw = os.environ.get("ADMIN_IDS", "")
ADMIN_IDS  = set(int(x.strip()) for x in _admin_raw.split(",") if x.strip().isdigit())
ADMIN_IDS.add(_OWNER_ID)

# ============================================================
#  EMOJI
# ============================================================
EMOJI_SUCCESS = "\u2705"        # ✅
EMOJI_FAILED  = "\u274c"        # ❌
EMOJI_SKIP    = "\u23ed\ufe0f"  # ⏭️
EMOJI_BLOCK   = "\U0001f6ab"    # 🚫

# ============================================================
#  PERSISTENT CLAIMS
# ============================================================
def _load_claims():
    try:
        with open(CLAIMS_FILE, "r") as f:
            data = json.load(f)
            high   = {int(k): v for k, v in data.get("high", {}).items()}
            medium = {int(k): v for k, v in data.get("medium", {}).items()}
            return high, medium
    except (FileNotFoundError, json.JSONDecodeError):
        return {}, {}

def _save_claims():
    _safe_save(CLAIMS_FILE, {
        "high":   {str(k): v for k, v in claim_counts_high.items()},
        "medium": {str(k): v for k, v in claim_counts_medium.items()},
    })

claim_counts_high, claim_counts_medium = _load_claims()

# ============================================================
#  PERSISTENSI ANTRIAN
# ============================================================
def _save_pending():
    _safe_save(PENDING_FILE, {"high": pending_high, "medium": pending_medium})

def _load_pending():
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            raw_high   = data.get("high",   [])
            raw_medium = data.get("medium", [])
            high   = [e for e in raw_high   if e.get("nick", "").strip()]
            medium = [e for e in raw_medium if e.get("nick", "").strip()]
            purged = (len(raw_high) - len(high)) + (len(raw_medium) - len(medium))
            if purged:
                log.warning(f"[LOAD-PURGE] {purged} entry nick kosong dibuang")
            return high, medium
    except FileNotFoundError:
        return [], []
    except (json.JSONDecodeError, KeyError) as e:
        log.warning(f"[WARN] pending_queue.json rusak ({e})")
        return [], []

def _save_processed():
    _safe_save(PROCESSED_FILE, {
        "high":   list(processed_high.keys())[-MAX_PROCESSED:],
        "medium": list(processed_medium.keys())[-MAX_PROCESSED:],
    })

def _load_processed() -> tuple[OrderedDict, OrderedDict]:
    try:
        with open(PROCESSED_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            ph = OrderedDict((k, None) for k in data.get("high", []))
            pm = OrderedDict((k, None) for k in data.get("medium", []))
            return ph, pm
    except FileNotFoundError:
        return OrderedDict(), OrderedDict()
    except (json.JSONDecodeError, KeyError) as e:
        log.warning(f"[WARN] processed_queue.json rusak ({e})")
        return OrderedDict(), OrderedDict()

MAX_PROCESSED = 2000
_saved_high, _saved_medium = _load_pending()
pending_high     = _saved_high
pending_medium   = _saved_medium
processed_high, processed_medium = _load_processed()
log_msg_map      = {}

if processed_high or processed_medium:
    log.info(f"[PROCESSED] Loaded — high: {len(processed_high)}, medium: {len(processed_medium)}")
if pending_high or pending_medium:
    log.info(f"[QUEUE] Loaded — high: {len(pending_high)}, medium: {len(pending_medium)}")

lock           = threading.Lock()
_bot_loop      = None
_shutting_down = False

# ============================================================
#  IN-PROGRESS TABLE
# ============================================================
CLAIM_TIMEOUT_SECONDS = int(os.environ.get("CLAIM_TIMEOUT_SECONDS", "300"))
in_progress_high   = {}
in_progress_medium = {}

def _make_ip_key(entry: dict) -> str:
    return str(entry.get("log_msg_id") or f"{entry['nick']}:{entry['author_id']}")

def _cleanup_expired_inprogress():
    now = datetime.now(timezone.utc)
    for table, queue in ((in_progress_high, pending_high), (in_progress_medium, pending_medium)):
        expired_keys = [k for k, v in table.items() if (now - v["taken_at"]).total_seconds() > CLAIM_TIMEOUT_SECONDS]
        for k in expired_keys:
            info  = table.pop(k)
            entry = info["entry"]
            nick  = entry.get("nick", "").strip()
            if not nick:
                try:
                    queue.remove(entry)
                except ValueError:
                    pass
                log.warning(f"[TAKE-EXPIRE-DISCARD] key={k} nick kosong dibuang")
                _save_pending()
                continue
            queue.insert(0, entry)
            log.info(f"[TAKE-EXPIRE] key={k} nick={nick} dikembalikan ke antrian")

def _trim_processed(od: OrderedDict):
    while len(od) > MAX_PROCESSED:
        od.popitem(last=False)

# ============================================================
#  GM REGISTRY
# ============================================================
GM_ONLINE_THRESHOLD_SECONDS = 15

def _load_gm_registry() -> dict:
    try:
        with open(GM_REGISTRY_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            for v in data.values():
                v["commands"] = []
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_gm_registry():
    _safe_save(GM_REGISTRY_FILE, {
        gid: {
            "name":      v.get("name", ""),
            "last_seen": v.get("last_seen", ""),
            "channel":   v.get("channel", ""),
        }
        for gid, v in gm_registry.items()
    })

gm_registry: dict = _load_gm_registry()

def _touch_gm(gm_id: str, gm_name: str, channel: str):
    if not gm_id or gm_id == "unknown":
        return
    with lock:
        entry = gm_registry.setdefault(gm_id, {"commands": []})
        if gm_name:
            entry["name"] = gm_name
        entry["channel"] = channel
        entry["last_seen"] = datetime.now(timezone.utc).isoformat()
    _save_gm_registry()

def _gm_display(gm_id: str) -> str:
    entry = gm_registry.get(gm_id)
    if entry and entry.get("name"):
        return entry["name"]
    return gm_id

def _find_gm_id(target: str):
    if target in gm_registry:
        return target
    target_l = target.strip().lower()
    for gid, v in gm_registry.items():
        if v.get("name", "").strip().lower() == target_l:
            return gid
    return None

def _pop_gm_commands(gm_id: str) -> list:
    if not gm_id or gm_id == "unknown":
        return []
    with lock:
        entry = gm_registry.get(gm_id)
        if not entry or not entry.get("commands"):
            return []
        cmds = entry["commands"]
        entry["commands"] = []
    _save_gm_registry()
    return cmds

def _queue_gm_command(gm_id: str, command: str):
    with lock:
        entry = gm_registry.setdefault(gm_id, {"commands": []})
        entry.setdefault("commands", []).append(command)

# ============================================================
#  HELPER FUNCTIONS
# ============================================================
async def _auto_delete(msg, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
    except Exception:
        pass

async def _send_to_channel(channel_id: int, message: str):
    if not channel_id:
        return
    try:
        ch = client.get_channel(channel_id)
        if ch is None:
            ch = await client.fetch_channel(channel_id)
        if ch:
            await ch.send(message)
    except Exception as e:
        log.warning(f"[WARN] Gagal kirim pesan: {e}")

async def _send_embed_to_channel(channel_id: int, embed: discord.Embed) -> discord.Message | None:
    if not channel_id:
        return None
    try:
        ch = client.get_channel(channel_id)
        if ch is None:
            ch = await client.fetch_channel(channel_id)
        if ch:
            return await ch.send(embed=embed)
    except Exception as e:
        log.warning(f"[WARN] Gagal kirim embed: {e}")
    return None

def schedule_reaction(channel_id: int, message_id: int, emoji: str):
    async def _do():
        for attempt in range(3):
            try:
                ch = client.get_channel(channel_id)
                if ch is None:
                    ch = await client.fetch_channel(channel_id)
                msg = await ch.fetch_message(message_id)
                await msg.add_reaction(emoji)
                return
            except Exception:
                if attempt < 2:
                    await asyncio.sleep(2)
    if _bot_loop is None or _bot_loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_do(), _bot_loop)

def _build_dm_message(channel_key: str, status: str, nick: str, mention: str, channel_name: str = "") -> str | None:
    cfg = REQUIREMENTS.get(channel_key, {})
    if not cfg:
        return None
    template_map = {
        "not_req":  cfg.get("dm_not_req", ""),
        "already":  cfg.get("dm_already", ""),
        "success":  cfg.get("dm_success", ""),
        "not_found": cfg.get("dm_not_found", ""),
    }
    template = template_map.get(status)
    if not template:
        return None
    placeholders = {k: str(v) for k, v in cfg.items()}
    placeholders.update({"nick": nick, "mention": mention, "channel_name": channel_name})
    try:
        return template.format_map(placeholders)
    except KeyError as e:
        log.warning(f"[DM-WARN] Placeholder {e} tidak ada")
        return template

def schedule_dm(author_id: int, log_ch_id: int, channel_key: str, status: str, nick: str):
    if status in ("not_req", "already"):
        dm_key = f"{nick.lower()}:{author_id}"
        with lock:
            entry = dm_nick_counts.get(dm_key, {"count": 0, "ts": None})
            count = entry.get("count", 0)
            if DM_NICK_LIMIT > 0 and count >= DM_NICK_LIMIT:
                log.info(f"[DM-SKIP] Limit {DM_NICK_LIMIT}x untuk {dm_key}")
                return
            dm_nick_counts[dm_key] = {
                "count": count + 1,
                "ts": datetime.now(timezone.utc).isoformat(),
            }
            _save_dm_nick_counts()
    async def _do():
        try:
            user = client.get_user(author_id)
            if user is None:
                user = await client.fetch_user(author_id)
            mention = user.mention
            channel_name = ""
            if log_ch_id:
                try:
                    ch = client.get_channel(log_ch_id)
                    if ch is None:
                        ch = await client.fetch_channel(log_ch_id)
                    channel_name = ch.name if ch else ""
                except Exception:
                    pass
            msg_text = _build_dm_message(channel_key, status, nick, mention, channel_name)
            if not msg_text:
                return
            await user.send(msg_text)
        except discord.Forbidden:
            log.info(f"[DM-SKIP] User {author_id} menutup DM")
        except Exception as e:
            log.warning(f"[DM-ERROR] {e}")
    if _bot_loop is None or _bot_loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_do(), _bot_loop)

async def _graceful_shutdown(reason: str = "dimatikan"):
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log.info(f"[BOT] Shutdown: {reason}")
    with lock:
        _save_pending()
        _save_processed()
    await client.close()

def _build_claim_embed(nick: str, tier_label: str, panel: str, choices: dict, author: discord.Member) -> discord.Embed:
    color = discord.Color.gold() if panel == "high" else discord.Color.blue()
    embed = discord.Embed(title=f"\U0001f3ab Claim — {nick}", description=f"**{tier_label}**", color=color)
    tier_items = get_form_items(tier_label)
    if tier_items:
        for item in tier_items:
            val = choices.get(item["key"]) or "—"
            embed.add_field(name=item["label"], value=val, inline=False)
    else:
        embed.add_field(name="Reward", value="Sesuai paket", inline=False)
    embed.set_footer(text=f"by {author.display_name} ({author.id})")
    return embed

# ============================================================
#  DISCORD UI COMPONENTS
# ============================================================
class ItemSelect(ui.Select):
    def __init__(self, item: dict, wizard: "ClaimWizard"):
        self.item_key = item["key"]
        self.wizard   = wizard
        options = [discord.SelectOption(label=opt, value=opt) for opt in item["options"]]
        super().__init__(placeholder=f"Pilih {item['label']}...", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        try:
            self.wizard.choices[self.item_key] = self.values[0]
            await self.wizard.advance(interaction)
        except Exception as e:
            log.warning(f"[ERROR-ITEM-SELECT] {e}")
            await interaction.response.send_message("❌ Terjadi kesalahan.", ephemeral=True)

class ItemSelectView(ui.View):
    def __init__(self, item: dict, wizard: "ClaimWizard"):
        super().__init__(timeout=FORM_TIMEOUT)
        self.wizard = wizard
        self.add_item(ItemSelect(item, wizard))

    async def on_timeout(self):
        await self.wizard.on_timeout()

    @ui.button(label="✏️ Mulai Ulang", style=discord.ButtonStyle.secondary, row=1)
    async def restart_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.restart(interaction)

    @ui.button(label="❌ Batal", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.cancel(interaction)

class ConfirmView(ui.View):
    def __init__(self, wizard: "ClaimWizard"):
        super().__init__(timeout=FORM_TIMEOUT)
        self.wizard = wizard

    async def on_timeout(self):
        await self.wizard.on_timeout()

    @ui.button(label="✅ Konfirmasi & Submit", style=discord.ButtonStyle.success)
    async def confirm_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.submit(interaction)

    @ui.button(label="✏️ Mulai Ulang", style=discord.ButtonStyle.secondary)
    async def restart_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.restart(interaction)

    @ui.button(label="❌ Batal", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.cancel(interaction)

class CancelWizardView(ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id

    @ui.button(label="🔄 Lanjutkan Form", style=discord.ButtonStyle.primary)
    async def resume_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Bukan form kamu.", ephemeral=True)
            return
        wizard = _active_wizards.get(self.user_id)
        if not wizard or wizard._submitted:
            await interaction.response.edit_message(content="⚠️ Form tidak aktif.", view=None)
            _active_wizards.pop(self.user_id, None)
            return
        self.stop()
        if not wizard.nick or not wizard.tier:
            modal = NickTierModal(wizard)
            await interaction.response.send_modal(modal)
            return
        form_items = wizard.active_form_items
        next_step = 0
        for i, item in enumerate(form_items):
            if item["key"] not in wizard.choices:
                next_step = i
                break
        else:
            next_step = len(form_items)
        wizard._resp_msg = None
        if next_step >= len(form_items):
            await wizard._show_summary(interaction)
        else:
            wizard._step = next_step + 2
            await wizard._show_item_step(interaction)

    @ui.button(label="🗑️ Batalkan Form Lama", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Bukan form kamu.", ephemeral=True)
            return
        wizard = _active_wizards.pop(self.user_id, None)
        if wizard:
            wizard._submitted = True
        self.stop()
        await interaction.response.edit_message(content="✅ Form lama dibatalkan.", view=None)

class NickTierModal(ui.Modal, title="Claim RF Online"):
    nick_input = ui.TextInput(label="Nick in-game", placeholder="Masukkan nick kamu...", min_length=1, max_length=20)

    def __init__(self, wizard: "ClaimWizard"):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        self.wizard._modal_submitted = True
        nick = self.nick_input.value.strip()
        if not nick:
            await interaction.response.send_message("❌ Nick tidak boleh kosong.", ephemeral=True)
            return
        self.wizard.nick = nick
        if self.wizard.tier:
            await self.wizard.start_item_steps(interaction)
        else:
            await self.wizard.show_tier_step(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.warning(f"[ERROR-MODAL] {error}")
        _active_wizards.pop(self.wizard.user.id, None)
        await interaction.response.send_message("❌ Terjadi kesalahan. Pilih paket lagi.", ephemeral=True)

class TierSelect(ui.Select):
    def __init__(self, wizard: "ClaimWizard"):
        self.wizard = wizard
        options = [discord.SelectOption(label=t["label"], value=t["label"], emoji=t.get("emoji", "🎫")) for t in CLAIM_TIERS]
        super().__init__(placeholder="Pilih paket claim...", options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            self.wizard.tier = self.values[0]
            await self.wizard.advance(interaction)
        except Exception as e:
            log.warning(f"[ERROR-TIER-SELECT] {e}")
            await interaction.response.send_message("❌ Terjadi kesalahan.", ephemeral=True)

class TierSelectView(ui.View):
    def __init__(self, wizard: "ClaimWizard"):
        super().__init__(timeout=FORM_TIMEOUT)
        self.wizard = wizard
        self.add_item(TierSelect(wizard))

    async def on_timeout(self):
        await self.wizard.on_timeout()

    @ui.button(label="✏️ Mulai Ulang", style=discord.ButtonStyle.secondary, row=1)
    async def restart_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.restart(interaction)

    @ui.button(label="❌ Batal", style=discord.ButtonStyle.danger, row=1)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        await self.wizard.cancel(interaction)

class ClaimWizard:
    def __init__(self, user: discord.Member, original_message: discord.Message | None = None):
        self.user = user
        self.original_message = original_message
        self.nick = ""
        self.tier = ""
        self.choices = {}
        self._step = 0
        self._resp_msg = None
        self._cancelled = False
        self._submitted = False
        self._modal_submitted = False

    async def start(self, interaction: discord.Interaction):
        modal = NickTierModal(self)
        await interaction.response.send_modal(modal)
        asyncio.create_task(self._modal_cleanup_guard())

    async def start_item_steps(self, interaction: discord.Interaction):
        self._step = 2
        if self.active_form_items:
            await self._show_item_step(interaction)
        else:
            await self._show_summary(interaction)

    async def _modal_cleanup_guard(self):
        await asyncio.sleep(180)
        if not self._modal_submitted and not self._submitted and not self._cancelled:
            log.info(f"[WIZARD] Modal timeout untuk {self.user.id}")
            _active_wizards.pop(self.user.id, None)

    async def show_tier_step(self, interaction: discord.Interaction):
        view = TierSelectView(self)
        total = len(FORM_ITEMS) + 1 if FORM_ITEMS else 1
        content = f"**🎫 Claim — Step 1/{total}**\n`{self.nick}` — Pilih tier:"
        if self._resp_msg is None:
            await interaction.response.send_message(content, view=view, ephemeral=True)
            self._resp_msg = await interaction.original_response()
        else:
            await interaction.response.edit_message(content=content, view=view)

    async def advance(self, interaction: discord.Interaction):
        if self.tier and not self._item_steps_started():
            if self.active_form_items:
                self._step = 2
                await self._show_item_step(interaction)
            else:
                self._step = 2
                await self._show_summary(interaction)
            return
        if self._step >= 2:
            self._step += 1
            item_idx = self._step - 2
            if item_idx < len(self.active_form_items):
                await self._show_item_step(interaction)
            else:
                await self._show_summary(interaction)

    def _item_steps_started(self) -> bool:
        return self._step >= 2

    @property
    def active_form_items(self) -> list:
        return get_form_items(self.tier)

    async def _show_item_step(self, interaction: discord.Interaction):
        item_idx = self._step - 2
        item = self.active_form_items[item_idx]
        total = len(self.active_form_items) + 1
        step_num = self._step
        view = ItemSelectView(item, self)
        content = f"**🎫 Claim — Step {step_num}/{total}**\n`{self.nick}` [{self.tier}]\n\n**{item['label']}**"
        if self._resp_msg is None:
            await interaction.response.send_message(content, view=view, ephemeral=True)
            self._resp_msg = await interaction.original_response()
        else:
            await interaction.response.edit_message(content=content, view=view)

    async def _show_summary(self, interaction: discord.Interaction):
        lines = [f"**🎫 Summary Claim**\n`{self.nick}` [{self.tier}]\n"]
        tier_items = self.active_form_items
        if tier_items:
            for item in tier_items:
                val = self.choices.get(item["key"]) or "—"
                lines.append(f"• **{item['label']}**: {val}")
        else:
            lines.append("• Reward sesuai paket")
        lines.append("\nSudah benar? Klik **Konfirmasi** untuk submit.")
        content = "\n".join(lines)
        view = ConfirmView(self)
        if self._resp_msg is None:
            await interaction.response.send_message(content, view=view, ephemeral=True)
            self._resp_msg = await interaction.original_response()
        else:
            await interaction.response.edit_message(content=content, view=view)

    async def submit(self, interaction: discord.Interaction):
        if self._submitted:
            return
        self._submitted = True
        if self._resp_msg is not None:
            await interaction.response.edit_message(content=f"⏳ Memproses claim `{self.nick}` [{self.tier}]...", view=None)
        else:
            await interaction.response.send_message(content=f"⏳ Memproses claim `{self.nick}` [{self.tier}]...", ephemeral=True)
            self._resp_msg = await interaction.original_response()
        tier_lower = get_panel(self.tier)
        counts    = claim_counts_high if tier_lower == "high" else claim_counts_medium
        queue     = pending_high if tier_lower == "high" else pending_medium
        processed = processed_high if tier_lower == "high" else processed_medium
        if self.user.id not in WHITELIST_IDS:
            if MAX_CLAIM > 0 and counts.get(self.user.id, 0) >= MAX_CLAIM:
                await interaction.edit_original_response(content=f"⛔ Kamu sudah mencapai batas maksimal **{MAX_CLAIM}** claim ({self.tier}).")
                return
        log_ch_id = LOG_CHANNEL_HIGH if tier_lower == "high" else LOG_CHANNEL_MED
        embed = _build_claim_embed(self.nick, self.tier, tier_lower, self.choices, self.user)
        log_msg = await _send_embed_to_channel(log_ch_id, embed)
        log_msg_id = log_msg.id if log_msg else None
        entry = {
            "nick": self.nick, "tier_label": self.tier, "tier": tier_lower,
            "choices": self.choices, "author_id": self.user.id,
            "log_msg_id": log_msg_id, "log_ch_id": log_ch_id,
        }
        ip_key = _make_ip_key(entry)
        with lock:
            already_pending    = any(_make_ip_key(e) == ip_key for e in queue)
            already_processed  = ip_key in processed
            already_inprogress = ip_key in (in_progress_high if tier_lower == "high" else in_progress_medium)
            if already_pending or already_processed or already_inprogress:
                await interaction.edit_original_response(content=f"⚠️ Claim `{self.nick}` sudah ada di antrian.")
                _active_wizards.pop(self.user.id, None)
                return
            if not entry.get("nick", "").strip():
                await interaction.edit_original_response(content="❌ Nick tidak boleh kosong.")
                _active_wizards.pop(self.user.id, None)
                return
            queue.append(entry)
            _save_pending()
        log.info(f"[SUBMIT-{tier_lower.upper()}] + {self.nick} | author: {self.user.id}")
        await interaction.edit_original_response(content=f"✅ Claim `{self.nick}` [{self.tier}] berhasil disubmit!")
        _active_wizards.pop(self.user.id, None)

    async def restart(self, interaction: discord.Interaction):
        self.nick = ""
        self.tier = ""
        self.choices = {}
        self._step = 0
        self._modal_submitted = False
        modal = NickTierModal(self)
        await interaction.response.send_modal(modal)

    async def cancel(self, interaction: discord.Interaction):
        if self._cancelled:
            return
        self._cancelled = True
        _active_wizards.pop(self.user.id, None)
        await interaction.response.edit_message(content="❌ Claim dibatalkan.", view=None)

    async def on_timeout(self):
        if self._submitted or self._cancelled:
            return
        _active_wizards.pop(self.user.id, None)
        if self._resp_msg:
            try:
                await self._resp_msg.edit(content="⏱️ Form claim kadaluarsa.", view=None)
            except Exception:
                pass

_active_wizards: dict[int, ClaimWizard] = {}

# ============================================================
#  PANEL PERSISTENT
# ============================================================
def _load_panel_msg_id() -> int | None:
    try:
        with open(PANEL_FILE, "r") as f:
            data = json.load(f)
            return data.get("message_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _save_panel_msg_id(msg_id: int):
    _safe_save(PANEL_FILE, {"message_id": msg_id})

_panel_msg_id: int | None = _load_panel_msg_id()

def _build_panel_embed() -> discord.Embed:
    try:
        from panel_channel_msg import _build_panel_embed as _ext
        return _ext()
    except ImportError:
        pass
    embed = discord.Embed(
        title="🎫 Claim Center RF Online",
        description=(
            "**Cara Penggunaan:**\n"
            "• Pilih paket claim dari dropdown di bawah\n"
            "• Isi form yang muncul (hanya kamu yang bisa lihat)\n"
            "• Submit dan tunggu admin memproses claimmu\n\n"
            "**⚠️ Peringatan:**\n"
            "• Setiap akun hanya boleh claim sesuai batas yang ditentukan\n"
            "• Pastikan nick in-game yang kamu masukkan sudah benar\n"
            "• Penyalahgunaan akan dikenakan sanksi"
        ),
        color=discord.Color.gold()
    )
    embed.set_footer(text="Klik dropdown di bawah untuk memulai claim")
    return embed

class PanelTierSelect(ui.Select):
    def __init__(self):
        options = [discord.SelectOption(label=t["label"], value=t["label"], emoji=t.get("emoji", "🎫")) for t in CLAIM_TIERS]
        super().__init__(placeholder="Pilih paket claim...", options=options, custom_id="panel_tier_select")

    async def callback(self, interaction: discord.Interaction):
        tier_label = self.values[0]
        self.values.clear()
        try:
            if interaction.user.id in BLACKLIST_IDS:
                await interaction.response.send_message("🚫 Kamu tidak dapat menggunakan fitur ini.", ephemeral=True)
                await self._reset_panel(interaction)
                return
            if interaction.user.id in _active_wizards:
                await interaction.response.send_message("⚠️ Kamu sudah punya form claim aktif.", view=CancelWizardView(interaction.user.id), ephemeral=True)
                await self._reset_panel(interaction)
                return
            wizard = ClaimWizard(user=interaction.user)
            wizard.tier = tier_label
            _active_wizards[interaction.user.id] = wizard
            modal = NickTierModal(wizard)
            await interaction.response.send_modal(modal)
            asyncio.create_task(wizard._modal_cleanup_guard())
            await self._reset_panel(interaction)
        except discord.NotFound:
            log.debug(f"[PANEL-SELECT] Interaction expired")
            _active_wizards.pop(interaction.user.id, None)
        except discord.HTTPException as e:
            log.warning(f"[PANEL-SELECT] HTTP error: {e}")
            _active_wizards.pop(interaction.user.id, None)

    async def _reset_panel(self, interaction: discord.Interaction):
        try:
            if interaction.response.is_done():
                await interaction.followup.send("\u200b", ephemeral=True, delete_after=0)
            else:
                await interaction.response.defer(ephemeral=True)
        except Exception as e:
            log.debug(f"[PANEL-RESET] {e}")

class PanelView(ui.View):
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelTierSelect())

async def _setup_persistent_panel():
    global _panel_msg_id
    if not CLAIM_CHANNEL:
        log.warning("[PANEL] CLAIM_CHANNEL belum diset")
        return
    try:
        ch = client.get_channel(CLAIM_CHANNEL)
        if ch is None:
            ch = await client.fetch_channel(CLAIM_CHANNEL)
    except Exception as e:
        log.warning(f"[PANEL] Gagal fetch claim channel: {e}")
        return

    found_msg = None
    try:
        async for msg in ch.history(limit=200):
            if msg.author != client.user:
                continue
            if msg.components:
                for row in msg.components:
                    for component in row.children:
                        if hasattr(component, 'custom_id') and component.custom_id == "panel_tier_select":
                            found_msg = msg
                            break
                    if found_msg:
                        break
            if found_msg:
                break
    except Exception as e:
        log.warning(f"[PANEL] Gagal mencari panel existing: {e}")

    view = PanelView()
    if found_msg:
        try:
            await found_msg.edit(embed=_build_panel_embed(), view=view)
            client.add_view(view, message_id=found_msg.id)
            _panel_msg_id = found_msg.id
            _save_panel_msg_id(found_msg.id)
            log.info(f"[PANEL] Panel existing ditemukan dan diperbarui (id={_panel_msg_id})")
            return
        except Exception as e:
            log.warning(f"[PANEL] Gagal edit panel existing: {e}")

    try:
        msg = await ch.send(embed=_build_panel_embed(), view=view)
        _panel_msg_id = msg.id
        client.add_view(view, message_id=_panel_msg_id)
        _save_panel_msg_id(msg.id)
        log.info(f"[PANEL] Panel baru dibuat (id={_panel_msg_id})")
    except Exception as e:
        log.warning(f"[PANEL] Gagal post panel: {e}")

# ============================================================
#  FLASK SERVER
# ============================================================
app = Flask(__name__)
CORS(app, origins=["*"], allow_headers=["Content-Type", "X-GCP-Token"], methods=["GET", "POST", "OPTIONS"])

def require_secret():
    token = request.headers.get("X-GCP-Token", "")
    if not secrets.compare_digest(token, FLASK_SECRET):
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    return None

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "ok",
        "bot_ready": _bot_loop is not None,
        "data_dir": DATA_DIR,
        "pending_high": len(pending_high),
        "pending_medium": len(pending_medium),
    }), 200

def _fmt_entry(e: dict) -> dict:
    return {
        "nick":       e["nick"],
        "tier_label": e.get("tier_label", ""),
        "tier":       e["tier"],
        "choices":    e["choices"],
        "log_msg_id": str(e["log_msg_id"]) if e.get("log_msg_id") else None,
        "log_ch_id":  str(e["log_ch_id"]) if e.get("log_ch_id") else None,
        "gm_id":      e.get("gm_id"),
    }

def _process_result(queue: list, in_progress: dict, processed: OrderedDict, counts: dict, tier_label: str, data: dict):
    nick    = data.get("nick", "").strip()
    status  = data.get("status", "failed")
    gm_id   = data.get("gm_id", "unknown")
    gm_name = data.get("gm_name", "")
    ip_key  = data.get("ip_key", "").strip()
    _touch_gm(gm_id, gm_name, "high" if tier_label == "HIGH" else "medium")
    log_ch_id = log_msg_id = None
    entry = None
    if not nick and not ip_key:
        log.warning(f"[WARN-{tier_label}] nick dan ip_key kosong")
        return None, None, None, status
    with lock:
        if ip_key and ip_key in in_progress:
            info  = in_progress.pop(ip_key)
            entry = info["entry"]
        else:
            for k, v in list(in_progress.items()):
                if v["entry"]["nick"] == nick:
                    in_progress.pop(k)
                    entry = v["entry"]
                    ip_key = k
                    break
        if entry:
            try:
                queue.remove(entry)
            except ValueError:
                pass
            log_ch_id  = entry.get("log_ch_id")
            log_msg_id = entry.get("log_msg_id")
            author_id  = entry["author_id"]
            log.info(f"[RESULT-{tier_label}] {nick} -> {status} (gm: {_gm_display(gm_id)})")
            if status == "success":
                if author_id not in WHITELIST_IDS:
                    counts[author_id] = counts.get(author_id, 0) + 1
                    _save_claims()
            if ip_key:
                processed[ip_key] = None
                _trim_processed(processed)
            _save_pending()
            _save_processed()
        else:
            log.warning(f"[WARN-{tier_label}] entry tidak ditemukan: nick={nick}")
    if entry and status in ("not_req", "already", "success", "not_found"):
        channel_key = "high" if tier_label == "HIGH" else "medium"
        schedule_dm(entry["author_id"], log_ch_id or 0, channel_key, status, nick)
    return entry, log_ch_id, log_msg_id, status

# ── HIGH endpoints ───────────────────────────────────────────
@app.route('/pending/take', methods=['POST'])
def take_pending_high():
    err = require_secret()
    if err: return err
    body    = request.json or {}
    gm_id   = body.get("gm_id", "unknown")
    gm_name = body.get("gm_name", "")
    _touch_gm(gm_id, gm_name, "high")
    commands = _pop_gm_commands(gm_id)
    with lock:
        _cleanup_expired_inprogress()
        if not pending_high:
            return jsonify({"entry": None, "queue_size": 0, "commands": commands})
        entry   = None
        corrupt = []
        for e in pending_high:
            if not e.get("nick", "").strip():
                corrupt.append(e)
                continue
            k = _make_ip_key(e)
            if k not in in_progress_high:
                entry = e
                break
        for e in corrupt:
            try:
                pending_high.remove(e)
            except ValueError:
                pass
        if corrupt:
            _save_pending()
        if not entry:
            return jsonify({"entry": None, "queue_size": 0, "commands": commands})
        ip_key = _make_ip_key(entry)
        in_progress_high[ip_key] = {"entry": entry, "taken_at": datetime.now(timezone.utc), "gm_id": gm_id}
        size = len(pending_high)
    log.info(f"[TAKE-HIGH] {entry['nick']} -> gm:{_gm_display(gm_id)}")
    out = _fmt_entry(entry)
    out["ip_key"] = ip_key
    return jsonify({"entry": out, "queue_size": size, "commands": commands})

@app.route('/result', methods=['POST'])
def result_high():
    err = require_secret()
    if err: return err
    data = request.json or {}
    entry, log_ch_id, log_msg_id, status = _process_result(
        pending_high, in_progress_high, processed_high, claim_counts_high, "HIGH", data
    )
    if not log_ch_id:
        log_ch_id  = data.get("log_ch_id")
        log_msg_id = data.get("log_msg_id")
    if log_ch_id and log_msg_id:
        emoji = EMOJI_SUCCESS if status == "success" else (EMOJI_SKIP if status == "already" else EMOJI_FAILED)
        schedule_reaction(int(log_ch_id), int(log_msg_id), emoji)
    return jsonify({"ok": True})

@app.route('/status', methods=['GET'])
def status_high():
    err = require_secret()
    if err: return err
    with lock:
        return jsonify({"pending": [e["nick"] for e in pending_high], "count": len(pending_high)})

@app.route('/clear', methods=['POST'])
def clear_high():
    err = require_secret()
    if err: return err
    with lock:
        pending_high.clear()
        in_progress_high.clear()
        processed_high.clear()
        _save_pending()
        _save_processed()
    return jsonify({"ok": True})

# ── MEDIUM endpoints ─────────────────────────────────────────
@app.route('/pending_medium/take', methods=['POST'])
def take_pending_medium():
    err = require_secret()
    if err: return err
    body    = request.json or {}
    gm_id   = body.get("gm_id", "unknown")
    gm_name = body.get("gm_name", "")
    _touch_gm(gm_id, gm_name, "medium")
    commands = _pop_gm_commands(gm_id)
    with lock:
        _cleanup_expired_inprogress()
        if not pending_medium:
            return jsonify({"entry": None, "queue_size": 0, "commands": commands})
        entry   = None
        corrupt = []
        for e in pending_medium:
            if not e.get("nick", "").strip():
                corrupt.append(e)
                continue
            k = _make_ip_key(e)
            if k not in in_progress_medium:
                entry = e
                break
        for e in corrupt:
            try:
                pending_medium.remove(e)
            except ValueError:
                pass
        if corrupt:
            _save_pending()
        if not entry:
            return jsonify({"entry": None, "queue_size": 0, "commands": commands})
        ip_key = _make_ip_key(entry)
        in_progress_medium[ip_key] = {"entry": entry, "taken_at": datetime.now(timezone.utc), "gm_id": gm_id}
        size = len(pending_medium)
    log.info(f"[TAKE-MEDIUM] {entry['nick']} -> gm:{_gm_display(gm_id)}")
    out = _fmt_entry(entry)
    out["ip_key"] = ip_key
    return jsonify({"entry": out, "queue_size": size, "commands": commands})

@app.route('/result_medium', methods=['POST'])
def result_medium():
    err = require_secret()
    if err: return err
    data = request.json or {}
    entry, log_ch_id, log_msg_id, status = _process_result(
        pending_medium, in_progress_medium, processed_medium, claim_counts_medium, "MEDIUM", data
    )
    if not log_ch_id:
        log_ch_id  = data.get("log_ch_id")
        log_msg_id = data.get("log_msg_id")
    if log_ch_id and log_msg_id:
        emoji = EMOJI_SUCCESS if status == "success" else (EMOJI_SKIP if status == "already" else EMOJI_FAILED)
        schedule_reaction(int(log_ch_id), int(log_msg_id), emoji)
    return jsonify({"ok": True})

@app.route('/status_medium', methods=['GET'])
def status_medium():
    err = require_secret()
    if err: return err
    with lock:
        return jsonify({"pending": [e["nick"] for e in pending_medium], "count": len(pending_medium)})

@app.route('/clear_medium', methods=['POST'])
def clear_medium():
    err = require_secret()
    if err: return err
    with lock:
        pending_medium.clear()
        in_progress_medium.clear()
        processed_medium.clear()
        _save_pending()
        _save_processed()
    return jsonify({"ok": True})

# ============================================================
#  DISCORD BOT
# ============================================================
from discord.ext import commands
intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

@client.tree.command(name="claim", description="Buka form claim item RF Online")
async def slash_claim(interaction: discord.Interaction):
    if CLAIM_CHANNEL and interaction.channel_id != CLAIM_CHANNEL:
        await interaction.response.send_message(f"❌ Command ini hanya bisa digunakan di <#{CLAIM_CHANNEL}>.", ephemeral=True)
        return
    if interaction.user.id in BLACKLIST_IDS:
        await interaction.response.send_message("🚫 Kamu tidak dapat menggunakan fitur ini.", ephemeral=True)
        return
    if interaction.user.id in _active_wizards:
        await interaction.response.send_message("⚠️ Kamu sudah punya form claim aktif.", view=CancelWizardView(interaction.user.id), ephemeral=True)
        return
    wizard = ClaimWizard(user=interaction.user)
    _active_wizards[interaction.user.id] = wizard
    view = _ClaimTriggerView(wizard)
    await interaction.response.send_message("🎫 Klik tombol di bawah untuk membuka form claim:", view=view, ephemeral=True)

class _ClaimTriggerView(ui.View):
    def __init__(self, wizard: ClaimWizard):
        super().__init__(timeout=60)
        self.wizard = wizard

    async def on_timeout(self):
        _active_wizards.pop(self.wizard.user.id, None)

    @ui.button(label="🎫 Buka Form Claim", style=discord.ButtonStyle.primary)
    async def open_form(self, interaction: discord.Interaction, button: ui.Button):
        try:
            self.stop()
            button.disabled = True
            await self.wizard.start(interaction)
            await interaction.message.edit(view=self)
        except Exception as e:
            log.warning(f"[ERROR-TRIGGER] {e}")
            _active_wizards.pop(self.wizard.user.id, None)

@client.event
async def on_message(message: discord.Message):
    global _panel_msg_id
    if message.author.bot:
        return
    if message.author.id not in ADMIN_IDS:
        await client.process_commands(message)
        return
    cmd   = message.content.strip().lower()
    parts = message.content.strip().split(maxsplit=1)

    if parts[0].lower() == "!stopgm":
        if len(parts) < 2:
            sent = await message.reply("⚠️ Format: `!stopgm <nama_atau_gm_id> [high|medium|all]` (default: all)")
        else:
            tokens = parts[1].split()
            channel_arg = "all"
            if tokens[-1].lower() in ("high", "medium", "all") and len(tokens) > 1:
                channel_arg = tokens[-1].lower()
                target_raw  = " ".join(tokens[:-1])
            else:
                target_raw = parts[1]
            gid = _find_gm_id(target_raw.strip())
            if not gid:
                sent = await message.reply(f"⚠️ GM **{target_raw}** tidak ditemukan. Cek nama yang aktif lewat `!listgm`.")
            else:
                cmd_map = {"high": "stop_high", "medium": "stop_medium", "all": "stop_all"}
                _queue_gm_command(gid, cmd_map[channel_arg])
                sent = await message.reply(
                    f"🛑 Perintah **{cmd_map[channel_arg]}** dikirim ke GM **{_gm_display(gid)}** "
                    f"— akan dieksekusi saat extension poll berikutnya (±5 detik)."
                )
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!listgm":
        with lock:
            now   = datetime.now(timezone.utc)
            lines = ["📋 **Daftar GM**"]
            if not gm_registry:
                lines.append("_(belum ada GM yang pernah konek)_")
            for gid, v in sorted(gm_registry.items(), key=lambda kv: (kv[1].get("name") or kv[0]).lower()):
                name = v.get("name") or "_(nama belum diisi)_"
                ch   = v.get("channel", "-")
                raw  = v.get("last_seen")
                if raw:
                    try:
                        delta      = (now - datetime.fromisoformat(raw)).total_seconds()
                        status_txt = f"🟢 online ({ch})" if delta < GM_ONLINE_THRESHOLD_SECONDS else f"⚪ idle {int(delta)}s lalu ({ch})"
                    except Exception:
                        status_txt = "❓"
                else:
                    status_txt = "❓"
                pending_cmds = v.get("commands") or []
                cmd_note = f" — ⏳ command pending: {', '.join(pending_cmds)}" if pending_cmds else ""
                lines.append(f"• **{name}** — `{gid}` — {status_txt}{cmd_note}")
        sent = await message.reply("\n".join(lines))
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!resetclaims":
        claim_counts_high.clear()
        claim_counts_medium.clear()
        _save_claims()
        sent = await message.reply("✅ Claim counts direset.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif parts[0].lower() == "!resetdmnick":
        if len(parts) == 2:
            target = parts[1].strip().lower()
            with lock:
                keys_to_del = [k for k in dm_nick_counts if k.startswith(f"{target}:")]
                if keys_to_del:
                    for k in keys_to_del:
                        del dm_nick_counts[k]
                    _save_dm_nick_counts()
                    sent = await message.reply(f"✅ DM counter untuk nick **{target}** direset.")
                else:
                    sent = await message.reply(f"⚠️ Nick **{target}** tidak ditemukan.")
        else:
            with lock:
                dm_nick_counts.clear()
                _save_dm_nick_counts()
            sent = await message.reply("✅ Semua DM nick counter direset.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!cleandmnick":
        removed = _cleanup_dm_nick_counts()
        sisa = len(dm_nick_counts)
        sent = await message.reply(
            f"🧹 Cleanup `dm_nick_counts` selesai.\n"
            f"Dihapus: **{removed}** entry (TTL {DM_NICK_TTL_DAYS} hari)\n"
            f"Sisa: **{sisa}** entry"
        )
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!resetpanel":
        _panel_msg_id = None
        try:
            if os.path.exists(PANEL_FILE):
                os.remove(PANEL_FILE)
        except Exception:
            pass
        sent = await message.reply("🔄 Panel di-reset.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        await _setup_persistent_panel()

    elif cmd == "!updatepanel":
        sent = await message.reply("🔄 Memperbarui panel...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        try:
            ch = client.get_channel(CLAIM_CHANNEL)
            if ch is None:
                ch = await client.fetch_channel(CLAIM_CHANNEL)
            if _panel_msg_id:
                try:
                    old_msg = await ch.fetch_message(_panel_msg_id)
                    await old_msg.delete()
                except Exception:
                    pass
                _panel_msg_id = None
            view    = PanelView()
            new_msg = await ch.send(embed=_build_panel_embed(), view=view)
            _panel_msg_id = new_msg.id
            client.add_view(view, message_id=new_msg.id)
            sent2 = await message.reply(f"✅ Panel berhasil diperbarui di <#{CLAIM_CHANNEL}>!")
        except Exception as e:
            sent2 = await message.reply(f"❌ Gagal update panel: {e}")
        asyncio.ensure_future(_auto_delete(sent2, AUTO_DELETE_SECONDS))

    elif cmd == "!listclaims":
        with lock:
            lines = ["📋 **Daftar Claim**"]
            if claim_counts_high:
                lines.append("\n**High:**")
                for uid, cnt in claim_counts_high.items():
                    lines.append(f"  `{uid}` — {cnt}x")
            if claim_counts_medium:
                lines.append("\n**Medium:**")
                for uid, cnt in claim_counts_medium.items():
                    lines.append(f"  `{uid}` — {cnt}x")
            if not claim_counts_high and not claim_counts_medium:
                lines.append("_(belum ada claim)_")
        sent = await message.reply("\n".join(lines))
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!status":
        with lock:
            h = len(pending_high)
            m = len(pending_medium)
        sent = await message.reply(f"📊 **Status antrian**\nHigh: {h} pending\nMedium: {m} pending")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!datadir":
        sent = await message.reply(f"📁 **Data directory:** `{DATA_DIR}`")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    elif cmd == "!synccmds":
        sent = await message.reply("🔄 Sync slash commands...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        _sync_flag = os.path.join(_BASE_DIR, "sync_done.flag")
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                client.tree.copy_global_to(guild=guild)
                synced = await client.tree.sync(guild=guild)
                client.tree.clear_commands(guild=None)
                await client.tree.sync()
            else:
                synced = await client.tree.sync()
            with open(_sync_flag, "w") as _f:
                _f.write(datetime.now(timezone.utc).isoformat())
            sent2 = await message.reply(f"✅ Sync selesai: {len(synced)} command(s).")
        except Exception as e:
            sent2 = await message.reply(f"❌ Gagal sync: {e}")
        asyncio.ensure_future(_auto_delete(sent2, AUTO_DELETE_SECONDS))

    elif cmd == "!restartbot":
        sent = await message.reply("🔄 Bot sedang restart...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        await _graceful_shutdown("bot direstart")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    elif cmd == "!stopbot":
        sent = await message.reply("❌ Bot dihentikan oleh admin.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        await _graceful_shutdown("bot dihentikan")

    await client.process_commands(message)

@client.event
async def on_disconnect():
    log.info("[BOT] Koneksi Discord terputus.")

async def _dm_nick_cleanup_loop():
    """Jalan di background, cek & buang entry dm_nick_counts yang expired secara berkala."""
    while not _shutting_down:
        await asyncio.sleep(DM_NICK_CLEANUP_INTERVAL_HOURS * 3600)
        if _shutting_down:
            break
        removed = _cleanup_dm_nick_counts()
        if removed:
            log.info(f"[DM-CLEANUP] {removed} entry dm_nick_counts expired dibuang (periodic)")

@client.event
async def on_ready():
    global _bot_loop
    _bot_loop = asyncio.get_event_loop()

    def _sigint_override(signum, frame):
        if not _shutting_down and _bot_loop and not _bot_loop.is_closed():
            asyncio.run_coroutine_threadsafe(_graceful_shutdown("dihentikan oleh launcher"), _bot_loop)

    signal.signal(signal.SIGINT,  _sigint_override)
    signal.signal(signal.SIGTERM, _sigint_override)
    try:
        signal.signal(signal.SIGBREAK, _sigint_override)
    except AttributeError:
        pass

    _sync_flag = os.path.join(_BASE_DIR, "sync_done.flag")
    _need_sync = not os.path.exists(_sync_flag)
    if _need_sync:
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                client.tree.copy_global_to(guild=guild)
                synced = await client.tree.sync(guild=guild)
                client.tree.clear_commands(guild=None)
                await client.tree.sync()
                log.info(f"[BOT] Slash commands synced ke guild {GUILD_ID}: {len(synced)}")
            else:
                synced = await client.tree.sync()
                log.info(f"[BOT] Slash commands synced global: {len(synced)}")
            with open(_sync_flag, "w") as _f:
                _f.write(datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning(f"[BOT] Gagal sync slash commands: {e}")
    else:
        log.info("[BOT] Slash commands sync dilewati (sudah sync).")

    log.info(f"[BOT] Login sebagai {client.user}")
    log.info(f"[BOT] Data directory: {DATA_DIR}")
    log.info(f"[BOT] Claim channel: {CLAIM_CHANNEL}")
    log.info(f"[BOT] Log channel High: {LOG_CHANNEL_HIGH}")
    log.info(f"[BOT] Log channel Medium: {LOG_CHANNEL_MED}")
    log.info(f"[BOT] Admin IDs: {ADMIN_IDS}")
    log.info(f"[BOT] Max claim: {MAX_CLAIM if MAX_CLAIM > 0 else 'UNLIMITED'}")

    removed = _cleanup_dm_nick_counts()
    if removed:
        log.info(f"[DM-CLEANUP] {removed} entry dm_nick_counts expired dibuang saat startup")
    asyncio.ensure_future(_dm_nick_cleanup_loop())

    await _setup_persistent_panel()

# ============================================================
#  MAIN
# ============================================================
if __name__ == '__main__':
    railway_port = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "8080")))
    railway_host = '0.0.0.0'

    def run_flask():
        import logging as _logging
        _logging.getLogger("werkzeug").setLevel(_logging.WARNING)
        app.run(host=railway_host, port=railway_port, debug=False, use_reloader=False)

    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info(f"[SERVER] Flask server started at http://{railway_host}:{railway_port}")
    log.info(f"[SERVER] Health check: /health")

    try:
        client.run(BOT_TOKEN, reconnect=True)
    except KeyboardInterrupt:
        if not _shutting_down and _bot_loop and not _bot_loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(_graceful_shutdown("dihentikan oleh launcher"), _bot_loop)
            try:
                future.result(timeout=6)
            except Exception:
                pass
