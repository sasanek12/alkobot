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
STATS_FOLDER = "stats"  # Folder do eksportu statystyk

intents = discord.Intents.default()
intents.message_content = True
intents.reactions = True
intents.members = True  # Upewnij się, że w panelu dewelopera Discord są włączone

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

VALID_TYPES = {"piwo", "wodka", "whiskey", "wino", "drink", "blunt"}

TIME_TO_EXPIRE = {
    "piwo": 3,
    "wodka": 2,
    "whiskey": 2,
    "wino": 2,
    "drink": 2,
    "blunt": 4
}

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
# GLOBALNE ZMIENNE
# ---------------------------------------------
user_statuses = {}           # {user_id: {...}}
status_message_id = None     # ID wiadomości z reakcjami (init_status_message)
listening_channel_id = None  # Kanał, w którym bot nasłuchuje (opcjonalnie)
live_leaderboard_message_id = None  # ID wiadomości z live_leaderboard
live_leaderboard_channel_id = None  # Kanał dla live_leaderboard
dedicated_channel_id = None  # Dedykowany kanał dla wiadomości z reakcjami i leaderboardu

# ---------------------------------------------
# MIGRACJA DANYCH (plik JSON)
# ---------------------------------------------
def migrate_raw_data(raw: dict) -> dict:
    # Upewnij się, że w sekcji settings są wymagane klucze
    settings = raw.get("settings", {})
    if "listening_channel_id" not in settings:
        settings["listening_channel_id"] = None
    if "dedicated_channel_id" not in settings:
        settings["dedicated_channel_id"] = None
    if "live_leaderboard_message_id" not in settings:
        settings["live_leaderboard_message_id"] = None
    if "live_leaderboard_channel_id" not in settings:
        settings["live_leaderboard_channel_id"] = None
    raw["settings"] = settings
    # Dla każdego wpisu użytkownika uzupełniamy brakujące klucze
    for key, data in raw.items():
        if key == "settings":
            continue
        if "original_nick" not in data:
            data["original_nick"] = ""
        for typ in VALID_TYPES:
            if typ not in data:
                data[typ] = 0
        if "monthly_usage" not in data or not isinstance(data["monthly_usage"], dict):
            data["monthly_usage"] = {}
        if "expires_per_substance" not in data or not isinstance(data["expires_per_substance"], dict):
            data["expires_per_substance"] = {}
        for typ in VALID_TYPES:
            if typ not in data["expires_per_substance"]:
                data["expires_per_substance"][typ] = None
    return raw

# ---------------------------------------------
# ŁADOWANIE / ZAPIS DANYCH
# ---------------------------------------------
def ensure_data_file_exists():
    if not os.path.exists(DATA_FILE):
        logging.info(f"Plik {DATA_FILE} nie istnieje. Tworzę go.")
        base_data = {
            "settings": {
                "listening_channel_id": None,
                "dedicated_channel_id": None,
                "live_leaderboard_message_id": None,
                "live_leaderboard_channel_id": None
            }
        }
        with open(DATA_FILE, "w", encoding="utf-8") as f:
            json.dump(base_data, f, ensure_ascii=False, indent=2)

def load_data():
    global user_statuses, listening_channel_id, dedicated_channel_id
    global live_leaderboard_message_id, live_leaderboard_channel_id
    ensure_data_file_exists()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"Błąd wczytywania {DATA_FILE}: {e}")
        raw = {"settings": {"listening_channel_id": None}}
    raw = migrate_raw_data(raw)
    settings = raw.get("settings", {})
    listening_channel_id = settings.get("listening_channel_id", None)
    dedicated_channel_id = settings.get("dedicated_channel_id", None)
    live_leaderboard_message_id = settings.get("live_leaderboard_message_id")
    live_leaderboard_channel_id = settings.get("live_leaderboard_channel_id")
    temp_statuses = {}
    for user_id_str, data in raw.items():
        if user_id_str == "settings":
            continue
        try:
            user_id = int(user_id_str)
        except ValueError:
            continue
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
    logging.info("Dane zostały wczytane z pliku.")

def save_data():
    to_save = {"settings": {}}
    to_save["settings"]["listening_channel_id"] = listening_channel_id
    to_save["settings"]["dedicated_channel_id"] = dedicated_channel_id
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
        logging.info(f"Dane zapisano do {DATA_FILE}.")
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
        "expires_per_substance": {typ: None for typ in VALID_TYPES}
    }

def remove_bot_suffix(nick: str) -> str:
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
        data["monthly_usage"][month] = {typ: 0 for typ in VALID_TYPES}

def build_usage_string(status_data: dict) -> str:
    parts = []
    for typ in VALID_TYPES:
        count = status_data.get(typ, 0)
        if count > 0:
            parts.append(f"{TYPE_TO_EMOJI[typ]}{count}")
    return "".join(parts)

async def update_nickname(member: discord.Member, source="command"):
    data = user_statuses.get(member.id)
    if not data:
        return
    pure_original = remove_bot_suffix(data.get("original_nick") or (member.nick or member.name))
    data["original_nick"] = pure_original
    usage_str = build_usage_string(data)
    new_nick = f"{pure_original}{NBSP}{usage_str}" if usage_str else pure_original
    if len(new_nick) > 32:
        new_nick = new_nick[:31] + "…"
    try:
        await member.edit(nick=new_nick)
    except discord.Forbidden:
        if member.guild.owner_id == member.id:
            try:
                if source == "command":
                    await member.send(
                        f"🔔 Propozycja zmiany nicku: `{new_nick}`\n"
                        f"👉 Użyj komendy: ```/nick \"{new_nick}\"```"
                    )
                elif source == "expire":
                    await member.send(
                        f"⏳ Twój status wygasł. Sugerowany nick: `{new_nick}`\n"
                        f"👉 Użyj komendy: ```/nick \"{new_nick}\"```"
                    )
            except discord.Forbidden:
                logging.warning(f"Nie udało się wysłać DM do właściciela ({member.name}).")
        else:
            logging.warning(f"Brak uprawnień do zmiany nicku {member.name}.")

def find_user_in_guild(guild: discord.Guild, name_or_mention: str) -> discord.Member:
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
    return member.guild_permissions.administrator or member.guild_permissions.manage_nicknames

def can_clear_others(member: discord.Member) -> bool:
    return member.guild_permissions.administrator or member.guild_permissions.manage_nicknames

def build_leaderboard_text(guild: discord.Guild) -> str:
    current_month = get_current_month()
    usage_list = []
    for user_id, data in user_statuses.items():
        if not guild.get_member(user_id):
            continue
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
        original_nick = remove_bot_suffix(data.get("original_nick") or "")
        if not original_nick:
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
    current_month = get_current_month()
    usage_list = []
    for user_id, data in user_statuses.items():
        if not guild.get_member(user_id):
            continue
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
        original_nick = remove_bot_suffix(data.get("original_nick") or "")
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
# TASK: update_live_leaderboard
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
# TASK: export_monthly_stats
# ---------------------------------------------
@tasks.loop(minutes=1)
async def export_monthly_stats():
    now = datetime.datetime.now(timezone.utc)
    current_month = now.strftime("%Y-%m")
    if now.day == 1:
        prev_month_date = now - timedelta(days=1)
        prev_month = prev_month_date.strftime("%Y-%m")
        if not os.path.exists(STATS_FOLDER):
            os.makedirs(STATS_FOLDER)
        lines = []
        for user_id, data in user_statuses.items():
            monthly = data.get("monthly_usage", {})
            stats = monthly.get(prev_month, {})
            total_used = sum(stats.values())
            if total_used > 0:
                original_nick = remove_bot_suffix(data.get("original_nick") or "")
                if not original_nick:
                    member = None
                    for g in bot.guilds:
                        member = g.get_member(user_id)
                        if member:
                            break
                    original_nick = member.display_name if member else f"<@{user_id}>"
                detail_parts = []
                for t in VALID_TYPES:
                    val = stats.get(t, 0)
                    if val > 0:
                        detail_parts.append(f"{TYPE_TO_EMOJI[t]}{val}")
                detail_str = "".join(detail_parts) or "Brak"
                lines.append(f"{original_nick} | {detail_str} | Suma: {total_used}")
        export_text = "\n".join(lines)
        file_path = os.path.join(STATS_FOLDER, f"{prev_month}.txt")
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(export_text)
        logging.info(f"Wyeksportowano statystyki za {prev_month} do {file_path}.")
        for data in user_statuses.values():
            monthly = data.get("monthly_usage", {})
            if prev_month in monthly:
                del monthly[prev_month]
        save_data()

# ---------------------------------------------
# EVENT: on_ready
# ---------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Zalogowano jako {bot.user}")
    load_data()
    # Jeśli dedykowany kanał ustawiony, aktywujemy wiadomości na nim
    if dedicated_channel_id:
        for g in bot.guilds:
            channel = g.get_channel(dedicated_channel_id)
            if channel:
                global status_message_id
                try:
                    await channel.fetch_message(status_message_id)
                except (discord.NotFound, TypeError):
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
                    msg = await channel.send(text)
                    status_message_id = msg.id
                    for emoji in EMOJI_TO_TYPE:
                        await msg.add_reaction(emoji)
                    await msg.add_reaction("❌")
                    save_data()
                global live_leaderboard_message_id, live_leaderboard_channel_id
                try:
                    await channel.fetch_message(live_leaderboard_message_id)
                except (discord.NotFound, TypeError):
                    embed = build_leaderboard_embed(g)
                    msg = await channel.send(embed=embed)
                    live_leaderboard_message_id = msg.id
                    live_leaderboard_channel_id = channel.id
                    save_data()
                break
    # Przy starcie – usuwamy NBSP i emotki z nicków
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
    export_monthly_stats.start()
    try:
        await bot.tree.sync()
        logging.info("Zarejestrowano slash commands.")
    except Exception as e:
        logging.warning(f"Nie udało się zsynchronizować slash commands: {e}")

# ---------------------------------------------
# TASK: clean_statuses
# ---------------------------------------------
@tasks.loop(minutes=1)
async def clean_statuses():
    now = datetime.datetime.now(timezone.utc)
    for user_id, data in user_statuses.items():
        for t in VALID_TYPES:
            count = data[t]
            exp_time = data["expires_per_substance"].get(t)
            if count > 0 and exp_time and now > exp_time:
                data[t] = 0
                data["expires_per_substance"][t] = None
                for guild_ in bot.guilds:
                    m = guild_.get_member(user_id)
                    if m and m.guild.owner_id == m.id:
                        await update_nickname(m, source="expire")
                        break
        if all(data[sub] == 0 for sub in VALID_TYPES):
            found_member = None
            for guild_ in bot.guilds:
                m = guild_.get_member(user_id)
                if m:
                    found_member = m
                    break
            if found_member:
                current_nick = found_member.nick or found_member.name
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
# KOMENDY: help, add, status, clear, leaderboard, init_status_message, setchannel,
#          live_leaderboard, setdedicatedchannel, shutdown, ping
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
        f"{BOT_PREFIX}live_leaderboard – Tworzy i aktualizuje co minutę embed z wynikami (admin)\n"
        f"{BOT_PREFIX}setdedicatedchannel <kanał> – Ustawia dedykowany kanał (admin)\n"
        f"{BOT_PREFIX}shutdown – Bezpieczne wyłączenie bota (admin)\n\n"
        "**Slash commands**:\n"
        "/help – ta sama pomoc\n"
        "/add, /status, /clear, /leaderboard, /init_status_message, /setchannel, /live_leaderboard, /setdedicatedchannel, /shutdown, /ping\n"
        "(Działają analogicznie do komend prefiksowych.)"
    )

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.send(get_help_text())

@bot.tree.command(name="help", description="Wyświetla opis wszystkich komend bota")
async def help_slash_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(get_help_text())

@bot.command()
@commands.has_permissions(administrator=True)
async def setdedicatedchannel(ctx: commands.Context, channel: discord.TextChannel):
    global dedicated_channel_id
    dedicated_channel_id = channel.id
    save_data()
    await ctx.send(f"Dedykowany kanał ustawiony na {channel.mention}.")

@bot.tree.command(name="setdedicatedchannel", description="Ustawia dedykowany kanał (admin).")
@app_commands.checks.has_permissions(administrator=True)
@app_commands.describe(channel="Dedykowany kanał dla wiadomości z reakcjami i leaderboardu")
async def setdedicatedchannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    global dedicated_channel_id
    dedicated_channel_id = channel.id
    save_data()
    await interaction.response.send_message(f"Dedykowany kanał ustawiony na {channel.mention}.", ephemeral=False)

@bot.command()
@commands.has_permissions(administrator=True)
async def shutdown(ctx: commands.Context):
    """
    Bezpieczne wyłączenie bota:
      - Przywraca oryginalne nicki (usuwa emotki).
      - Zapisuje dane.
      - Anuluje działające zadania.
      - Wyłącza bota.
    """
    await ctx.send("Bot jest w trakcie bezpiecznego wyłączania...")
    for g in bot.guilds:
        for member in g.members:
            if member.bot:
                continue
            try:
                original = remove_bot_suffix(member.nick) if member.nick else member.name
                if len(original) > 32:
                    original = original[:31] + "…"
                await member.edit(nick=original)
            except Exception as e:
                logging.warning(f"Nie udało się przywrócić nicku dla {member.name}: {e}")
    save_data()
    clean_statuses.cancel()
    update_live_leaderboard.cancel()
    export_monthly_stats.cancel()
    await ctx.send("Dane zapisane, zadania zatrzymane. Wyłączam bota.")
    await bot.close()

@bot.tree.command(name="shutdown", description="Bezpiecznie wyłącza bota (admin).")
@app_commands.checks.has_permissions(administrator=True)
async def shutdown_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze.", ephemeral=True)
        return
    for g in bot.guilds:
        for member in g.members:
            if member.bot:
                continue
            try:
                original = remove_bot_suffix(member.nick) if member.nick else member.name
                if len(original) > 32:
                    original = original[:31] + "…"
                await member.edit(nick=original)
            except Exception as e:
                logging.warning(f"Nie udało się przywrócić nicku dla {member.name}: {e}")
    save_data()
    clean_statuses.cancel()
    update_live_leaderboard.cancel()
    export_monthly_stats.cancel()
    await interaction.response.send_message("Dane zapisane, zadania zatrzymane. Wyłączam bota.", ephemeral=True)
    await bot.close()

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
