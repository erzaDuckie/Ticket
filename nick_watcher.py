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
#  DATA DIR — Railway Volume atau lokal
#  Set env var DATA_DIR ke mount path volume Railway (/data)
#  Kalau tidak diset, pakai folder yang sama dengan script ini
# ============================================================
DATA_DIR = os.environ.get("DATA_DIR", os.path.dirname(os.path.abspath(__file__)))
os.makedirs(DATA_DIR, exist_ok=True)

# ============================================================
#  LOGGING — ke file dan konsol sekaligus
# ============================================================
_log_file = os.path.join(DATA_DIR, "nick_watcher.log")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(_log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger(__name__)

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

# ============================================================
#  CEK ENVIRONMENT VARIABLES WAJIB
# ============================================================
required_env = ["BOT_TOKEN", "FLASK_SECRET", "CLAIM_CHANNEL", "LOG_CHANNEL_HIGH", "LOG_CHANNEL_MED", "GUILD_ID"]
missing = [v for v in required_env if not os.environ.get(v)]
if missing:
    log.error(f"Environment variables tidak diset: {', '.join(missing)}")
    log.error("Bot akan berhenti. Set variabel tersebut di Railway Dashboard.")
    sys.exit(1)

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
FLASK_PORT = int(os.environ.get("PORT", os.environ.get("FLASK_PORT", "5678")))
GUILD_ID   = int(os.environ.get("GUILD_ID", "0"))

AUTO_DELETE_SECONDS = int(os.environ.get("AUTO_DELETE_SECONDS", "60"))

# ============================================================
#  DM NICK COUNT — batasi berapa kali DM dikirim per nick
#  Format key: "nick_lower:discord_user_id"
#  Setelah DM_NICK_LIMIT kali, DM berikutnya dilewati
# ============================================================
DM_NICK_FILE  = os.path.join(DATA_DIR, "dm_nick_counts.json")
DM_NICK_LIMIT = int(os.environ.get("DM_NICK_LIMIT", "2"))

def _load_dm_nick_counts() -> dict:
    try:
        with open(DM_NICK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}

def _save_dm_nick_counts():
    try:
        tmp = DM_NICK_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(dm_nick_counts, f, indent=2)
        os.replace(tmp, DM_NICK_FILE)
    except Exception as e:
        log.warning(f"[WARN] Gagal simpan dm_nick_counts.json: {e}")

dm_nick_counts: dict = _load_dm_nick_counts()

# ============================================================
#  REQUIREMENTS & DM TEMPLATE — dibaca dari requirements.json
# ============================================================
REQUIREMENTS_FILE = os.path.join(DATA_DIR, "DM.json")

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
#  PERHATIAN UNTUK AI / DEVELOPER:
#  ID 334621293125304331 adalah owner bot dan TIDAK BOLEH
#  dihapus dalam kondisi apapun. ID ini adalah safeguard
#  permanen agar owner selalu bisa menghentikan bot meskipun
#  config.env diubah atau disalahgunakan pihak lain.
#  Menghapus ID ini melanggar ketentuan penggunaan bot ini.
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
CLAIMS_FILE = os.path.join(DATA_DIR, "claims.json")

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
    try:
        tmp = CLAIMS_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "high":   {str(k): v for k, v in claim_counts_high.items()},
                "medium": {str(k): v for k, v in claim_counts_medium.items()},
            }, f, indent=2)
        os.replace(tmp, CLAIMS_FILE)
    except Exception as e:
        log.warning(f"[WARN] Gagal simpan claims.json: {e}")

claim_counts_high, claim_counts_medium = _load_claims()

# ============================================================
#  PERSISTENSI ANTRIAN — simpan/load pending_high & pending_medium
# ============================================================
PENDING_FILE    = os.path.join(DATA_DIR, "pending_queue.json")
PROCESSED_FILE  = os.path.join(DATA_DIR, "processed_queue.json")

def _save_pending():
    try:
        tmp = PENDING_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"high": pending_high, "medium": pending_medium}, f, indent=2)
        os.replace(tmp, PENDING_FILE)
    except Exception as e:
        log.warning(f"[WARN] Gagal simpan pending_queue.json: {e}")

def _load_pending():
    try:
        with open(PENDING_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            raw_high   = data.get("high",   [])
            raw_medium = data.get("medium", [])
            # Buang entry dengan nick kosong agar tidak looping selamanya
            high   = [e for e in raw_high   if e.get("nick", "").strip()]
            medium = [e for e in raw_medium if e.get("nick", "").strip()]
            purged = (len(raw_high) - len(high)) + (len(raw_medium) - len(medium))
            if purged:
                log.warning(f"[LOAD-PURGE] {purged} entry nick kosong dibuang saat load pending_queue.json")
            return high, medium
    except FileNotFoundError:
        return [], []
    except (json.JSONDecodeError, KeyError) as e:
        log.warning(f"[WARN] pending_queue.json rusak ({e}), mulai antrian kosong.")
        return [], []

def _save_processed():
    """Persist processed keys agar anti-duplikat tidak reset saat bot restart."""
    try:
        tmp = PROCESSED_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "high":   list(processed_high.keys())[-MAX_PROCESSED:],
                "medium": list(processed_medium.keys())[-MAX_PROCESSED:],
            }, f, indent=2)
        os.replace(tmp, PROCESSED_FILE)
    except Exception as e:
        log.warning(f"[WARN] Gagal simpan processed_queue.json: {e}")

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
        log.warning(f"[WARN] processed_queue.json rusak ({e}), mulai processed kosong.")
        return OrderedDict(), OrderedDict()

# ============================================================
#  ANTRIAN — load dari file saat startup
#  processed_* pakai OrderedDict sebagai FIFO set:
#  key=entry_id, value=None. Entri terlama dibuang duluan.
# ============================================================
MAX_PROCESSED = 2000

_saved_high, _saved_medium = _load_pending()
pending_high     = _saved_high
pending_medium   = _saved_medium
processed_high, processed_medium = _load_processed()   # persist across restarts
log_msg_map      = {}

if processed_high or processed_medium:
    log.info(f"[PROCESSED] Loaded dari processed_queue.json — high: {len(processed_high)}, medium: {len(processed_medium)}")

if pending_high or pending_medium:
    log.info(f"[QUEUE] Loaded dari pending_queue.json — high: {len(pending_high)}, medium: {len(pending_medium)}")

lock           = threading.Lock()
_bot_loop      = None
_shutting_down = False

# ============================================================
#  IN-PROGRESS TABLE — entry yang sudah di-take GM tapi belum
#  ada /result. Dipakai untuk rollback claim count jika GM
#  tidak mengirim /result (crash/disconnect).
#  Format: { entry_id_str: {"entry": dict, "taken_at": datetime, "gm_id": str} }
#
#  CLAIM_TIMEOUT_SECONDS — jika GM tidak kirim /result dalam
#  waktu ini, entry dikembalikan ke depan antrian otomatis.
# ============================================================
CLAIM_TIMEOUT_SECONDS = int(os.environ.get("CLAIM_TIMEOUT_SECONDS", "300"))  # 5 menit

in_progress_high   = {}   # { str(log_msg_id or nick): {"entry": dict, "taken_at": datetime, "gm_id": str} }
in_progress_medium = {}

def _make_ip_key(entry: dict) -> str:
    """Buat key unik untuk in_progress table."""
    return str(entry.get("log_msg_id") or f"{entry['nick']}:{entry['author_id']}")

def _cleanup_expired_inprogress():
    """
    Dipanggil di dalam lock.
    Entry yang sudah timeout dikembalikan ke depan antrian
    agar GM lain bisa mengambilnya.
    Entry corrupt (nick kosong) langsung dibuang — tidak dikembalikan ke antrian
    karena tidak akan bisa diproses dan hanya akan looping terus.
    """
    now = datetime.now(timezone.utc)
    for table, queue in ((in_progress_high, pending_high), (in_progress_medium, pending_medium)):
        expired_keys = [
            k for k, v in table.items()
            if (now - v["taken_at"]).total_seconds() > CLAIM_TIMEOUT_SECONDS
        ]
        for k in expired_keys:
            info  = table.pop(k)
            entry = info["entry"]
            nick  = entry.get("nick", "").strip()

            # Entry corrupt (nick kosong) — buang langsung, jangan looping
            if not nick:
                # Hapus juga dari queue kalau masih ada
                try:
                    queue.remove(entry)
                except ValueError:
                    pass
                log.warning(
                    f"[TAKE-EXPIRE-DISCARD] key={k} nick kosong — entry dibuang "
                    f"(timeout {CLAIM_TIMEOUT_SECONDS}s, gm={info['gm_id']})"
                )
                _save_pending()
                continue

            # Entry valid — kembalikan ke depan antrian (priority)
            queue.insert(0, entry)
            log.info(
                f"[TAKE-EXPIRE] key={k} nick={nick} "
                f"dikembalikan ke antrian (timeout {CLAIM_TIMEOUT_SECONDS}s, gm={info['gm_id']})"
            )

def _trim_processed(od: OrderedDict):
    """Buang entri paling lama (FIFO) saat melebihi MAX_PROCESSED."""
    while len(od) > MAX_PROCESSED:
        od.popitem(last=False)

# ============================================================
#  HELPER — auto-delete pesan setelah N detik
# ============================================================
async def _auto_delete(msg, delay: int):
    await asyncio.sleep(delay)
    try:
        await msg.delete()
        log.info(f"[AUTO-DELETE] Pesan {msg.id} dihapus setelah {delay}d.")
    except discord.NotFound:
        pass
    except Exception as e:
        log.warning(f"[AUTO-DELETE] Gagal hapus pesan {msg.id}: {e}")

# ============================================================
#  HELPER — kirim ke channel
# ============================================================
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
        log.warning(f"[WARN] Gagal kirim pesan ke channel {channel_id}: {e}")

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
        log.warning(f"[WARN] Gagal kirim embed ke channel {channel_id}: {e}")
    return None

# ============================================================
#  HELPER — schedule reaction dari thread Flask
# ============================================================
def schedule_reaction(channel_id: int, message_id: int, emoji: str):
    async def _do():
        for attempt in range(3):
            try:
                ch = client.get_channel(channel_id)
                if ch is None:
                    ch = await client.fetch_channel(channel_id)
                msg = await ch.fetch_message(message_id)
                await msg.add_reaction(emoji)
                log.info(f"[REACTION] {emoji} -> msg {message_id}")
                return
            except Exception as e:
                log.warning(f"[REACTION-ERROR] attempt {attempt+1}/3: {e}")
                if attempt < 2:
                    await asyncio.sleep(2)
        log.warning(f"[REACTION-FAILED] semua retry habis untuk msg {message_id}")

    if _bot_loop is None or _bot_loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_do(), _bot_loop)

# ============================================================
#  HELPER — kirim DM ke user (not_req / already)
# ============================================================
def _build_dm_message(channel_key: str, status: str, nick: str, mention: str, channel_name: str = "") -> str | None:
    cfg = REQUIREMENTS.get(channel_key, {})
    if not cfg:
        return None
    if status == "not_req":
        template = cfg.get("dm_not_req", "")
    elif status == "already":
        template = cfg.get("dm_already", "")
    elif status == "success":
        template = cfg.get("dm_success", "")
    elif status == "not_found":
        template = cfg.get("dm_not_found", "")
    else:
        return None
    if not template:
        return None
    placeholders = {k: str(v) for k, v in cfg.items()}
    placeholders.update({"nick": nick, "mention": mention, "channel_name": channel_name})
    try:
        return template.format_map(placeholders)
    except KeyError as e:
        log.warning(f"[DM-WARN] Placeholder {e} tidak ada di DM.json")
        return template

def schedule_dm(author_id: int, log_ch_id: int, channel_key: str, status: str, nick: str):
    # DM success selalu dikirim (tidak kena limit).
    # DM not_req / already dibatasi DM_NICK_LIMIT kali per nick per user.
    if status in ("not_req", "already"):
        dm_key = f"{nick.lower()}:{author_id}"
        with lock:
            count = dm_nick_counts.get(dm_key, 0)
            if DM_NICK_LIMIT > 0 and count >= DM_NICK_LIMIT:
                log.info(f"[DM-SKIP] Limit {DM_NICK_LIMIT}x tercapai untuk {dm_key} — DM dilewati.")
                return
            dm_nick_counts[dm_key] = count + 1
            _save_dm_nick_counts()
            log.info(f"[DM-TRACK] {dm_key} DM ke-{count + 1}/{DM_NICK_LIMIT}")

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
                except Exception as e:
                    log.warning(f"[DM-WARN] Gagal fetch nama channel: {e}")
            msg_text = _build_dm_message(channel_key, status, nick, mention, channel_name)
            if not msg_text:
                return
            await user.send(msg_text)
            log.info(f"[DM] Terkirim ke {author_id} ({status}) — nick: {nick} | channel: #{channel_name}")
        except discord.Forbidden:
            log.info(f"[DM-SKIP] User {author_id} menutup DM.")
        except Exception as e:
            log.warning(f"[DM-ERROR] {e}")

    if _bot_loop is None or _bot_loop.is_closed():
        return
    asyncio.run_coroutine_threadsafe(_do(), _bot_loop)

# ============================================================
#  HELPER — graceful shutdown
# ============================================================
async def _graceful_shutdown(reason: str = "dimatikan"):
    global _shutting_down
    if _shutting_down:
        return
    _shutting_down = True
    log.info(f"[BOT] Shutdown: {reason}")
    with lock:
        _save_pending()
        _save_processed()
        log.info(f"[QUEUE] Antrian disimpan — high: {len(pending_high)}, medium: {len(pending_medium)}")
        log.info(f"[PROCESSED] Processed disimpan — high: {len(processed_high)}, medium: {len(processed_medium)}")
    await client.close()

# ============================================================
#  HELPER — buat embed log claim
# ============================================================
def _build_claim_embed(nick: str, tier_label: str, panel: str, choices: dict, author: discord.Member) -> discord.Embed:
    color = discord.Color.gold() if panel == "high" else discord.Color.blue()
    embed = discord.Embed(
        title=f"\U0001f3ab Claim — {nick}",
        description=f"**{tier_label}**",
        color=color
    )
    tier_items = get_form_items(tier_label)
    if tier_items:
        for item in tier_items:
            key   = item["key"]
            label = item["label"]
            val   = choices.get(key) or "—"
            embed.add_field(name=label, value=val, inline=False)
    else:
        embed.add_field(name="Reward", value="Sesuai paket", inline=False)
    embed.set_footer(text=f"by {author.display_name} ({author.id})")
    return embed

# ============================================================
#  DISCORD UI — Step dropdown per item form
# ============================================================
class ItemSelect(ui.Select):
    def __init__(self, item: dict, wizard: "ClaimWizard"):
        self.item_key = item["key"]
        self.wizard   = wizard
        options = [
            discord.SelectOption(label=opt, value=opt)
            for opt in item["options"]
        ]
        super().__init__(
            placeholder=f"Pilih {item['label']}...",
            options=options,
            min_values=1,
            max_values=1,
        )

    async def callback(self, interaction: discord.Interaction):
        try:
            self.wizard.choices[self.item_key] = self.values[0]
            await self.wizard.advance(interaction)
        except Exception as e:
            log.warning(f"[ERROR-ITEM-SELECT] {e}")
            try:
                await interaction.response.send_message(
                    "❌ Terjadi kesalahan. Pilih paket lagi dari channel claim.", ephemeral=True
                )
            except Exception:
                pass


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


# ============================================================
#  DISCORD UI — Summary + Confirm
# ============================================================
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



# ============================================================
#  DISCORD UI — Cancel Wizard View
#  Muncul saat player klik dropdown tapi sudah punya wizard aktif.
#  Memberi opsi: batalkan wizard lama agar bisa mulai baru.
# ============================================================
class CancelWizardView(ui.View):
    """
    Muncul saat player klik dropdown tapi sudah punya wizard aktif.
    Opsi:
      🔄 Lanjutkan  — kirim ulang step terakhir wizard yang aktif
      🗑️ Batalkan   — hapus wizard lama, player bisa mulai baru
    """
    def __init__(self, user_id: int):
        super().__init__(timeout=60)
        self.user_id = user_id

    @ui.button(label="🔄 Lanjutkan Form", style=discord.ButtonStyle.primary)
    async def resume_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ini bukan form kamu.", ephemeral=True)
            return

        wizard = _active_wizards.get(self.user_id)
        if not wizard or wizard._submitted:
            await interaction.response.edit_message(
                content="⚠️ Form sudah tidak aktif. Silakan pilih paket dari dropdown lagi.",
                view=None
            )
            _active_wizards.pop(self.user_id, None)
            return

        self.stop()

        # Kalau nick belum diisi, tampilkan modal nick lagi
        if not wizard.nick or not wizard.tier:
            modal = NickTierModal(wizard)
            await interaction.response.send_modal(modal)
            return

        # Nick & tier sudah ada — lanjut ke step dropdown atau summary
        # Tentukan step terakhir yang sudah diisi
        form_items = wizard.active_form_items
        next_step  = 0
        for i, item in enumerate(form_items):
            if item["key"] not in wizard.choices:
                next_step = i
                break
        else:
            next_step = len(form_items)  # semua sudah diisi, ke summary

        # Reset _resp_msg agar kirim pesan baru (interaksi lama sudah hilang)
        wizard._resp_msg = None

        if next_step >= len(form_items):
            await wizard._show_summary(interaction)
        else:
            wizard._step = next_step + 2
            await wizard._show_item_step(interaction)

    @ui.button(label="🗑️ Batalkan Form Lama", style=discord.ButtonStyle.danger)
    async def cancel_btn(self, interaction: discord.Interaction, button: ui.Button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message("❌ Ini bukan form kamu.", ephemeral=True)
            return
        wizard = _active_wizards.pop(self.user_id, None)
        if wizard:
            wizard._submitted = True
        self.stop()
        await interaction.response.edit_message(
            content="✅ Form lama dibatalkan. Silakan pilih paket dari dropdown lagi.",
            view=None
        )


class NickTierModal(ui.Modal, title="Claim RF Online"):
    nick_input = ui.TextInput(
        label="Nick in-game",
        placeholder="Masukkan nick kamu...",
        min_length=1,
        max_length=20,
    )

    def __init__(self, wizard: "ClaimWizard"):
        super().__init__()
        self.wizard = wizard

    async def on_submit(self, interaction: discord.Interaction):
        self.wizard._modal_submitted = True
        nick = self.nick_input.value.strip()
        if not nick:
            await interaction.response.send_message(
                "❌ Nick tidak boleh kosong.", ephemeral=True
            )
            return
        self.wizard.nick = nick
        if self.wizard.tier:
            await self.wizard.start_item_steps(interaction)
        else:
            await self.wizard.show_tier_step(interaction)

    async def on_error(self, interaction: discord.Interaction, error: Exception):
        log.warning(f"[ERROR-MODAL] {error}")
        _active_wizards.pop(self.wizard.user.id, None)
        try:
            await interaction.response.send_message(
                "❌ Terjadi kesalahan. Pilih paket lagi dari channel claim.", ephemeral=True
            )
        except Exception:
            pass


# ============================================================
#  DISCORD UI — Tier Select (dari /claim slash command)
# ============================================================
class TierSelect(ui.Select):
    def __init__(self, wizard: "ClaimWizard"):
        self.wizard = wizard
        options = [
            discord.SelectOption(
                label=t["label"],
                value=t["label"],
                emoji=t.get("emoji", "🎫")
            )
            for t in CLAIM_TIERS
        ]
        super().__init__(placeholder="Pilih paket claim...", options=options)

    async def callback(self, interaction: discord.Interaction):
        try:
            self.wizard.tier = self.values[0]
            await self.wizard.advance(interaction)
        except Exception as e:
            log.warning(f"[ERROR-TIER-SELECT] {e}")
            try:
                await interaction.response.send_message(
                    "❌ Terjadi kesalahan. Pilih paket lagi dari channel claim.", ephemeral=True
                )
            except Exception:
                pass


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


# ============================================================
#  WIZARD — orchestrator seluruh alur form
# ============================================================
class ClaimWizard:
    """
    Step 0 : Modal (nick)
    Step 1 : Tier select (hanya dari /claim, panel langsung ke step 2)
    Step 2..N : Item select per FORM_ITEMS
    Step N+1 : Summary + Confirm
    """
    def __init__(self, user: discord.Member, original_message: discord.Message | None = None):
        self.user             = user
        self.original_message = original_message
        self.nick: str        = ""
        self.tier: str        = ""
        self.choices: dict    = {}
        self._step            = 0
        self._resp_msg        = None
        self._cancelled       = False
        self._submitted       = False
        self._modal_submitted = False

    # ── Mulai wizard (dari /claim) ────────────────────────────
    async def start(self, interaction: discord.Interaction):
        modal = NickTierModal(self)
        await interaction.response.send_modal(modal)
        asyncio.create_task(self._modal_cleanup_guard())

    # ── Langsung ke item steps (tier sudah dari panel) ────────
    async def start_item_steps(self, interaction: discord.Interaction):
        self._step = 2
        if self.active_form_items:
            await self._show_item_step(interaction)
        else:
            await self._show_summary(interaction)

    async def _modal_cleanup_guard(self):
        """Cleanup wizard kalau modal ditutup tanpa diisi."""
        await asyncio.sleep(180)
        if not self._modal_submitted and not self._submitted and not self._cancelled:
            log.info(f"[WIZARD] Modal timeout/closed untuk {self.user.id} — cleanup")
            _active_wizards.pop(self.user.id, None)

    # ── Tampilkan step tier ───────────────────────────────────
    async def show_tier_step(self, interaction: discord.Interaction):
        view    = TierSelectView(self)
        total   = len(FORM_ITEMS) + 1 if FORM_ITEMS else 1
        content = f"**🎫 Claim — Step 1/{total}**\n`{self.nick}` — Pilih tier:"
        if self._resp_msg is None:
            await interaction.response.send_message(content, view=view, ephemeral=True)
            self._resp_msg = await interaction.original_response()
        else:
            await interaction.response.edit_message(content=content, view=view)

    # ── Advance ke step berikutnya ────────────────────────────
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
        item     = self.active_form_items[item_idx]
        total    = len(self.active_form_items) + 1
        step_num = self._step

        view    = ItemSelectView(item, self)
        content = (
            f"**🎫 Claim — Step {step_num}/{total}**\n"
            f"`{self.nick}` [{self.tier}]\n\n"
            f"**{item['label']}**"
        )
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
        view    = ConfirmView(self)
        # Jika _resp_msg belum ada (mis. medium tanpa step form langsung ke summary),
        # pakai send_message, bukan edit_message, agar tidak crash.
        if self._resp_msg is None:
            await interaction.response.send_message(content, view=view, ephemeral=True)
            self._resp_msg = await interaction.original_response()
        else:
            await interaction.response.edit_message(content=content, view=view)

    # ── Submit ────────────────────────────────────────────────
    async def submit(self, interaction: discord.Interaction):
        if self._submitted:
            return
        self._submitted = True

        # Jika _resp_msg sudah ada (sudah ada pesan ephemeral sebelumnya), edit.
        # Jika belum (edge case: langsung konfirm dari summary pertama kali), send baru.
        if self._resp_msg is not None:
            await interaction.response.edit_message(
                content=f"⏳ Memproses claim `{self.nick}` [{self.tier}]...",
                view=None
            )
        else:
            await interaction.response.send_message(
                content=f"⏳ Memproses claim `{self.nick}` [{self.tier}]...",
                ephemeral=True
            )
            self._resp_msg = await interaction.original_response()

        tier_lower = get_panel(self.tier)
        counts     = claim_counts_high if tier_lower == "high" else claim_counts_medium
        queue      = pending_high if tier_lower == "high" else pending_medium
        processed  = processed_high if tier_lower == "high" else processed_medium

        # Cek max claim — berdasarkan count yang sudah confirmed success
        if self.user.id not in WHITELIST_IDS:
            if MAX_CLAIM > 0 and counts.get(self.user.id, 0) >= MAX_CLAIM:
                await interaction.edit_original_response(
                    content=f"⛔ Kamu sudah mencapai batas maksimal **{MAX_CLAIM}** claim ({self.tier})."
                )
                return

        log_ch_id  = LOG_CHANNEL_HIGH if tier_lower == "high" else LOG_CHANNEL_MED
        embed      = _build_claim_embed(self.nick, self.tier, tier_lower, self.choices, self.user)
        log_msg    = await _send_embed_to_channel(log_ch_id, embed)
        log_msg_id = log_msg.id if log_msg else None

        entry = {
            "nick":       self.nick,
            "tier_label": self.tier,
            "tier":       tier_lower,
            "choices":    self.choices,
            "author_id":  self.user.id,
            "log_msg_id": log_msg_id,
            "log_ch_id":  log_ch_id,
        }
        ip_key = _make_ip_key(entry)

        with lock:
            # Anti-duplikat: cek apakah entry ini sudah di queue/processed/in-progress
            already_pending    = any(
                _make_ip_key(e) == ip_key for e in queue
            )
            already_processed  = ip_key in processed
            already_inprogress = ip_key in (in_progress_high if tier_lower == "high" else in_progress_medium)
            if already_pending or already_processed or already_inprogress:
                log.warning(f"[SUBMIT-DUP] nick={self.nick} user={self.user.id} sudah ada di queue/processed/in-progress, skip.")
                await interaction.edit_original_response(
                    content=f"⚠️ Claim `{self.nick}` sudah ada di antrian atau sedang diproses. Tunggu sebentar."
                )
                _active_wizards.pop(self.user.id, None)
                return

            # PENTING: claim_count TIDAK ditambah di sini.
            # Count baru naik setelah GM kirim /result dengan status "success".
            # Jika gagal, tidak ada yang perlu di-rollback.

            # Guard final: pastikan nick tidak kosong sebelum masuk queue
            if not entry.get("nick", "").strip():
                log.warning(f"[SUBMIT-REJECT] Entry dibuang — nick kosong (user: {self.user.id})")
                await interaction.edit_original_response(
                    content="\u274c Nick tidak boleh kosong. Silakan coba claim lagi."
                )
                _active_wizards.pop(self.user.id, None)
                return

            queue.append(entry)
            _save_pending()

        log.info(f"[SUBMIT-{tier_lower.upper()}] + {self.nick} | tier: {self.tier} | author: {self.user.id} | choices: {self.choices}")

        await interaction.edit_original_response(
            content=f"✅ Claim `{self.nick}` [{self.tier}] berhasil disubmit! Cek {f'<#{log_ch_id}>' if log_ch_id else 'log channel'} untuk status."
        )
        _active_wizards.pop(self.user.id, None)

    # ── Restart ───────────────────────────────────────────────
    async def restart(self, interaction: discord.Interaction):
        self.nick             = ""
        self.tier             = ""
        self.choices          = {}
        self._step            = 0
        self._modal_submitted = False  # reset agar cleanup guard bekerja benar
        modal = NickTierModal(self)
        await interaction.response.send_modal(modal)

    # ── Batal ─────────────────────────────────────────────────
    async def cancel(self, interaction: discord.Interaction):
        if self._cancelled:
            return
        self._cancelled = True
        _active_wizards.pop(self.user.id, None)
        await interaction.response.edit_message(content="❌ Claim dibatalkan.", view=None)

    # ── Timeout ───────────────────────────────────────────────
    async def on_timeout(self):
        if self._submitted or self._cancelled:
            return
        _active_wizards.pop(self.user.id, None)
        if self._resp_msg:
            try:
                await self._resp_msg.edit(
                    content="⏱️ Form claim kadaluarsa (5 menit). Pilih paket lagi dari channel claim.",
                    view=None
                )
            except Exception:
                pass


# ── Registry wizard aktif (satu per user) ────────────────────
_active_wizards: dict[int, ClaimWizard] = {}

# ============================================================
#  PANEL FILE — atomic write untuk panel_msg.json
# ============================================================
PANEL_FILE = os.path.join(DATA_DIR, "panel_msg.json")

def _load_panel_msg_id() -> int | None:
    try:
        with open(PANEL_FILE, "r") as f:
            data = json.load(f)
            return data.get("message_id")
    except (FileNotFoundError, json.JSONDecodeError):
        return None

def _save_panel_msg_id(msg_id: int):
    """Atomic write — pakai .tmp + os.replace agar tidak korup saat di-kill."""
    try:
        tmp = PANEL_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"message_id": msg_id}, f)
        os.replace(tmp, PANEL_FILE)
    except Exception as e:
        log.warning(f"[WARN] Gagal simpan panel_msg.json: {e}")

_panel_msg_id: int | None = _load_panel_msg_id()


# ============================================================
#  PERSISTENT PANEL — embed + dropdown tier di CLAIM_CHANNEL
# ============================================================
def _build_panel_embed() -> discord.Embed:
    """Embed utama panel claim — edit di panel_channel_msg.py."""
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
    """Dropdown tier permanen di channel — survive restart via custom_id."""
    def __init__(self):
        options = [
            discord.SelectOption(
                label=t["label"],
                value=t["label"],
                emoji=t.get("emoji", "🎫")
            )
            for t in CLAIM_TIERS
        ]
        super().__init__(
            placeholder="Pilih paket claim...",
            options=options,
            custom_id="panel_tier_select",
        )

    async def callback(self, interaction: discord.Interaction):
        tier_label = self.values[0]
        self.values.clear()

        try:
            if interaction.user.id in BLACKLIST_IDS:
                await interaction.response.send_message(
                    "🚫 Kamu tidak dapat menggunakan fitur ini.", ephemeral=True
                )
                await self._reset_panel(interaction)
                return

            if interaction.user.id in _active_wizards:
                await interaction.response.send_message(
                    "⚠️ Kamu sudah punya form claim yang aktif. Selesaikan, tunggu timeout, atau batalkan dulu.",
                    view=CancelWizardView(interaction.user.id),
                    ephemeral=True
                )
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
            # 10062: interaction expired — user klik terlambat / Discord lag
            # Tidak berbahaya, cleanup wizard kalau sempat terbuat
            log.debug(f"[PANEL-SELECT] Interaction expired (10062) untuk user {interaction.user.id}")
            _active_wizards.pop(interaction.user.id, None)
        except discord.HTTPException as e:
            log.warning(f"[PANEL-SELECT] HTTP error saat respond interaction: {e}")
            _active_wizards.pop(interaction.user.id, None)

    async def _reset_panel(self, interaction: discord.Interaction):
        """Reset visual dropdown tanpa edit panel message (hindari rate limit 30046).

        Discord menyimpan opsi yang terakhir dipilih secara visual di sisi client.
        Tanpa reset ini, memilih opsi yang SAMA lagi tidak akan trigger callback.

        Solusi: kirim followup ephemeral kosong (zero-width space) dengan delete_after=0.
        Ini cukup untuk acknowledge interaction dan reset visual dropdown di client,
        tanpa menyentuh panel message yang sudah tua (>1 jam) sama sekali.
        """
        try:
            if interaction.response.is_done():
                # Modal / send_message sudah consume response — pakai followup
                await interaction.followup.send("\u200b", ephemeral=True, delete_after=0)
            else:
                # Belum ada response — defer saja
                await interaction.response.defer(ephemeral=True)
        except Exception as e:
            log.debug(f"[PANEL-RESET] {e}")  # expected kadang gagal, bukan error fatal


class PanelView(ui.View):
    """View permanen — timeout=None, custom_id pada Select survive restart."""
    def __init__(self):
        super().__init__(timeout=None)
        self.add_item(PanelTierSelect())


async def _setup_persistent_panel():
    global _panel_msg_id

    if not CLAIM_CHANNEL:
        log.warning("[PANEL] CLAIM_CHANNEL belum diset — panel tidak dipost.")
        return

    try:
        ch = client.get_channel(CLAIM_CHANNEL)
        if ch is None:
            ch = await client.fetch_channel(CLAIM_CHANNEL)
    except Exception as e:
        log.warning(f"[PANEL] Gagal fetch claim channel: {e}")
        return

    existing_msg = None
    if _panel_msg_id:
        try:
            existing_msg = await ch.fetch_message(_panel_msg_id)
            log.info(f"[PANEL] Pesan panel lama ditemukan (id={_panel_msg_id}) — re-attach view.")
        except discord.NotFound:
            log.info("[PANEL] Pesan panel lama tidak ditemukan — akan post baru.")
            _panel_msg_id = None
        except Exception as e:
            log.warning(f"[PANEL] Error fetch panel msg: {e}")
            _panel_msg_id = None

    view = PanelView()
    client.add_view(view, message_id=_panel_msg_id)

    if existing_msg:
        try:
            await existing_msg.edit(embed=_build_panel_embed(), view=view)
            log.info(f"[PANEL] Pesan panel diperbarui (id={_panel_msg_id}).")
        except Exception as e:
            log.warning(f"[PANEL] Gagal edit panel msg: {e}")
    else:
        try:
            msg = await ch.send(embed=_build_panel_embed(), view=view)
            _panel_msg_id = msg.id
            _save_panel_msg_id(msg.id)
            log.info(f"[PANEL] Pesan panel baru dipost (id={msg.id}).")
        except Exception as e:
            log.warning(f"[PANEL] Gagal post panel: {e}")

# ============================================================
#  FLASK AUTH
# ============================================================
def require_secret():
    token = request.headers.get("X-GCP-Token", "")
    if not secrets.compare_digest(token, FLASK_SECRET):
        return jsonify({"ok": False, "error": "Unauthorized"}), 403
    return None

# ============================================================
#  FLASK SERVER
# ============================================================
app = Flask(__name__)
CORS(app, origins=["chrome-extension://*"])

def _fmt_entry(e: dict) -> dict:
    return {
        "nick":       e["nick"],
        "tier_label": e.get("tier_label", ""),
        "tier":       e["tier"],
        "choices":    e["choices"],
        "log_msg_id": str(e["log_msg_id"]) if e.get("log_msg_id") else None,
        "log_ch_id":  str(e["log_ch_id"])  if e.get("log_ch_id")  else None,
        "gm_id":      e.get("gm_id"),
    }

def _process_result(queue: list, in_progress: dict, processed: OrderedDict, counts: dict, tier_label: str, data: dict):
    nick   = data.get("nick", "").strip()
    status = data.get("status", "failed")
    gm_id  = data.get("gm_id", "unknown")
    ip_key = data.get("ip_key", "").strip()

    log_ch_id = log_msg_id = None
    entry     = None

    # Tidak bail kalau nick kosong — tetap coba lookup via ip_key.
    # Entry lama yang nick-nya kosong (dari versi bot sebelumnya) tetap bisa diproses.
    if not nick and not ip_key:
        log.warning(f"[WARN-{tier_label}] nick DAN ip_key kosong dari gm={gm_id} — tidak bisa proses result")
        return None, None, None, status

    with lock:
        # Cari di in_progress dulu (via ip_key jika ada, fallback ke nick)
        if ip_key and ip_key in in_progress:
            info  = in_progress.pop(ip_key)
            entry = info["entry"]
        else:
            # Fallback: cari by nick di in_progress
            for k, v in list(in_progress.items()):
                if v["entry"]["nick"] == nick:
                    in_progress.pop(k)
                    entry = v["entry"]
                    ip_key = k
                    break

        if entry:
            # Hapus dari queue pending
            try:
                queue.remove(entry)
            except ValueError:
                pass  # sudah tidak ada (race condition, aman)

            log_ch_id  = entry.get("log_ch_id")
            log_msg_id = entry.get("log_msg_id")
            author_id  = entry["author_id"]
            log.info(f"[RESULT-{tier_label}] {nick} -> {status} (gm: {gm_id})")

            if status == "success":
                # Tambah count HANYA kalau sukses
                if author_id not in WHITELIST_IDS:
                    counts[author_id] = counts.get(author_id, 0) + 1
                    log.info(f"[COUNT-{tier_label}] {author_id}: {counts[author_id]} (status=success)")
                _save_claims()
            else:
                # Gagal — count tidak naik, tidak perlu rollback
                log.info(f"[COUNT-SKIP-{tier_label}] status={status}, count tidak dihitung untuk {author_id}")

            # Tandai processed agar tidak bisa di-submit ulang
            if ip_key:
                processed[ip_key] = None
                _trim_processed(processed)

            _save_pending()
            _save_processed()
        else:
            log.warning(f"[WARN-{tier_label}] entry tidak ditemukan di in_progress: nick={nick} ip_key={ip_key} gm={gm_id}")

    if entry and status in ("not_req", "already", "success", "not_found"):
        channel_key = "high" if tier_label == "HIGH" else "medium"
        schedule_dm(entry["author_id"], log_ch_id or 0, channel_key, status, nick)

    return entry, log_ch_id, log_msg_id, status

# ── HIGH endpoints ────────────────────────────────────────────
@app.route('/pending/take', methods=['POST'])
def take_pending_high():
    err = require_secret()
    if err: return err
    gm_id = (request.json or {}).get("gm_id", "unknown")
    with lock:
        _cleanup_expired_inprogress()
        if not pending_high:
            return jsonify({"entry": None, "queue_size": 0})
        # Cari entry pertama yang valid (nick tidak kosong) dan belum di-take GM lain
        entry = None
        corrupt = []
        for e in pending_high:
            if not e.get("nick", "").strip():
                corrupt.append(e)
                continue
            k = _make_ip_key(e)
            if k not in in_progress_high:
                entry = e
                break
        # Buang semua entry corrupt sekaligus
        for e in corrupt:
            try:
                pending_high.remove(e)
            except ValueError:
                pass
        if corrupt:
            log.warning(f"[PURGE-HIGH] {len(corrupt)} entry corrupt (nick kosong) dibuang dari queue")
            _save_pending()
        if not entry:
            return jsonify({"entry": None, "queue_size": 0})
        ip_key = _make_ip_key(entry)
        in_progress_high[ip_key] = {"entry": entry, "taken_at": datetime.now(timezone.utc), "gm_id": gm_id}
        # TIDAK pop dari pending_high — baru dihapus setelah /result diterima
        size = len(pending_high)
    log.info(f"[TAKE-HIGH] {entry['nick']} → gm:{gm_id} | queue: {size}")
    out = _fmt_entry(entry)
    out["ip_key"] = ip_key
    return jsonify({"entry": out, "queue_size": size})

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
        return jsonify({
            "pending": [e["nick"] for e in pending_high],
            "count":   len(pending_high),
        })

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
    log.info("[CLEAR-HIGH] antrian high dikosongkan")
    return jsonify({"ok": True})

# ── MEDIUM endpoints ──────────────────────────────────────────
@app.route('/pending_medium/take', methods=['POST'])
def take_pending_medium():
    err = require_secret()
    if err: return err
    gm_id = (request.json or {}).get("gm_id", "unknown")
    with lock:
        _cleanup_expired_inprogress()
        if not pending_medium:
            return jsonify({"entry": None, "queue_size": 0})
        # Cari entry pertama yang valid (nick tidak kosong) dan belum di-take GM lain
        entry = None
        corrupt = []
        for e in pending_medium:
            if not e.get("nick", "").strip():
                corrupt.append(e)
                continue
            k = _make_ip_key(e)
            if k not in in_progress_medium:
                entry = e
                break
        # Buang semua entry corrupt sekaligus
        for e in corrupt:
            try:
                pending_medium.remove(e)
            except ValueError:
                pass
        if corrupt:
            log.warning(f"[PURGE-MEDIUM] {len(corrupt)} entry corrupt (nick kosong) dibuang dari queue")
            _save_pending()
        if not entry:
            return jsonify({"entry": None, "queue_size": 0})
        ip_key = _make_ip_key(entry)
        in_progress_medium[ip_key] = {"entry": entry, "taken_at": datetime.now(timezone.utc), "gm_id": gm_id}
        size = len(pending_medium)
    log.info(f"[TAKE-MEDIUM] {entry['nick']} → gm:{gm_id} | queue: {size}")
    out = _fmt_entry(entry)
    out["ip_key"] = ip_key
    return jsonify({"entry": out, "queue_size": size})

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
        return jsonify({
            "pending": [e["nick"] for e in pending_medium],
            "count":   len(pending_medium),
        })

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
    log.info("[CLEAR-MEDIUM] antrian medium dikosongkan")
    return jsonify({"ok": True})

def run_flask():
    app.run(
        host="0.0.0.0",   # ← ganti dari 127.0.0.1
        port=FLASK_PORT,
        debug=False,
        use_reloader=False
    )

# ============================================================
#  BACKFILL — di-skip, antrian sudah persist via pending_queue.json
# ============================================================
async def _backfill_channel(channel_id: int, tier_key: str):
    log.info(f"[BACKFILL-{tier_key.upper()}] Skipped — antrian persist via pending_queue.json.")

# ============================================================
#  DISCORD BOT
# ============================================================
from discord.ext import commands

intents = discord.Intents.default()
intents.message_content = True
client = commands.Bot(command_prefix="!", intents=intents)

# ── Slash command /claim ──────────────────────────────────────
@client.tree.command(name="claim", description="Buka form claim item RF Online")
async def slash_claim(interaction: discord.Interaction):
    if CLAIM_CHANNEL and interaction.channel_id != CLAIM_CHANNEL:
        await interaction.response.send_message(
            f"❌ Command ini hanya bisa digunakan di <#{CLAIM_CHANNEL}>.",
            ephemeral=True
        )
        return

    if interaction.user.id in BLACKLIST_IDS:
        await interaction.response.send_message(
            "🚫 Kamu tidak dapat menggunakan fitur ini.", ephemeral=True
        )
        return

    if interaction.user.id in _active_wizards:
        await interaction.response.send_message(
            "⚠️ Kamu sudah punya form claim yang aktif. Selesaikan, tunggu timeout, atau batalkan dulu.",
            view=CancelWizardView(interaction.user.id),
            ephemeral=True
        )
        return

    wizard = ClaimWizard(user=interaction.user)
    _active_wizards[interaction.user.id] = wizard

    view = _ClaimTriggerView(wizard)
    await interaction.response.send_message(
        "🎫 Klik tombol di bawah untuk membuka form claim:",
        view=view,
        ephemeral=True
    )


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
            try:
                await interaction.message.edit(view=self)
            except Exception:
                pass
        except Exception as e:
            log.warning(f"[ERROR-TRIGGER] {e}")
            _active_wizards.pop(self.wizard.user.id, None)
            try:
                if not interaction.response.is_done():
                    await interaction.response.send_message(
                        "❌ Terjadi kesalahan. Pilih paket lagi dari channel claim.", ephemeral=True
                    )
            except Exception:
                pass


# ── Admin text commands ───────────────────────────────────────
@client.event
async def on_message(message: discord.Message):
    global _panel_msg_id  # deklarasi di awal fungsi — hindari SyntaxError Python
    if message.author.bot:
        return
    if message.author.id not in ADMIN_IDS:
        await client.process_commands(message)
        return

    cmd   = message.content.strip().lower()
    parts = message.content.strip().split(maxsplit=1)

    # ── !resetclaims ──────────────────────────────────────────
    if cmd == "!resetclaims":
        claim_counts_high.clear()
        claim_counts_medium.clear()
        _save_claims()
        sent = await message.reply("✅ Claim counts (High & Medium) direset.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    # ── !resetdmnick [nick] ───────────────────────────────────
    elif parts[0].lower() == "!resetdmnick":
        if len(parts) == 2:
            target = parts[1].strip().lower()
            with lock:
                keys_to_del = [k for k in dm_nick_counts if k.startswith(f"{target}:")]
                if keys_to_del:
                    for k in keys_to_del:
                        del dm_nick_counts[k]
                    _save_dm_nick_counts()
                    sent = await message.reply(
                        f"✅ DM counter untuk nick **{parts[1].strip()}** ({len(keys_to_del)} user) direset."
                    )
                else:
                    sent = await message.reply(f"⚠️ Nick **{parts[1].strip()}** tidak ada di DM counter.")
        else:
            with lock:
                dm_nick_counts.clear()
                _save_dm_nick_counts()
            sent = await message.reply("✅ Semua DM nick counter direset.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    # ── !resetpanel ───────────────────────────────────────────
    elif cmd == "!resetpanel":
        _panel_msg_id = None
        try:
            if os.path.exists(PANEL_FILE):
                os.remove(PANEL_FILE)
        except Exception:
            pass
        sent = await message.reply("🔄 Panel di-reset. Memposting ulang panel claim...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        await _setup_persistent_panel()

    # ── !updatepanel — force delete panel lama, kirim panel baru ─
    elif cmd == "!updatepanel":
        sent = await message.reply("🔄 Memperbarui panel...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        try:
            ch = client.get_channel(CLAIM_CHANNEL)
            if ch is None:
                ch = await client.fetch_channel(CLAIM_CHANNEL)
            # Hapus panel lama jika ada
            if _panel_msg_id:
                try:
                    old_msg = await ch.fetch_message(_panel_msg_id)
                    await old_msg.delete()
                    log.info(f"[PANEL] Panel lama (id={_panel_msg_id}) dihapus via !updatepanel")
                except discord.NotFound:
                    pass
                except Exception as e:
                    log.warning(f"[PANEL] Gagal hapus panel lama: {e}")
                _panel_msg_id = None
                _save_panel_msg_id(0)
            # Kirim panel baru
            view = PanelView()
            new_msg = await ch.send(embed=_build_panel_embed(), view=view)
            _panel_msg_id = new_msg.id
            _save_panel_msg_id(new_msg.id)
            client.add_view(view, message_id=new_msg.id)
            log.info(f"[PANEL] Panel baru dipost via !updatepanel (id={new_msg.id})")
            sent2 = await message.reply(f"✅ Panel berhasil diperbarui di <#{CLAIM_CHANNEL}>!")
        except Exception as e:
            log.warning(f"[PANEL] Gagal !updatepanel: {e}")
            sent2 = await message.reply(f"❌ Gagal update panel: {e}")
        asyncio.ensure_future(_auto_delete(sent2, AUTO_DELETE_SECONDS))

    # ── !listclaims ───────────────────────────────────────────
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

    # ── !status ───────────────────────────────────────────────
    elif cmd == "!status":
        with lock:
            h = len(pending_high)
            m = len(pending_medium)
        sent = await message.reply(
            f"📊 **Status antrian**\n"
            f"High   : {h} pending\n"
            f"Medium : {m} pending"
        )
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))

    # ── !synccmds — force sync slash commands ─────────────────
    elif cmd == "!synccmds":
        sent = await message.reply("🔄 Sync slash commands...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        _sync_flag = os.path.join(DATA_DIR, "sync_done.flag")
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

    # ── !restartbot ───────────────────────────────────────────
    elif cmd == "!restartbot":
        sent = await message.reply("🔄 Bot sedang restart...")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        await _graceful_shutdown("bot direstart")
        await asyncio.sleep(1)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    # ── !stopbot ──────────────────────────────────────────────
    elif cmd == "!stopbot":
        sent = await message.reply("❌ Bot dihentikan oleh admin.")
        asyncio.ensure_future(_auto_delete(sent, AUTO_DELETE_SECONDS))
        await _graceful_shutdown("bot dihentikan")

    await client.process_commands(message)


# ── on_disconnect ─────────────────────────────────────────────
@client.event
async def on_disconnect():
    log.info("[BOT] Koneksi Discord terputus (on_disconnect).")


# ── on_ready ──────────────────────────────────────────────────
@client.event
async def on_ready():
    global _bot_loop
    _bot_loop = asyncio.get_event_loop()

    # Signal handler — satu tempat saja, di sini, setelah loop siap
    def _sigint_override(signum, frame):
        if not _shutting_down and _bot_loop and not _bot_loop.is_closed():
            asyncio.run_coroutine_threadsafe(
                _graceful_shutdown("dihentikan oleh launcher"), _bot_loop
            )

    signal.signal(signal.SIGINT,  _sigint_override)
    signal.signal(signal.SIGTERM, _sigint_override)
    try:
        signal.signal(signal.SIGBREAK, _sigint_override)
    except AttributeError:
        pass

    # Sync slash commands — hanya dilakukan sekali (ada flag file).
    # Untuk force sync ulang, hapus file sync_done.flag atau pakai !synccmds.
    _sync_flag = os.path.join(DATA_DIR, "sync_done.flag")
    _need_sync = not os.path.exists(_sync_flag)
    if _need_sync:
        try:
            if GUILD_ID:
                guild = discord.Object(id=GUILD_ID)
                client.tree.copy_global_to(guild=guild)
                synced = await client.tree.sync(guild=guild)
                client.tree.clear_commands(guild=None)
                await client.tree.sync()
                log.info(f"[BOT] Slash commands synced ke guild {GUILD_ID}: {len(synced)} command(s)")
            else:
                synced = await client.tree.sync()
                log.info(f"[BOT] Slash commands synced global: {len(synced)} command(s)")
            # Tandai sudah sync agar restart berikutnya skip
            with open(_sync_flag, "w") as _f:
                _f.write(datetime.now(timezone.utc).isoformat())
        except Exception as e:
            log.warning(f"[BOT] Gagal sync slash commands: {e}")
    else:
        log.info("[BOT] Slash commands sync dilewati (sudah sync sebelumnya). Hapus sync_done.flag untuk force sync ulang.")

    log.info(f"[BOT] Login sebagai {client.user}")
    log.info(f"[BOT] Claim channel      : {CLAIM_CHANNEL or 'BELUM DISET'}")
    log.info(f"[BOT] Log channel High   : {LOG_CHANNEL_HIGH or 'BELUM DISET'}")
    log.info(f"[BOT] Log channel Medium : {LOG_CHANNEL_MED or 'BELUM DISET'}")
    log.info(f"[BOT] Admin IDs          : {ADMIN_IDS}")
    log.info(f"[BOT] Blacklist          : {len(BLACKLIST_IDS)} user(s)")
    log.info(f"[BOT] Whitelist          : {len(WHITELIST_IDS)} user(s)")
    log.info(f"[BOT] Max claim          : {MAX_CLAIM if MAX_CLAIM > 0 else 'UNLIMITED'}")
    log.info(f"[BOT] DM nick limit      : {DM_NICK_LIMIT}x per nick")
    log.info(f"[BOT] Auto-delete        : {AUTO_DELETE_SECONDS}d")
    log.info(f"[BOT] Log file           : {_log_file}")

    await _setup_persistent_panel()


# ============================================================
#  MAIN
# ============================================================
if __name__ == '__main__':
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    log.info(f"[SERVER] Flask server started at http://0.0.0.0:{FLASK_PORT}")
    log.info(f"[SERVER] Endpoints High   : POST /pending/take  POST /result  GET /status  POST /clear")
    log.info(f"[SERVER] Endpoints Medium : POST /pending_medium/take  POST /result_medium  GET /status_medium  POST /clear_medium")

    try:
        client.run(BOT_TOKEN, reconnect=True)
    except KeyboardInterrupt:
        if not _shutting_down and _bot_loop and not _bot_loop.is_closed():
            future = asyncio.run_coroutine_threadsafe(
                _graceful_shutdown("dihentikan oleh launcher"), _bot_loop
            )
            try:
                future.result(timeout=6)
            except Exception:
                pass
