import discord

def _build_panel_embed() -> discord.Embed:
    embed = discord.Embed(
        title="🎫 Claim Event RF Warden v2",
        description=(
            "**Requirement Claim Event Monthsary v2:**\n"
            "• CPT Minimum: 50000\n"
            "• Level Minimum: 66\n"
            "• Max Claim per ID: 1\n"
            "More Info Click [HERE](https://discord.com/channels/963096419735044137/1515394182695354518)\n\n"
            "**Requirement Claim Event Newplayer v2:**\n"
            "• CPT Minimum: 15000\n"
            "• Level Minimum: 40\n"
            "• Max Claim per ID: 1\n"
            "More Info Click [HERE](https://discord.com/channels/963096419735044137/1515394162894045284)\n\n"
            "**Cara Penggunaan:**\n"
            "• Pilih Event dari dropdown di bawah\n"
            "• Isi form yang muncul\n"
            "• Submit dan tunggu admin memproses claimmu\n\n"
            "**⚠️ Peringatan:**\n"
            "• Setiap akun hanya boleh claim sesuai batas yang ditentukan\n"
            "• Pastikan nick in-game yang kamu masukkan sudah benar\n"
            "• Penyalahgunaan akan dikenakan sanksi"
        ),
        color=discord.Color.purple()
    )
    embed.set_footer(text="**Klik dropdown di bawah untuk memulai claim**")
    return embed