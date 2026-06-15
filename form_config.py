# ============================================================
#  FORM CONFIG — Edit file ini untuk ubah isi form claim
#
#  Setiap item di FORM_ITEMS_HIGH / FORM_ITEMS_MEDIUM akan
#  menjadi satu step dropdown di Discord.
#  Urutan list = urutan step di form.
#
#  Field tiap item:
#    key      : nama internal (jangan pakai spasi)
#    label    : teks yang tampil di Discord
#    required : True = wajib diisi, False = bisa di-skip
#    options  : list pilihan yang tampil di dropdown
#               (max 25 per Discord limitation)
# ============================================================

# ── Tier / Package — label bebas, panel = "high" atau "medium" ──
CLAIM_TIERS = [
    {"label": "Event Newplayer v2",  "panel": "medium", "emoji": "🎮"},
    {"label": "Special Monthsary v2",    "panel": "high",   "emoji": "🎆"},
]

# ── Helper: ambil panel ("high"/"medium") dari label ─────────
def get_panel(label: str) -> str:
    """Kembalikan 'high' atau 'medium' berdasarkan label tier."""
    for t in CLAIM_TIERS:
        if t["label"] == label:
            return t["panel"]
    return "medium"  # fallback aman

# ── Timeout form (detik) ─────────────────────────────────────
FORM_TIMEOUT = 120  # 5 menit

# ============================================================
#  FORM ITEMS — HIGH
#  Edit bagian ini untuk mengubah item form tier High
# ============================================================
FORM_ITEMS_HIGH = [
    {
        "key":      "excellent_weapon_permanent",
        "label":    "[1] Weapons Excellent 3% Permanent [+7/7 Ignorant]",
        "required": True,
        "options": [
            "Excellent Back Blade Custom [3%]",
            "Excellent Soul Eater [3%]",
            "Excellent Armor Killer Type 2 [3%]",
            "Excellent Apocalypse [3%]",
            "Excellent Baltin Toel [3%]",
            "Excellent Blunt of Repose [3%]",
            "Excellent Divine Core [3%]",
            "Excellent Name of Vengeance [3%]",
            "Excellent Aldebaran [3%]",
            "Excellent Beleropon [3%]",
            "Excellent Twin Impact [3%]",
            "Excellent M-72A3 Shadow Bullet [3%]",
            "Excellent MG-L-073GA ThunderBolt [3%]",
            "Excellent Beat of Beast [3%]",
            "Excellent Judecker [3%]",
            "Excellent Revanthane [3%]",
            "Excellent Tartharos [3%]",
            "Excellent Lighthalzen [3%]",
            "Excellent Veins [3%]",
        ],
    },
    {
        "key":      "leon_high_weapon",
        "label":    "[2] Leon High [+7/7 Chaos]",
        "required": True,
        "options": [
            "Leon's Gun Blade",
            "Leon's Beam Siege Bow",
            "Leon's Black Stickbead",
        ],
    },
]

# ============================================================
#  FORM ITEMS — MEDIUM
#  Edit bagian ini untuk mengubah item form tier Medium
# ============================================================
FORM_ITEMS_MEDIUM = []

# ── Alias FORM_ITEMS → default ke High (backward compat) ─────
FORM_ITEMS = FORM_ITEMS_HIGH

# ── Helper: ambil form items berdasarkan label tier ───────────
def get_form_items(tier_label: str) -> list:
    """
    Kembalikan list form items sesuai tier.
    Cek panel tier → 'high' pakai FORM_ITEMS_HIGH,
                    → 'medium' pakai FORM_ITEMS_MEDIUM.
    """
    panel = get_panel(tier_label)
    if panel == "high":
        return FORM_ITEMS_HIGH
    return FORM_ITEMS_MEDIUM

# ── Helper: ambil item by key (dari pool gabungan) ────────────
def get_item(key: str) -> dict | None:
    all_items = {i["key"]: i for i in FORM_ITEMS_HIGH + FORM_ITEMS_MEDIUM}
    return all_items.get(key)

def get_options(key: str) -> list[str]:
    item = get_item(key)
    return item["options"] if item else []