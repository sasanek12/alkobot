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

bot = commands.Bot(command_prefix=BOT_PREFIX, intents=intents, help_command=None)

VALID_TYPES = {"piwo", "wodka", "whiskey", "inne", "blunt"}

# Czas (w godzinach) po jakim "schodzi" ostatnio dodana porcja danego typu:
TIME_TO_EXPIRE = {
    "piwo": 3,     # ~3h
    "wodka": 2,    # ~2h
    "whiskey": 2,  # ~2h
    "inne": 2,     # np. wino ~2h
    "blunt": 4     # ~4h
}

EMOJI_TO_TYPE = {
    "🍺": "piwo",
    "🥃": "whiskey",
    "🍸": "wodka",
    "🍷": "inne",
    "🍃": "blunt"
}
TYPE_TO_EMOJI = {v: k for k, v in EMOJI_TO_TYPE.items()}

# ---------------------------------------------
# DANE I ZMIENNE
# ---------------------------------------------
user_statuses = {}           # { user_id: {...} }
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

        # wczytujemy expires_per_substance
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
    to_save = {"settings": {"listening_channel_id": listening_channel_id}}
    for user_id, data in user_statuses.items():
        data_copy = dict(data)

        # musimy przekształcić expires_per_substance na string
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
        logging.info(f"Zapisano dane do pliku {DATA_FILE}.")
    except OSError as e:
        logging.error(f"Błąd zapisu do {DATA_FILE}: {e}")

# ---------------------------------------------
# FUNKCJE POMOCNICZE
# ---------------------------------------------
def create_new_status(original_nick: str) -> dict:
    """
    Tworzy nową strukturę statusu. Każda substancja = 0,
    a expires_per_substance[typ] = None.
    """
    return {
        "original_nick": original_nick,
        "piwo": 0,
        "wodka": 0,
        "whiskey": 0,
        "inne": 0,
        "blunt": 0,
        "monthly_usage": {},
        "expires_per_substance": {
            "piwo": None,
            "wodka": None,
            "whiskey": None,
            "inne": None,
            "blunt": None
        }
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
    Zwraca skróconą formę np. "🍺5🥃2", pomijając zera.
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
    Modyfikuje nick użytkownika. Jeśli to właściciel serwera, wysyła mu DM z gotową komendą do wklejenia.
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
    except discord.Forbidden:
        if member.guild.owner_id == member.id:
            try:
                if source == "command":
                    # Gotowa komenda dla właściciela do wklejenia na serwerze
                    await member.send(
                        f"🔔 **Propozycja zmiany nicku:** `{new_nick}`\n\n"
                        f"👉 Użyj na serwerze gotowej komendy:\n"
                        f"```/setnick \"{new_nick}\"```"
                    )
                elif source == "expire":
                    # Po wygaśnięciu — gotowa komenda do przywrócenia
                    await member.send(
                        f"⏳ Czas dla Twojego statusu minął. Sugerowana nowa nazwa: `{new_nick}`\n\n"
                        f"👉 Użyj na serwerze gotowej komendy:\n"
                        f"```/setnick \"{new_nick}\"```"
                    )
            except discord.Forbidden:
                logging.warning(f"Nie udało się wysłać DM do właściciela ({member.name}).")
        else:
            logging.warning(f"Nie udało się zmienić nicku dla {member.name}.")



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

def build_leaderboard_text(guild: discord.Guild) -> str:
    current_month = get_current_month()
    usage_list = []
    for user_id, data in user_statuses.items():
        monthly_data = data.get("monthly_usage", {})
        stats = monthly_data.get(current_month, {})
        total_used = sum(stats.values())
        if total_used > 0:
            usage_list.append((user_id, stats, total_used))

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
# EVENT on_ready + pętla czyszcząca
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
        for t in VALID_TYPES:
            count = data[t]
            exp_time = data["expires_per_substance"].get(t)
            if count > 0 and exp_time is not None:
                if now > exp_time:
                    data[t] = 0
                    data["expires_per_substance"][t] = None

                    member = bot.get_guild(data["guild_id"]).get_member(user_id)
                    if member:
                        if member.guild.owner_id == member.id:
                            # Właściciel serwera – wysyłamy DM z aktualnym nickiem
                            await update_nickname(member, source="expire")

        # Jeśli wszystkie substancje są wyzerowane, usuwamy status
        if all(data[sub] == 0 for sub in VALID_TYPES):
            to_remove.append(user_id)

    for uid in to_remove:
        user_statuses.pop(uid, None)

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
        f"{BOT_PREFIX}status - Wyświetla Twój status (z czasem do wygaśnięcia)\n"
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
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze (guild).")
        return

    if len(args) == 2:
        typ, ilosc_str = args
        member = ctx.guild.get_member(ctx.author.id)
    elif len(args) == 3:
        name_or_mention, typ, ilosc_str = args
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

    # Konwersja ilości
    try:
        ilosc = int(ilosc_str)
    except ValueError:
        await ctx.send("Podaj liczbę jako ilość.")
        return

    typ = typ.lower()
    if typ not in VALID_TYPES:
        await ctx.send(f"Nieznany typ! Dozwolone: {', '.join(VALID_TYPES)}.")
        return

    if member.id not in user_statuses:
        user_statuses[member.id] = create_new_status(member.nick or member.name)

    data = user_statuses[member.id]
    data[typ] += ilosc

    hours_to_expire = TIME_TO_EXPIRE[typ]
    data["expires_per_substance"][typ] = datetime.datetime.now(timezone.utc) + timedelta(hours=hours_to_expire)

    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += ilosc

    if member.id == ctx.author.id:
        await ctx.send(f"Dodano **{ilosc}** do **{typ}** dla {ctx.author.mention}.")
    else:
        await ctx.send(f"Dodano **{ilosc}** do **{typ}** dla {member.mention}.")

    # Używamy poprawnej wartości "source"
    await update_nickname(member, source="command")
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
    """
    Wyświetla aktualne ilości i czas do wygaśnięcia każdej substancji > 0
    """
    data = user_statuses.get(ctx.author.id)
    if not data:
        await ctx.send("Nie masz obecnie żadnego statusu.")
        return

    lines = []
    now = datetime.datetime.now(timezone.utc)
    for t in VALID_TYPES:
        count = data[t]
        if count > 0:
            # Obliczamy czas do wygaśnięcia:
            exp_time = data["expires_per_substance"].get(t)
            if exp_time:
                diff = exp_time - now
                diff_hours = diff.total_seconds() / 3600.0
                # zaokrąglamy w górę do pełnych godzin:
                hours_left = math.ceil(diff_hours)
                lines.append(f"• {t.capitalize()}: {count} (pozostało ~{hours_left}h)")
            else:
                # Teoretycznie nie powinno się zdarzyć,
                # ale gdyby count>0, a brak exp_time => brak czasu
                lines.append(f"• {t.capitalize()}: {count} (czas nieustalony)")

    if not lines:
        await ctx.send("Nie masz obecnie żadnej aktywnej substancji.")
        return

    msg = "**Twój status**:\n" + "\n".join(lines)
    await ctx.send(msg)

# ---------------------------------------------
# KOMENDA .CLEAR
# ---------------------------------------------
@bot.command()
async def clear(ctx: commands.Context, user_arg: str = None):
    """
    .clear => czyści własny status
    .clear <nick> => czyści cudzy status (Manage Nicknames / Admin)
    """
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze (guild).")
        return

    if user_arg is None:
        # Czyścimy swój status
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
    else:
        # Czyścimy status innej osoby
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
# KOMENDA .LEADERBOARD
# ---------------------------------------------
@bot.command(name="leaderboard")
async def leaderboard_cmd(ctx: commands.Context, hide_arg: str = None):
    """
    .leaderboard [hide]
      - bez 'hide' -> wyniki publiczne
      - 'hide' -> wyniki w DM
    """
    if not ctx.guild:
        await ctx.send("Ta komenda działa tylko na serwerze (guild).")
        return

    leaderboard_text = build_leaderboard_text(ctx.guild)

    if hide_arg == "hide":
        # Wysyłamy prywatnie
        try:
            await ctx.author.send(leaderboard_text)
            await ctx.send(f"Sprawdź prywatną wiadomość, {ctx.author.mention}!")
        except discord.Forbidden:
            await ctx.send(f"Nie mogę wysłać prywatnej wiadomości do {ctx.author.mention}.")
    else:
        await ctx.send(leaderboard_text)

# ---------------------------------------------
# SLASH: /LEADERBOARD
# ---------------------------------------------
@bot.tree.command(name="leaderboard", description="Wyświetla tabelę wyników")
@app_commands.describe(
    hide="Czy wiadomość ma być wysłana dyskretnie (ephemeral)? Domyślnie: False."
)
async def leaderboard_slash(interaction: discord.Interaction, hide: bool = False):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze (guild).", ephemeral=True)
        return

    text = build_leaderboard_text(interaction.guild)
    # ephemeral=hide => jeśli hide=True, wiadomość tylko dla wywołującego
    await interaction.response.send_message(content=text, ephemeral=hide)

# ---------------------------------------------
# KOMENDA .INIT_STATUS_MESSAGE
# ---------------------------------------------
@bot.command()
async def init_status_message(ctx: commands.Context):
    global status_message_id

    text = (
        "**Kliknij w reakcję, aby dodać spożycie**:\n"
        "🍺 — Piwo (3h)\n"
        "🥃 — Whiskey (2h)\n"
        "🍸 — Wódka (2h)\n"
        "🍷 — Inne (2h)\n"
        "🍃 — Blunt (4h)\n"
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
        # Wyczyść status
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

    # Ustawiamy/odświeżamy czas wygaśnięcia
    hours_to_expire = TIME_TO_EXPIRE[typ]
    data["expires_per_substance"][typ] = datetime.datetime.now(timezone.utc) + timedelta(hours=hours_to_expire)

    month = get_current_month()
    ensure_monthly_record(data, month)
    data["monthly_usage"][month][typ] += 1

    await reaction.message.channel.send(f"{user.mention} dodał +1 do **{typ}**.")
    await update_nickname(member)
    save_data()

    await reaction.remove(user)

# ---------------------------------------------
# /PING
# ---------------------------------------------
@bot.tree.command(name="ping", description="Testowa komenda slash – odpowiada 'Pong!'")
async def ping_slash(interaction: discord.Interaction):
    await interaction.response.send_message("Pong!")

# ---------------------------------------------
# START BOTA
# ---------------------------------------------
if __name__ == "__main__":
    load_dotenv()
    TOKEN = os.getenv("DISCORD_TOKEN")
    if TOKEN is None:
        logging.warning(f"Nie znaleziono DISCORD_TOKEN w .env. Wykorzystam TOKEN z kodu (NIEZALECANE)")
        TOKEN = "TWOJ-TOKEN-TUTAJ-NIEZALECANE"
    bot.run(TOKEN)
