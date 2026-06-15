"""
Jalankan script ini SEKALI untuk menghapus semua global slash commands.
Setelah selesai, jalankan bot seperti biasa.

Usage:
    python clear_commands.py
"""
import asyncio
import os

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

import discord
from discord.ext import commands

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
GUILD_ID  = int(os.environ.get("GUILD_ID", "0"))

intents = discord.Intents.default()
bot     = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    print(f"[CLEAR] Login sebagai {bot.user}")

    # 1. Hapus semua GLOBAL commands
    bot.tree.clear_commands(guild=None)
    await bot.tree.sync()
    print("[CLEAR] Global commands dihapus.")

    # 2. Hapus semua GUILD commands (kalau ada sisa lama)
    if GUILD_ID:
        guild = discord.Object(id=GUILD_ID)
        bot.tree.clear_commands(guild=guild)
        await bot.tree.sync(guild=guild)
        print(f"[CLEAR] Guild commands dihapus untuk guild {GUILD_ID}.")

    print("[CLEAR] Selesai! Sekarang jalankan bot utama (nick_watcher.py).")
    print("[CLEAR] Slash command /claim akan muncul dalam beberapa detik setelah bot utama jalan.")
    await bot.close()

bot.run(BOT_TOKEN)
