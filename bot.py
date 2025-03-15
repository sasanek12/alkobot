import os
from dotenv import load_dotenv
import json
import math
import logging
import datetime
from datetime import timezone, timedelta
import discord
from discord.ext import commands, tasks
from discord import app_commands

# ---------------------------------------------
# KONFIGURACJA, STAŁE
# ---------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s - %(message)s"
)

BOT_PREFIX = "."
DATA_FILE = "data.json"
NBSP = "\u00A0"  # non-breakable space separator

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True  # Włącz w panelu dewelopera

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

VALID_TYPES = {"piwo", "wodka", "whiskey", "wino", "drink", "blunt"}

# Czas (w godzinach) po jakim "schodzi" ostatnio dodana porcja danego typu:
TIME_TO_EXPIRE = {
    "piwo": 3,
    "wodka": 2,
    "whiskey": 2,
    "wino": 2,
    "drink": 2,
    "blunt": 4
}

# Emotki do typów, i odwrotnie:
EMOJI_TO_TYPE = {
    "🍺": "piwo",
    "🥃": "whiskey",
    "🍸": "wodka",
    "🍷": "wino",
    "🍹": "drink",
    "🍃": "blunt"
}
TYPE_TO_EMOJI = {v: k for k, v in EMOJI_TO_TYPE.items()}

# ---------------------------------------------
# DANE, ZMIENNE GLOBALNE
# ---------------------------------------------
user_statuses = {}           # {user_id: {...}}
status_message_id = None     # ID wiadomości z init_status_message
listening_channel_id = None  # None => bot słucha w każdym kanale

# Zmienna do "żywego" leaderboardu
live_leaderboard_message_id = None
live_leaderboard_channel_id = None

# ---------------------------------------------
# ŁADOWANIE / ZAPIS PLIKU JSON
# ---------------------------------------------
def ensure_data_file_exists():
    if not os.path.exists(DATA_FILE):
        logging.info(f"Plik {DATA_FILE} nie istnieje. Tworzę go.")
        base_data = {"settings": {"listening_channel_id": None}}
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(base_data, f, ensure_ascii=False, indent=2)

def load_data():
    """
    Ładuje dane z pliku JSON do user_statuses i zmiennych globalnych
    (listening_channel_id, live_leaderboard_message_id, live_leaderboard_channel_id).
    """
    global user_statuses
    global listening_channel_id
    global live_leaderboard_message_id
    global live_leaderboard_channel_id

    ensure_data_file_exists()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"Błąd wczytywania {DATA_FILE}: {e}")
        raw = {"settings": {"listening_channel_id": None}}

    settings = raw.get("settings", {})
    listening_channel_id = settings.get("listening_channel_id", None)
    live_leaderboard_message_id = settings.get("live_leaderboard_message_id")
    live_leaderboard_channel_id = settings.get("live_leaderboard_channel_id")

    # Odczyt user_statuses
    temp_statuses = {}
    for user_id_str, data in raw.items():
        if user_id_str == "settings":
            continue

        try:
            user_id = int(user_id_str)
        except ValueError:
            continue

        # Konwertujemy expires na datetime
        eps = data.get("expires_per_substance", {})
        for typ, val in eps.items():
            if val is not None:
                try:
                    eps[typ] = datetime.datetime.fromisoformat(val)
                except ValueError:
                    eps[typ] = None
        data["expires_per_substance"] = eps

        temp_statuses[user_id] = data

    user_statuses.clear()
    user_statuses.update(temp_statuses)
    logging.info(f"Wczytano dane z pliku {DATA_FILE}.")

def save_data():
    """
    Zapisuje user_statuses i zmienne globalne do data.json
    """
    to_save = {"settings": {}}
    to_save["settings"]["listening_channel_id"] = listening_channel_id
    to_save["settings"]["live_leaderboard_message_id"] = live_leaderboard_message_id
    to_save["settings"]["live_leaderboard_channel_id"] = live_leaderboard_channel_id

    for user_id, data in user_statuses.items():
        data_copy = dict(data)
        if "expires_per_substance" in data_copy:
            eps_dict = {}
            for typ, dt_value in data_copy["expires_per_substance"].items():
                if isinstance(dt_value, datetime.datetime):
                    eps_dict[typ] = dt_value.isoformat()
                else:
                    eps_dict[typ] = None
            data_copy["expires_per_substance"] = eps_dict
        to_save[str(user_id)] = data_copy

    try:
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(to_save, f, ensure_ascii=False, indent=2)
        logging.info(f"Zapisano dane do {DATA_FILE}.")
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
        "wino": 0,
        "drink": 0,
        "blunt": 0,
        "monthly_usage": {},
        "expires_per_substance": {
            "piwo": None,
            "wodka": None,
            "whiskey": None,
            "wino": None,
            "drink": None,
            "blunt": None
        }
    }

def remove_bot_suffix(nick: str) -> str:
    """
    Jeśli w nicku jest NBSP (\u00A0), usuwa wszystko od NBSP włącznie.
    """
    if not nick:
        return nick
    idx = nick.find(NBSP)
    if idx != -1:
        return nick[:idx]
    return nick

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
            "wino": 0,
            "drink": 0,
            "blunt": 0
        }

def build_usage_string(status_data: dict) -> str:
    """
    Buduje np. "🍺2🥃1" na podstawie stanu w status_data (VALID_TYPES).
    """
    parts = []
    for typ in VALID_TYPES:
        count = status_data.get(typ, 0)
        if count > 0:
            emoji = TYPE_TO_EMOJI[typ]
            parts.append(f"{emoji}{count}")
    return "".join(parts)

async def update_nickname(member: discord.Member, source="command"):
    """
    Ustawia nowy pseudonim: oryginalny + NBSP + emotki
    Zabezpieczenie na 32 znaki.
    W razie braku uprawnień do zmiany nicku właściciela – wysyła DM z propozycją.
    """
    data = user_statuses.get(member.id)
    if not data:
        return

    pure_original = remove_bot_suffix(data.get("original_nick") or (member.nick or member.name))
    data["original_nick"] = pure_original

    usage_str = build_usage_string(data)
    if usage_str:
        new_nick = f"{pure_original}{NBSP}{usage_str}"
    else:
        new_nick = pure_original

    if len(new_nick) > 32:
        new_nick = new_nick[:31] + "…"

    try:
        await member.edit(nick=new_nick)
    except discord.Forbidden:
        # Jeśli to właściciel – wyślij DM z propozycją
        if member.guild.owner_id == member.id:
            try:
                if source == "command":
                    await member.send(
                        f"🔔 **Propozycja zmiany nicku:** `{new_nick}`\n"
                        f"👉 Użyj na serwerze komendy:\n"
                        f"```/nick \"{new_nick}\"```"
                    )
                elif source == "expire":
                    await member.send(
                        f"⏳ Czas dla Twojego statusu minął. Sugerowana nazwa: `{new_nick}`\n"
                        f"👉 Użyj na serwerze komendy:\n"
                        f"```/nick \"{new_nick}\"```"
                    )
            except discord.Forbidden:
                logging.warning(f"Nie udało się wysłać DM do właściciela ({member.name}).")
        else:
            logging.warning(f"Brak uprawnień do zmiany nicku {member.name}.")

def find_user_in_guild(guild: discord.Guild, name_or_mention: str) -> discord.Member:
    """
    Wyszukuje usera po:
    - wzmiance <@123456789>
    - ID
    - `member.name` lub `member.nick`
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
    return (member.guild_permissions.administrator or
            member.guild_permissions.manage_nicknames)

def can_clear_others(member: discord.Member) -> bool:
    return (member.guild_permissions.administrator or
            member.guild_permissions.manage_nicknames)

def build_leaderboard_text(guild: discord.Guild) -> str:
    """
    Zwraca tekstową wersję leaderboardu, bazując na oryginalnym nicku (bez emotek).
    """
    current_month = get_current_month()
    usage_list = []
    for user_id, data in user_statuses.items():
        monthly = data.get("monthly_usage", {})
        stats = monthly.get(current_month, {})
        total_used = sum(stats.values())
        if total_used > 0:
            usage_list.append((user_id, stats, total_used))

    usage_list.sort(key=lambda x: x[2], reverse=True)

    if not usage_list:
        return f"Nikt nie ma punktów w miesiącu {current_month}."

    lines = []
    pos = 1
    for user_id, stats, total_used in usage_list:
        data = user_statuses[user_id]
        # Używamy oryginalnego nicku z pliku, aby nie dodawać emotek
        original_nick = remove_bot_suffix(data.get("original_nick", ""))
        if not original_nick:
            # fallback:
            member = guild.get_member(user_id)
            original_nick = member.display_name if member else f"<@{user_id}>"

        detail_parts = []
        for t in VALID_TYPES:
            val = stats.get(t, 0)
            if val > 0:
                detail_parts.append(f"{TYPE_TO_EMOJI[t]}{val}")
        detail_str = "".join(detail_parts) or "Brak"

        lines.append(f"**{pos})** {original_nick} ({detail_str}) - Suma: {total_used}")
        pos += 1

    return f"**Tabela wyników za {current_month}**:\n" + "\n".join(lines)

def build_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    """
    Zwraca obiekt Embed z leaderboardem – także używa "original_nick".
    """
    current_month = get_current_month()
    usage_list = []
    for user_id, data in user_statuses.items():
        monthly = data.get("monthly_usage", {})
        stats = monthly.get(current_month, {})
        total_used = sum(stats.values())
        if total_used > 0:
            usage_list.append((user_id, stats, total_used))

    usage_list.sort(key=lambda x: x[2], reverse=True)

    embed = discord.Embed(
        title="Aktualizowany Leaderboard",
        description=f"Wyniki miesiąca: {current_month}",
        color=discord.Color.blue()
    )

    if not usage_list:
        embed.add_field(name="Brak danych", value="Nikt nie ma punktów w tym miesiącu", inline=False)
        return embed

    pos = 1
    for user_id, stats, total_used in usage_list:
        data = user_statuses[user_id]
        original_nick = remove_bot_suffix(data.get("original_nick", ""))

        if not original_nick:
            member = guild.get_member(user_id)
            original_nick = member.display_name if member else f"<@{user_id}>"

        detail_parts = []
        for t in VALID_TYPES:
            val = stats.get(t, 0)
            if val > 0:
                detail_parts.append(f"{TYPE_TO_EMOJI[t]}{val}")
        detail_str = "".join(detail_parts) or "Brak"

        embed.add_field(
            name=f"{pos}) {original_nick}",
            value=f"{detail_str} | Suma: {total_used}",
            inline=False
        )
        pos += 1

    return embed

# ---------------------------------------------
# ZADANIE: update_live_leaderboard
# ---------------------------------------------
@tasks.loop(minutes=1)
async def update_live_leaderboard():
    if not live_leaderboard_channel_id or not live_leaderboard_message_id:
        return

    for g in bot.guilds:
        channel = g.get_channel(live_leaderboard_channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(live_leaderboard_message_id)
            except discord.NotFound:
                continue
            embed = build_leaderboard_embed(g)
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

# ---------------------------------------------
# EVENT: on_ready
# ---------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Zalogowano jako {bot.user}")
    load_data()

    # Przy starcie bota – usuwamy suffixy z ewentualnych nicków z NBSP
    for g in bot.guilds:
        for member in g.members:
            if member.bot:
                continue
            if member.nick and NBSP in member.nick:
                new_nick = remove_bot_suffix(member.nick)
                if len(new_nick) > 32:
                    new_nick = new_nick[:31] + "…"
                try:
                    await member.edit(nick=new_nick)
                except (discord.Forbidden, discord.HTTPException):
                    pass

    await bot.change_presence(activity=discord.Game(name=f"Prefix: {BOT_PREFIX}"))
    clean_statuses.start()
    update_live_leaderboard.start()

    try:
        await bot.tree.sync()
        logging.info("Zarejestrowano slash commands.")
    except Exception as e:
        logging.warning(f"Nie udało się zsynchronizować slash commands: {e}")

# ---------------------------------------------
# ZADANIE: clean_statuses
# ---------------------------------------------
@tasks.loop(minutes=1)
async def clean_statuses():
    now = datetime.datetime.now(timezone.utc)

    for user_id, data in user_statuses.items():
        # Zerujemy substancje, jeśli minął ich czas
        for t in VALID_TYPES:
            count = data[t]
            exp_time = data["expires_per_substance"].get(t)
            if count > 0 and exp_time and now > exp_time:
                data[t] = 0
                data["expires_per_substance"][t] = None

                # Jeśli user to właściciel
                for guild_ in bot.guilds:
                    m = guild_.get_member(user_id)
                    if m and m.guild.owner_id == m.id:
                        await update_nickname(m, source="expire")
                        break

        # Jeśli user ma 0 we wszystkich – NIE usuwamy go, ale aktualizujemy original_nick
        if all(data[sub] == 0 for sub in VALID_TYPES):
            found_member = None
            for guild_ in bot.guilds:
                m = guild_.get_member(user_id)
                if m:
                    found_member = m
                    break

            if found_member:
                current_nick = found_member.nick or found_member.name
                # Usuwamy ewentualne NBSP i emotki
                pure_nick = remove_bot_suffix(current_nick)
                data["original_nick"] = pure_nick

    save_data()

# ---------------------------------------------
# EVENT: on_message – filtr kanału
# ---------------------------------------------
@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return
    if listening_channel_id is not None and message.channel.id != listening_channel_id:
        return
    await bot.process_commands(message)

# ---------------------------------------------
# KOMENDA .HELP oraz /HELP
# ---------------------------------------------
def get_help_text() -> str:
    return (
        f"**Komendy (prefix: {BOT_PREFIX})**:\n"
        f"{BOT_PREFIX}help – Wyświetla tę pomoc\n"
        f"{BOT_PREFIX}add <typ> <ilość> – Dodaje do Twojego statusu\n"
        f"{BOT_PREFIX}add <nick> <typ> <ilość> – Dodaje do cudzego statusu (Manage Nicknames / Admin)\n"
        f"{BOT_PREFIX}status – Wyświetla Twój status\n"
        f"{BOT_PREFIX}clear [<nick>] – Czyści Twój status lub czyjś (Manage Nicknames / Admin)\n"
        f"{BOT_PREFIX}leaderboard [hide] – Wyświetla tabelę wyników; z 'hide' wyśle w DM\n"
        f"{BOT_PREFIX}init_status_message – Tworzy wiadomość z reakcjami\n"
        f"{BOT_PREFIX}setchannel <kanał> – Ustawia kanał nasłuchu (admin)\n"
        f"{BOT_PREFIX}live_leaderboard – Tworzy i aktualizuje co minutę embed z wynikami (admin)\n\n"
        "**Slash commands**:\n"
        "/help – ta sama pomoc\n"
        "/add, /status, /clear, /leaderboard, /init_status_message, /setchannel, /live_leaderboard, /ping\n"
        "(Działają analogicznie do komend prefiksowych.)"
    )

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.send(get_help_text())

@bot.tree.command(name="help", description="Wyświetla opis wszystkich komend bota")
async def help_slash_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(get_help_text())

# ---------------------------------------------
# .ADD (prefiks) i /add (slash)
# ---------------------------------------------
@bot.command()
async def add(ctx: commands.Context, *args):
    """
    .add <typ> <ilość>
    .add <nick> <typ> <ilość>
    """
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze.")
        return

    if len(args) == 2:
        typ, ilosc_str = args
        member = ctx.guild.get_member(ctx.author.id)
    elif len(args) == 3:
        name_or_mention, typ, ilosc_str = args
        if not can_add_for_others(ctx.author):
            await ctx.send("Nie masz uprawnień do dodawania innym.")
            return
        member = find_user_in_guild(ctx.guild, name_or_mention)
        if not member:
            await ctx.send(f"Nie znaleziono użytkownika: {name_or_mention}")
            return
    else:
        await ctx.send("Poprawne użycie: .add <typ> <ilosć> lub .add <nick> <typ> <ilosć>")
        return

    # Normalizacja typu
    typ = typ.strip().lower()
    try:
        ilosc = int(ilosc_str)
    except ValueError:
        await ctx.send("Podaj liczbę jako ilość.")
        return

    if typ not in VALID_TYPES:
        await ctx.send(f"Nieznany typ! Dostępne: {', '.join(VALID_TYPES)}.")
        return

    if member.id not in user_statuses:
        user_statuses[member.id] = create_new_status(member.nick or member.name)

    data = user_statuses[member.id]
    data[typ] += ilosc

    # Ustawiamy czas wygaśnięcia
    hours_to_expire = TIME_TO_EXPIRE[typ]
    data["expires_per_substance"][typ] = datetime.datetime.now(timezone.utc) + timedelta(hours=hours_to_expire)

    # Miesięczna statystyka
    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += ilosc

    if member.id == ctx.author.id:
        await ctx.send(f"Dodano **{ilosc}** do **{typ}** dla {ctx.author.mention}.")
    else:
        await ctx.send(f"Dodano **{ilosc}** do **{typ}** dla {member.mention}.")

    await update_nickname(member, source="command")
    save_data()

@bot.tree.command(name="add", description="Dodaje używkę do statusu (Twojego lub czyjegoś).")
@app_commands.describe(
    user="Opcjonalnie inny użytkownik",
    typ="Typ używki (piwo, wódka, itp.)",
    ilosc="Ile sztuk"
)
async def add_slash(interaction: discord.Interaction, typ: str, ilosc: int, user: discord.Member = None):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze.", ephemeral=True)
        return

    member = user or interaction.guild.get_member(interaction.user.id)

    # Normalizacja
    typ = typ.strip().lower()
    if user and not can_add_for_others(interaction.user):
        await interaction.response.send_message("Nie masz uprawnień do dodawania innym.", ephemeral=True)
        return

    if typ not in VALID_TYPES:
        await interaction.response.send_message(f"Nieznany typ! Dostępne: {', '.join(VALID_TYPES)}.", ephemeral=True)
        return

    # Tworzymy status
    if member.id not in user_statuses:
        user_statuses[member.id] = create_new_status(member.nick or member.name)

    data = user_statuses[member.id]
    data[typ] += ilosc

    hours_to_expire = TIME_TO_EXPIRE[typ]
    data["expires_per_substance"][typ] = datetime.datetime.now(timezone.utc) + timedelta(hours=hours_to_expire)

    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += ilosc

    if member.id == interaction.user.id:
        msg = f"Dodano **{ilosc}** do **{typ}**."
    else:
        msg = f"Dodano **{ilosc}** do **{typ}** dla {member.mention}."

    await update_nickname(member, source="command")
    save_data()

    await interaction.response.send_message(msg, ephemeral=False)

# ---------------------------------------------
# .STATUS, /status
# ---------------------------------------------
@bot.command()
async def status(ctx: commands.Context):
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze.")
        return

    data = user_statuses.get(ctx.author.id)
    if not data:
        await ctx.send("Nie masz obecnie żadnego statusu.")
        return

    lines = []
    now = datetime.datetime.now(timezone.utc)
    for t in VALID_TYPES:
        count = data[t]
        if count > 0:
            exp_time = data["expires_per_substance"].get(t)
            if exp_time:
                diff_hours = (exp_time - now).total_seconds() / 3600.0
                hours_left = math.ceil(diff_hours)
                lines.append(f"• {t.capitalize()}: {count} (pozostało ~{hours_left}h)")
            else:
                lines.append(f"• {t.capitalize()}: {count} (czas nieustalony)")

    if not lines:
        await ctx.send("Nie masz żadnej aktywnej substancji.")
        return

    msg = "**Twój status**:\n" + "\n".join(lines)
    await ctx.send(msg)

@bot.tree.command(name="status", description="Pokazuje Twój status.")
async def status_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Tylko na serwerze.", ephemeral=True)
        return

    data = user_statuses.get(interaction.user.id)
    if not data:
        await interaction.response.send_message("Nie masz żadnego statusu.", ephemeral=True)
        return

    lines = []
    now = datetime.datetime.now(timezone.utc)
    for t in VALID_TYPES:
        count = data[t]
        if count > 0:
            exp_time = data["expires_per_substance"].get(t)
            if exp_time:
                diff_hours = (exp_time - now).total_seconds() / 3600.0
                hours_left = math.ceil(diff_hours)
                lines.append(f"• {t.capitalize()}: {count} (pozostało ~{hours_left}h)")
            else:
                lines.append(f"• {t.capitalize()}: {count} (czas nieustalony)")

    if not lines:
        await interaction.response.send_message("Nie masz żadnej aktywnej substancji.", ephemeral=True)
        return

    msg = "**Twój status**:\n" + "\n".join(lines)
    await interaction.response.send_message(msg, ephemeral=True)

# ---------------------------------------------
# .CLEAR, /clear
# ---------------------------------------------
@bot.command()
async def clear(ctx: commands.Context, user_arg: str = None):
    if not ctx.guild:
        await ctx.send("Tylko na serwerze.")
        return

    if user_arg is None:
        # Czyścimy swój
        if ctx.author.id not in user_statuses:
            await ctx.send("Nie masz statusu do wyczyszczenia.")
            return
        data = user_statuses.pop(ctx.author.id)
        member = ctx.guild.get_member(ctx.author.id)
        if member:
            # Przywracamy oryginal_nick
            original_nick = remove_bot_suffix(data.get("original_nick") or (member.nick or member.name))
            if len(original_nick) > 32:
                original_nick = original_nick[:31] + "…"
            try:
                await member.edit(nick=original_nick)
            except:
                pass
        await ctx.send("Twój status został wyczyszczony.")
        save_data()
    else:
        # Czyścimy innego
        if not can_clear_others(ctx.author):
            await ctx.send("Nie masz uprawnień (Manage Nicknames / Admin).")
            return
        target = find_user_in_guild(ctx.guild, user_arg)
        if not target:
            await ctx.send(f"Nie znaleziono użytkownika: {user_arg}")
            return
        if target.id not in user_statuses:
            await ctx.send(f"Użytkownik {target.mention} nie ma statusu.")
            return

        data = user_statuses.pop(target.id)
        original_nick = remove_bot_suffix(data.get("original_nick") or (target.nick or target.name))
        if len(original_nick) > 32:
            original_nick = original_nick[:31] + "…"
        try:
            await target.edit(nick=original_nick)
        except:
            pass
        await ctx.send(f"Status użytkownika {target.mention} wyczyszczony.")
        save_data()

@bot.tree.command(name="clear", description="Czyści Twój status lub czyjś (Manage Nicknames / Admin).")
@app_commands.describe(
    user="Osoba do wyczyszczenia (opcjonalnie)"
)
async def clear_slash(interaction: discord.Interaction, user: discord.Member = None):
    if not interaction.guild:
        await interaction.response.send_message("Tylko na serwerze.", ephemeral=True)
        return

    if user is None:
        # Czyścimy swój
        if interaction.user.id not in user_statuses:
            await interaction.response.send_message("Nie masz statusu do wyczyszczenia.", ephemeral=True)
            return
        data = user_statuses.pop(interaction.user.id)
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            original_nick = remove_bot_suffix(data.get("original_nick") or (member.nick or member.name))
            if len(original_nick) > 32:
                original_nick = original_nick[:31] + "…"
            try:
                await member.edit(nick=original_nick)
            except:
                pass
        await interaction.response.send_message("Twój status został wyczyszczony.", ephemeral=False)
        save_data()
    else:
        # Czyścimy kogoś innego
        if not can_clear_others(interaction.user):
            await interaction.response.send_message("Nie masz uprawnień do czyszczenia cudzych statusów.", ephemeral=True)
            return
        if user.id not in user_statuses:
            await interaction.response.send_message(f"Użytkownik {user.mention} nie ma statusu.", ephemeral=True)
            return

        data = user_statuses.pop(user.id)
        original_nick = remove_bot_suffix(data.get("original_nick") or (user.nick or user.name))
        if len(original_nick) > 32:
            original_nick = original_nick[:31] + "…"
        try:
            await user.edit(nick=original_nick)
        except:
            pass
        await interaction.response.send_message(f"Status {user.mention} wyczyszczony.", ephemeral=False)
        save_data()

# ---------------------------------------------
# .LEADERBOARD, /leaderboard
# ---------------------------------------------
@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx: commands.Context, hide_arg: str = None):
    if not ctx.guild:
        await ctx.send("Tylko na serwerze.")
        return
    text = build_leaderboard_text(ctx.guild)
    if hide_arg == "hide":
        try:
            await ctx.author.send(text)
            await ctx.send("Sprawdź DM.")
        except discord.Forbidden:
            await ctx.send("Nie mogę wysłać DM.")
    else:
        await ctx.send(text)

@bot.tree.command(name="leaderboard", description="Tabela wyników.")
@app_commands.describe(
    hide="Czy ma być ephemeral (ukryte)? Domyślne: False."
)
async def leaderboard_slash(interaction: discord.Interaction, hide: bool = False):
    if not interaction.guild:
        await interaction.response.send_message("Tylko na serwerze.", ephemeral=True)
        return
    text = build_leaderboard_text(interaction.guild)
    await interaction.response.send_message(content=text, ephemeral=hide)

# ---------------------------------------------
# .INIT_STATUS_MESSAGE, /init_status_message
# ---------------------------------------------
@bot.command()
async def init_status_message(ctx: commands.Context):
    global status_message_id
    text = (
        "**Kliknij w reakcję, aby dodać spożycie**:\n"
        "🍺 — Piwo (3h)\n"
        "🥃 — Whiskey (2h)\n"
        "🍸 — Wódka (2h)\n"
        "🍷 — Wino (2h)\n"
        "🍹 — Drink (2h)\n"
        "🍃 — Blunt (4h)\n"
        "❌ — Wyczyść status"
    )
    msg = await ctx.send(text)
    status_message_id = msg.id

    for emoji in EMOJI_TO_TYPE:
        await msg.add_reaction(emoji)
    await msg.add_reaction("❌")
    save_data()

@bot.tree.command(name="init_status_message", description="Tworzy wiadomość z reakcjami do dodawania spożycia.")
async def init_status_message_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Tylko na serwerze.", ephemeral=True)
        return

    global status_message_id
    text = (
        "**Kliknij w reakcję, aby dodać spożycie**:\n"
        "🍺 — Piwo (3h)\n"
        "🥃 — Whiskey (2h)\n"
        "🍸 — Wódka (2h)\n"
        "🍷 — Wino (2h)\n"
        "🍹 — Drink (2h)\n"
        "🍃 — Blunt (4h)\n"
        "❌ — Wyczyść status"
    )
    msg = await interaction.channel.send(text)
    status_message_id = msg.id

    for emoji in EMOJI_TO_TYPE:
        await msg.add_reaction(emoji)
    await msg.add_reaction("❌")

    save_data()
    await interaction.response.send_message("Wiadomość z reakcjami utworzona!", ephemeral=False)

# ---------------------------------------------
# .SETCHANNEL, /setchannel
# ---------------------------------------------
@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx: commands.Context, channel: discord.TextChannel):
    global listening_channel_id
    listening_channel_id = channel.id
    save_data()
    await ctx.send(f"Ustawiono kanał nasłuchu na {channel.mention}.")

@bot.tree.command(name="setchannel", description="Ustawia kanał nasłuchu (admin).")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Kanał do nasłuchu")
async def setchannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    global listening_channel_id
    listening_channel_id = channel.id
    save_data()
    await interaction.response.send_message(f"Ustawiono kanał nasłuchu na {channel.mention}.")

# ---------------------------------------------
# .LIVE_LEADERBOARD, /live_leaderboard
# ---------------------------------------------
@bot.command(name="live_leaderboard")
@commands.has_permissions(administrator=True)
async def live_leaderboard_cmd(ctx: commands.Context):
    global live_leaderboard_message_id, live_leaderboard_channel_id
    embed = build_leaderboard_embed(ctx.guild)
    msg = await ctx.send(embed=embed)
    live_leaderboard_message_id = msg.id
    live_leaderboard_channel_id = msg.channel.id
    save_data()
    await ctx.send("Utworzono 'żywy' leaderboard (odświeżanie co minutę)!")

@bot.tree.command(name="live_leaderboard", description="Tworzy embed z wynikami, aktualizowany co minutę (admin).")
@app_commands.checks.has_permissions(administrator=True)
async def live_leaderboard_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Tylko na serwerze.", ephemeral=True)
        return
    global live_leaderboard_message_id, live_leaderboard_channel_id
    embed = build_leaderboard_embed(interaction.guild)
    msg = await interaction.channel.send(embed=embed)
    live_leaderboard_message_id = msg.id
    live_leaderboard_channel_id = msg.channel.id
    save_data()
    await interaction.response.send_message("Utworzono 'żywy' leaderboard. Aktualizacja co minutę!", ephemeral=False)

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
            original_nick = remove_bot_suffix(data.get("original_nick") or (member.nick or member.name))
            if len(original_nick) > 32:
                original_nick = original_nick[:31] + "…"
            try:
                await member.edit(nick=original_nick)
            except (discord.Forbidden, discord.HTTPException):
                pass
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
    hours_to_expire = TIME_TO_EXPIRE[typ]
    data["expires_per_substance"][typ] = datetime.datetime.now(timezone.utc) + timedelta(hours=hours_to_expire)

    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += 1

    await update_nickname(member)
    save_data()
    await reaction.remove(user)

# ---------------------------------------------
# /PING
# ---------------------------------------------
@bot.tree.command(name="ping", description="Test – odpowiada 'Pong!'")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

# ---------------------------------------------
# START BOTA
# ---------------------------------------------
if __name__ == "__main__":
    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if not TOKEN:
        logging.warning("Nie znaleziono DISCORD_TOKEN w .env, użyję fallbacku.")
        TOKEN = "TWOJ-TOKEN-TUTAJ"
    bot.run(TOKEN)
