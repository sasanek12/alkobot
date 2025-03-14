import os
import json
import logging
import datetime
from datetime import timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ---------------------------------------------
# KONFIGURACJA I STAŁE
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

BOT_PREFIX = "."
DATA_FILE = "data.json"

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True  # Pamiętaj o włączeniu w panelu Discord: Privileged Gateway Intents

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents,help_command=None)

VALID_TYPES = {"piwo", "wodka", "whiskey", "inne", "blunt"}
EMOJI_TO_TYPE = {
    "🍺": "piwo",
    "🥃": "whiskey",
    "🍸": "wodka",
    "🍷": "inne",
    "🚬": "blunt"
}
TYPE_TO_EMOJI = {v: k for k, v in EMOJI_TO_TYPE.items()}

# ---------------------------------------------
# DANE I ZMIENNE
# ---------------------------------------------
user_statuses = {}           # {user_id: {...}}
status_message_id = None     # do init_status_message
listening_channel_id = None  # None => bot słucha wszędzie

# ---------------------------------------------
# PLIKI JSON
# ---------------------------------------------
def ensure_data_file_exists():
    if not os.path.exists(DATA_FILE):
        logging.info(f"Plik {DATA_FILE} nie istnieje. Tworzę nowy.")
        base_data = {"settings": {"listening_channel_id": None}}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(base_data, f, ensure_ascii=False, indent=2)

def load_data():
    global user_statuses, listening_channel_id

    ensure_data_file_exists()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"Błąd wczytywania {DATA_FILE}: {e}")
        raw = {"settings": {"listening_channel_id": None}}

    settings = raw.get("settings", {})
    listening_channel_id = settings.get("listening_channel_id", None)

    temp_statuses = {}
    for user_id_str, data in raw.items():
        if user_id_str == "settings":
            continue
        try:
            user_id = int(user_id_str)
        except ValueError:
            continue

        expires_str = data.get("expires")
        if expires_str:
            try:
                dt = datetime.datetime.fromisoformat(expires_str)
                data["expires"] = dt
            except ValueError:
                data["expires"] = datetime.datetime.now(timezone.utc) + timedelta(hours=8)
        else:
            data["expires"] = datetime.datetime.now(timezone.utc) + timedelta(hours=8)

        temp_statuses[user_id] = data

    user_statuses.clear()
    user_statuses.update(temp_statuses)
    logging.info(f"Wczytano dane z pliku {DATA_FILE}.")

def save_data():
    to_save = {"settings": {"listening_channel_id": listening_channel_id}}
    for user_id, data in user_statuses.items():
        data_copy = dict(data)
        if isinstance(data.get("expires"), datetime.datetime):
            data_copy["expires"] = data["expires"].isoformat()
        to_save[str(user_id)] = data_copy

    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        logging.info(f"Zapisano dane do pliku {DATA_FILE}.")
    except OSError as e:
        logging.error(f"Błąd zapisu do {DATA_FILE}: {e}")

# ---------------------------------------------
# FUNKCJE POMOCNICZE
# ---------------------------------------------
def create_new_status(original_nick: str) -> dict:
    return {
        "original_nick": original_nick,
        "piwo": 0,
        "wodka": 0,
        "whiskey": 0,
        "inne": 0,
        "blunt": 0,
        "expires": datetime.datetime.now(timezone.utc) + timedelta(hours=8),
        "monthly_usage": {}
    }

def get_current_month() -> str:
    return datetime.datetime.now(timezone.utc).strftime("%Y-%m")

def ensure_monthly_record(data: dict, month: str):
    if "monthly_usage" not in data:
        data["monthly_usage"] = {}
    if month not in data["monthly_usage"]:
        data["monthly_usage"][month] = {
            "piwo": 0,
            "wodka": 0,
            "whiskey": 0,
            "inne": 0,
            "blunt": 0
        }

def build_usage_string(status_data: dict) -> str:
    """
    Skrócona forma np. 🍺5🥃2.
    """
    parts = []
    for typ in VALID_TYPES:
        count = status_data.get(typ, 0)
        if count > 0:
            emoji = TYPE_TO_EMOJI[typ]
            parts.append(f"{emoji}{count}")
    return "".join(parts)

async def update_nickname(member: discord.Member):
    """
    Modyfikuje pseudonim, np. "nick" na "nick 🍺5🥃2".
    """
    data = user_statuses.get(member.id)
    if not data:
        return

    original_nick = data.get("original_nick") or (member.nick or member.name)
    data["original_nick"] = original_nick

    usage_str = build_usage_string(data)
    new_nick = f"{original_nick} {usage_str}" if usage_str else original_nick

    if len(new_nick) > 32:
        new_nick = new_nick[:31] + "…"

    try:
        await member.edit(nick=new_nick)
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.warning(f"Nie udało się zmienić nicku {member.name}: {e}")

def find_user_in_guild(guild: discord.Guild, name_or_mention: str) -> discord.Member:
    """
    Znajduje użytkownika po wzmiance (np. <@123>), ID lub nazwie/nicku.
    """
    if not guild:
        return None

    mention_id = None
    if name_or_mention.startswith("<@") and name_or_mention.endswith(">"):
        mention_id_str = name_or_mention.strip("<@!>")
        if mention_id_str.isdigit():
            mention_id = int(mention_id_str)
    elif name_or_mention.isdigit():
        mention_id = int(name_or_mention)

    if mention_id is not None:
        return guild.get_member(mention_id)

    name_lower = name_or_mention.lower()
    for member in guild.members:
        if member.name.lower() == name_lower:
            return member
        if member.nick and member.nick.lower() == name_lower:
            return member
    return None

def can_add_for_others(member: discord.Member) -> bool:
    """
    Zwraca True, jeśli member ma uprawnienie Manage Nicknames lub jest administratorem.
    """
    if member.guild_permissions.administrator:
        return True
    return member.guild_permissions.manage_nicknames

def can_clear_others(member: discord.Member) -> bool:
    """
    Zwraca True, jeśli member ma uprawnienie Manage Nicknames lub jest administratorem.
    """
    if member.guild_permissions.administrator:
        return True
    return member.guild_permissions.manage_nicknames

# ---------------------------------------------
# BUDOWANIE TEKSTU LEADERBOARD
# ---------------------------------------------
def build_leaderboard_text(guild: discord.Guild) -> str:
    current_month = get_current_month()
    usage_list = []

    # Zbieramy dane z user_statuses
    for user_id, data in user_statuses.items():
        monthly_data = data.get("monthly_usage", {})
        stats = monthly_data.get(current_month, {})
        total_used = sum(stats.values())
        if total_used > 0:
            usage_list.append((user_id, stats, total_used))

    # Sort malejąco
    usage_list.sort(key=lambda x: x[2], reverse=True)

    if not usage_list:
        return f"Nikt nie ma punktów w miesiącu {current_month}."

    lines = []
    position = 1
    for user_id, stats, total_used in usage_list:
        member = guild.get_member(user_id)
        mention_str = member.mention if member else f"<@{user_id}>"

        detail_parts = []
        for t in VALID_TYPES:
            val = stats.get(t, 0)
            if val > 0:
                detail_parts.append(f"{TYPE_TO_EMOJI[t]}{val}")
        detail_str = "".join(detail_parts) or "Brak"

        lines.append(f"**{position})** {mention_str} ({detail_str}) - Suma: {total_used}")
        position += 1

    leaderboard_text = "\n".join(lines)
    return f"**Tabela wyników za {current_month}**:\n{leaderboard_text}"

# ---------------------------------------------
# EVENT on_ready i pętla czyszcząca
# ---------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Zalogowano jako {bot.user}")
    load_data()
    await bot.change_presence(activity=discord.Game(name=f"Prefix: {BOT_PREFIX}"))
    clean_statuses.start()

    try:
        await bot.tree.sync()
        logging.info("Zarejestrowano slash commands.")
    except Exception as e:
        logging.warning(f"Nie udało się zsynchronizować slash commands: {e}")

@tasks.loop(minutes=1)
async def clean_statuses():
    now = datetime.datetime.now(timezone.utc)
    to_remove = []
    for user_id, data in user_statuses.items():
        if now > data["expires"]:
            to_remove.append(user_id)
    for user_id in to_remove:
        user_statuses.pop(user_id, None)
    if to_remove:
        save_data()

# ---------------------------------------------
# EVENT: on_message (filtr kanału)
# ---------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if listening_channel_id is not None and message.channel.id != listening_channel_id:
        return
    await bot.process_commands(message)

# ---------------------------------------------
# KOMENDY .HELP i /HELP
# ---------------------------------------------
def get_help_text() -> str:
    return (
        f"**Komendy (prefix: {BOT_PREFIX})**:\n"
        f"{BOT_PREFIX}help - Wyświetla tę pomoc\n"
        f"{BOT_PREFIX}add <typ> <ilość> - Dodaje do Twojego statusu\n"
        f"{BOT_PREFIX}add <nick> <typ> <ilość> - Dodaje do cudzego statusu (wymaga Manage Nicknames / Admin)\n"
        f"{BOT_PREFIX}status - Wyświetla Twój status\n"
        f"{BOT_PREFIX}clear [<nick>] - Czyści Twój status lub czyjś (Manage Nicknames / Admin)\n"
        f"{BOT_PREFIX}leaderboard [hide] - Wyświetla tabelę wyników; z 'hide' wyśle w DM\n"
        f"{BOT_PREFIX}init_status_message - Tworzy wiadomość z reakcjami\n"
        f"{BOT_PREFIX}setchannel <kanał> - Ustawia kanał nasłuchu (admin)\n\n"
        "**Slash commands**:\n"
        "/leaderboard (hide=False) - publicznie, (hide=True) - ephemeral\n"
        "/help - pokazuje tę pomoc\n"
        "/ping - test ping\n"
    )

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.send(get_help_text())

@bot.tree.command(name="help", description="Wyświetla opis najważniejszych komend bota")
async def help_slash_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(get_help_text())

# ---------------------------------------------
# KOMENDA .ADD
# ---------------------------------------------
@bot.command()
async def add(ctx: commands.Context, *args):
    """
    .add <typ> <ilość> - dodaje do Twojego statusu
    .add <nick> <typ> <ilość> - dodaje do cudzego statusu (wymaga Manage Nicknames / Admin)
    """
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze (guild).")
        return

    if len(args) == 2:
        typ, ilosc_str = args
        member = ctx.guild.get_member(ctx.author.id)
    elif len(args) == 3:
        name_or_mention, typ, ilosc_str = args
        # SPRAWDZAMY UPRAWNIENIA
        if not can_add_for_others(ctx.author):
            await ctx.send("Nie masz uprawnień (Manage Nicknames / Admin), by dodawać innym.")
            return
        member = find_user_in_guild(ctx.guild, name_or_mention)
        if member is None:
            await ctx.send(f"Nie znaleziono użytkownika: {name_or_mention}")
            return
    else:
        await ctx.send("Poprawne użycie: .add <typ> <ilość> lub .add <nick> <typ> <ilość>")
        return

    try:
        ilosc = int(ilosc_str)
    except ValueError:
        await ctx.send("Podaj liczbę jako ilość, np. .add piwo 2 lub .add @Marcin piwo 2")
        return

    typ = typ.lower()
    if typ not in VALID_TYPES:
        await ctx.send(f"Nieznany typ! Dozwolone: {', '.join(VALID_TYPES)}.")
        return

    if member.id not in user_statuses:
        user_statuses[member.id] = create_new_status(member.nick or member.name)

    data = user_statuses[member.id]
    data[typ] += ilosc
    data["expires"] = datetime.datetime.now(timezone.utc) + timedelta(hours=8)

    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += ilosc

    if member.id == ctx.author.id:
        await ctx.send(f"Dodano **{ilosc}** do **{typ}** dla {ctx.author.mention}.")
    else:
        await ctx.send(
            f"Dodano **{ilosc}** do **{typ}** dla użytkownika {member.mention} (wywołane przez {ctx.author.mention})."
        )
    await update_nickname(member)
    save_data()

@add.error
async def add_error(ctx: commands.Context, error):
    logging.error(f"Błąd w komendzie .add: {error}")
    if isinstance(error, commands.BadArgument):
        await ctx.send("Podaj liczbę jako ilość, np. `.add piwo 2`.")
    else:
        await ctx.send(f"Wystąpił błąd: {error}")

# ---------------------------------------------
# KOMENDA .STATUS
# ---------------------------------------------
@bot.command()
async def status(ctx: commands.Context):
    data = user_statuses.get(ctx.author.id)
    if not data:
        await ctx.send("Nie masz obecnie żadnego statusu.")
        return

    now = datetime.datetime.now(timezone.utc)
    expires_in = data["expires"] - now
    hours, remainder = divmod(int(expires_in.total_seconds()), 3600)
    minutes, _ = divmod(remainder, 60)

    msg = (
        f"**Twój status**:\n"
        f"• Piwo: {data['piwo']}\n"
        f"• Wódka: {data['wodka']}\n"
        f"• Whiskey: {data['whiskey']}\n"
        f"• Inne: {data['inne']}\n"
        f"• Blunty: {data['blunt']}\n\n"
        f"Wygasa za: {hours}h {minutes}min."
    )
    await ctx.send(msg)

# ---------------------------------------------
# KOMENDA .CLEAR
# ---------------------------------------------
@bot.command()
async def clear(ctx: commands.Context, user_arg: str = None):
    """
    .clear - czyści Twój status
    .clear <nick> - czyści czyjś status (Manage Nicknames / Admin)
    """
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze (guild).")
        return

    # 1) Bez argumentu -> czyści własny status
    if user_arg is None:
        if ctx.author.id not in user_statuses:
            await ctx.send("Nie masz obecnie żadnego statusu do wyczyszczenia.")
            return

        data = user_statuses.pop(ctx.author.id)
        member = ctx.guild.get_member(ctx.author.id)
        if member:
            original_nick = data.get("original_nick") or (member.nick or member.name)
            if len(original_nick) > 32:
                original_nick = original_nick[:31] + "…"
            try:
                await member.edit(nick=original_nick)
            except (discord.Forbidden, discord.HTTPException) as e:
                logging.warning(f"Nie udało się przywrócić nicku {member.name}: {e}")

        await ctx.send("Twój status został wyczyszczony.")
        save_data()
        return

    # 2) Z argumentem -> czyścimy status innego usera (wymaga Manage Nicknames / Admin)
    if not can_clear_others(ctx.author):
        await ctx.send("Nie masz uprawnień (Manage Nicknames / Admin), by czyścić statusy innych.")
        return

    target = find_user_in_guild(ctx.guild, user_arg)
    if not target:
        await ctx.send(f"Nie znaleziono użytkownika: {user_arg}")
        return

    if target.id not in user_statuses:
        await ctx.send(f"Użytkownik {target.mention} nie ma żadnego statusu do wyczyszczenia.")
        return

    data = user_statuses.pop(target.id)
    original_nick = data.get("original_nick") or (target.nick or target.name)
    if len(original_nick) > 32:
        original_nick = original_nick[:31] + "…"
    try:
        await target.edit(nick=original_nick)
    except (discord.Forbidden, discord.HTTPException) as e:
        logging.warning(f"Nie udało się przywrócić nicku {target.name}: {e}")

    await ctx.send(f"Status użytkownika {target.mention} został wyczyszczony (wywołane przez {ctx.author.mention}).")
    save_data()

# ---------------------------------------------
# KOMENDA .LEADERBOARD [HIDE]
# ---------------------------------------------
@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx: commands.Context, hide_arg: str = None):
    """
    .leaderboard [hide]
      - bez hide -> wyniki publiczne
      - z hide  -> wyniki w DM
    """
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze (guild).")
        return

    leaderboard_text = build_leaderboard_text(ctx.guild)

    if hide_arg == "hide":
        # Wysyłamy na priv
        try:
            await ctx.author.send(leaderboard_text)
            await ctx.send(f"Sprawdź prywatną wiadomość, {ctx.author.mention}!")
        except discord.Forbidden:
            await ctx.send(f"Nie mogę wysłać prywatnej wiadomości do {ctx.author.mention}.")
    else:
        # Publicznie
        await ctx.send(leaderboard_text)

# ---------------------------------------------
# SLASH: /LEADERBOARD (hide: bool)
# ---------------------------------------------
@bot.tree.command(name="leaderboard", description="Wyświetla tabelę wyników")
@app_commands.describe(
    hide="Czy wiadomość ma być wysłana dyskretnie (ephemeral)? Domyślnie: False."
)
async def leaderboard_slash(interaction: discord.Interaction, hide: bool = False):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze (guild).", ephemeral=True)
        return

    leaderboard_text = build_leaderboard_text(interaction.guild)
    # ephemeral = hide => jeśli hide=True, wiadomość jest ukryta, widoczna tylko dla wywołującego
    await interaction.response.send_message(content=leaderboard_text, ephemeral=hide)

# ---------------------------------------------
# KOMENDA .INIT_STATUS_MESSAGE
# ---------------------------------------------
@bot.command()
async def init_status_message(ctx: commands.Context):
    global status_message_id

    text = (
        "**Kliknij w reakcję, aby dodać spożycie**:\n"
        "🍺 — Piwo\n"
        "🥃 — Whiskey\n"
        "🍸 — Wódka\n"
        "🍷 — Inne\n"
        "🚬 — Blunt\n"
        "❌ — Wyczyść status"
    )
    message = await ctx.send(text)
    status_message_id = message.id

    for emoji in EMOJI_TO_TYPE:
        await message.add_reaction(emoji)
    await message.add_reaction("❌")

# ---------------------------------------------
# KOMENDA .SETCHANNEL
# ---------------------------------------------
@bot.command()
async def setchannel(ctx: commands.Context, channel: discord.TextChannel):
    if not ctx.author.guild_permissions.administrator:
        await ctx.send("Tylko administrator może to zrobić.")
        return

    global listening_channel_id
    listening_channel_id = channel.id
    save_data()
    await ctx.send(f"Ustawiono kanał nasłuchu na {channel.mention}.")

# ---------------------------------------------
# OBSŁUGA REAKCJI
# ---------------------------------------------
@bot.event
async def on_reaction_add(reaction: discord.Reaction, user: discord.User):
    global status_message_id
    if user.bot:
        return
    if reaction.message.id != status_message_id:
        return

    guild = reaction.message.guild
    if not guild:
        return

    member = guild.get_member(user.id)
    if not member:
        return

    emoji = str(reaction.emoji)
    if emoji == "❌":
        if member.id in user_statuses:
            data = user_statuses.pop(member.id)
            original_nick = data.get("original_nick") or (member.nick or member.name)
            if len(original_nick) > 32:
                original_nick = original_nick[:31] + "…"
            try:
                await member.edit(nick=original_nick)
            except (discord.Forbidden, discord.HTTPException) as e:
                logging.warning(f"Nie udało się przywrócić nicku {member.name}: {e}")

            await reaction.message.channel.send(f"{user.mention} - Twój status został wyczyszczony.")
            save_data()
        await reaction.remove(user)
        return

    if emoji not in EMOJI_TO_TYPE:
        await reaction.remove(user)
        return

    typ = EMOJI_TO_TYPE[emoji]
    if member.id not in user_statuses:
        user_statuses[member.id] = create_new_status(member.nick or member.name)

    data = user_statuses[member.id]
    data[typ] += 1
    data["expires"] = datetime.datetime.now(timezone.utc) + timedelta(hours=8)

    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += 1

    await reaction.message.channel.send(f"{user.mention} dodał +1 do **{typ}**.")
    await update_nickname(member)
    save_data()
    await reaction.remove(user)

# ---------------------------------------------
# PRZYKŁADOWA KOMENDA /ping
# ---------------------------------------------
@bot.tree.command(name="ping", description="Testowa komenda slash – odpowiada 'Pong!'")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

# ---------------------------------------------
# START BOTA
# ---------------------------------------------
if __name__ == "__main__":
    token = os.getenv("DISCORD_TOKEN", "TWOJ_TOKEN_DISCORDA")
    bot.run(token)
