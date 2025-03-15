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

VALID_TYPES = {"piwo", "wodka", "whiskey", "wino", "drink", "blunt"}

# Czas (w godzinach) po jakim "schodzi" ostatnio dodana porcja danego typu:
TIME_TO_EXPIRE = {
    "piwo": 3,     # ~3h
    "wodka": 2,    # ~2h
    "whiskey": 2,  # ~2h
    "wino": 2,
    "drink": 2,
    "blunt": 4     # ~4h
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
# DANE I ZMIENNE
# ---------------------------------------------
user_statuses = {}           # { user_id: {...} }
status_message_id = None     # ID wiadomości z init_status_message
listening_channel_id = None  # None => bot słucha wszędzie

# Nowe zmienne do "żywego" leaderboardu  # <-- ZMIANA
live_leaderboard_message_id = None
live_leaderboard_channel_id = None

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
    global user_statuses, listening_channel_id, live_leaderboard_message_id, live_leaderboard_channel_id

    ensure_data_file_exists()
    try:
        with open(DATA_FILE, "r", encoding="utf-8") as f:
            raw = json.load(f)
    except (json.JSONDecodeError, OSError) as e:
        logging.error(f"Błąd wczytywania {DATA_FILE}: {e}")
        raw = {"settings": {"listening_channel_id": None}}

    settings = raw.get("settings", {})
    listening_channel_id = settings.get("listening_channel_id", None)

    # Nowe pola w "settings" do leaderboardu   # <-- ZMIANA
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
    logging.info(f"Wczytano dane z pliku {DATA_FILE}.")

def save_data():
    to_save = {"settings": {"listening_channel_id": listening_channel_id}}

    # Zapisujemy także info o "żywym" leaderboardzie  # <-- ZMIANA
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
    parts = []
    for typ in VALID_TYPES:
        count = status_data.get(typ, 0)
        if count > 0:
            emoji = TYPE_TO_EMOJI[typ]
            parts.append(f"{emoji}{count}")
    return "".join(parts)

async def update_nickname(member: discord.Member, source="command"):
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
                    await member.send(
                        f"🔔 **Propozycja zmiany nicku:** `{new_nick}`\n\n"
                        f"👉 Użyj na serwerze gotowej komendy:\n"
                        f"```/setnick \"{new_nick}\"```"
                    )
                elif source == "expire":
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
    if member.guild_permissions.administrator:
        return True
    return member.guild_permissions.manage_nicknames

def can_clear_others(member: discord.Member) -> bool:
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
# EMBED LEADERBOARD (do "żywej" wiadomości)
# ---------------------------------------------
def build_leaderboard_embed(guild: discord.Guild) -> discord.Embed:
    """
    Tworzy ładnego Embeda z bieżącym leaderboardem.
    """
    current_month = get_current_month()
    usage_list = []
    for user_id, data in user_statuses.items():
        monthly_data = data.get("monthly_usage", {})
        stats = monthly_data.get(current_month, {})
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

    position = 1
    for user_id, stats, total_used in usage_list:
        member = guild.get_member(user_id)
        if member:
            name_str = f"{position}) {member.display_name}"
        else:
            name_str = f"{position}) <@{user_id}>"

        detail_parts = []
        for t in VALID_TYPES:
            val = stats.get(t, 0)
            if val > 0:
                detail_parts.append(f"{TYPE_TO_EMOJI[t]}{val}")
        detail_str = "".join(detail_parts) or "Brak"

        embed.add_field(
            name=name_str,
            value=f"{detail_str} | Suma: {total_used}",
            inline=False
        )
        position += 1

    return embed

# ---------------------------------------------
# PĘTLA AKTUALIZUJĄCA "ŻYWY" LEADERBOARD  # <-- ZMIANA
# ---------------------------------------------
@tasks.loop(minutes=1)
async def update_live_leaderboard():
    """
    Co minutę aktualizuje wiadomość "live leaderboard" (jeśli istnieje).
    """
    global live_leaderboard_channel_id, live_leaderboard_message_id

    if not live_leaderboard_channel_id or not live_leaderboard_message_id:
        return  # Nie ustawiono "żywego" leaderboardu

    guild_list = bot.guilds
    for g in guild_list:
        channel = g.get_channel(live_leaderboard_channel_id)
        if channel:
            try:
                msg = await channel.fetch_message(live_leaderboard_message_id)
            except discord.NotFound:
                # Wiadomość skasowana lub niedostępna
                continue

            # Budujemy nowy embed
            embed = build_leaderboard_embed(g)
            try:
                await msg.edit(embed=embed)
            except discord.HTTPException:
                pass

# ---------------------------------------------
# EVENT on_ready + pętla czyszcząca
# ---------------------------------------------
@bot.event
async def on_ready():
    logging.info(f"Zalogowano jako {bot.user}")
    load_data()
    await bot.change_presence(activity=discord.Game(name=f"Prefix: {BOT_PREFIX}"))
    clean_statuses.start()
    update_live_leaderboard.start()  # <-- Startujemy nową pętlę

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

                    member = None
                    # Może user zapisał "guild_id" w data?
                    # Wcześniej tak robiliśmy w jednym z przykładów, ale tutaj nie ma guild_id.
                    # Zatem opuśćmy. Jeżeli chcesz, dodaj do data "guild_id".
                    # if "guild_id" in data:
                    #     g = bot.get_guild(data["guild_id"])
                    #     if g: member = g.get_member(user_id)

                    # Ewentualnie spróbujmy w pętli po bot.guilds i poszukać:
                    for guild_ in bot.guilds:
                        m = guild_.get_member(user_id)
                        if m:
                            member = m
                            break

                    if member:
                        if member.guild.owner_id == member.id:
                            await update_nickname(member, source="expire")

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
        f"{BOT_PREFIX}add <nick> <typ> <ilość> - Dodaje do cudzego statusu (Manage Nicknames / Admin)\n"
        f"{BOT_PREFIX}status - Wyświetla Twój status\n"
        f"{BOT_PREFIX}clear [<nick>] - Czyści Twój status lub czyjś (Manage Nicknames / Admin)\n"
        f"{BOT_PREFIX}leaderboard [hide] - Wyświetla tabelę wyników; z 'hide' wyśle w DM\n"
        f"{BOT_PREFIX}init_status_message - Tworzy wiadomość z reakcjami\n"
        f"{BOT_PREFIX}setchannel <kanał> - Ustawia kanał nasłuchu (admin)\n"
        f"{BOT_PREFIX}live_leaderboard - Tworzy i aktualizuje co minutę embed z wynikami (admin)\n\n"
        "**Slash commands**:\n"
        "/add, /status, /clear, /leaderboard, /init_status_message, /setchannel, /live_leaderboard, /help, /ping\n"
    )

@bot.command(name="help")
async def help_cmd(ctx: commands.Context):
    await ctx.send(get_help_text())

@bot.tree.command(name="help", description="Wyświetla opis najważniejszych komend bota")
async def help_slash_cmd(interaction: discord.Interaction):
    await interaction.response.send_message(get_help_text())

# ---------------------------------------------
# KOMENDY TEKSTOWE (PREFIX) - add, status, clear już mamy
# DODAJEMY KOMENDY SLASH do analogicznej obsługi
# ---------------------------------------------
@bot.tree.command(name="add", description="Dodaje używkę do statusu (Twojego lub innego).")
@app_commands.describe(
    user="Osoba, której chcesz dodać (opcjonalnie)",
    typ="Typ używki",
    ilosc="Ile sztuk dodać"
)
async def add_slash(
    interaction: discord.Interaction,
    typ: str,
    ilosc: int,
    user: discord.Member = None
):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze.", ephemeral=True)
        return

    member = user or interaction.guild.get_member(interaction.user.id)

    # Jeśli chcemy dodać komuś innemu, a nie mamy uprawnień
    if user and not can_add_for_others(interaction.user):
        await interaction.response.send_message(
            "Nie masz uprawnień (Manage Nicknames / Admin), by dodawać innym.",
            ephemeral=True
        )
        return

    typ = typ.lower()
    if typ not in VALID_TYPES:
        await interaction.response.send_message(
            f"Nieznany typ! Dozwolone: {', '.join(VALID_TYPES)}.",
            ephemeral=True
        )
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

    if member.id == interaction.user.id:
        msg = f"Dodano **{ilosc}** do **{typ}**."
    else:
        msg = f"Dodano **{ilosc}** do **{typ}** dla {member.mention}."

    await update_nickname(member, source="command")
    save_data()

    await interaction.response.send_message(msg, ephemeral=False)

@bot.tree.command(name="status", description="Wyświetla Twój status.")
async def status_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze.", ephemeral=True)
        return

    data = user_statuses.get(interaction.user.id)
    if not data:
        await interaction.response.send_message("Nie masz obecnie żadnego statusu.", ephemeral=True)
        return

    lines = []
    now = datetime.datetime.now(timezone.utc)
    for t in VALID_TYPES:
        count = data[t]
        if count > 0:
            exp_time = data["expires_per_substance"].get(t)
            if exp_time:
                diff = exp_time - now
                diff_hours = diff.total_seconds() / 3600.0
                hours_left = math.ceil(diff_hours)
                lines.append(f"• {t.capitalize()}: {count} (pozostało ~{hours_left}h)")
            else:
                lines.append(f"• {t.capitalize()}: {count} (czas nieustalony)")

    if not lines:
        await interaction.response.send_message("Nie masz obecnie żadnej aktywnej substancji.", ephemeral=True)
        return

    msg = "**Twój status**:\n" + "\n".join(lines)
    await interaction.response.send_message(msg, ephemeral=True)

@bot.tree.command(name="clear", description="Czyści Twój status lub czyjś (Admin / Manage Nicknames).")
@app_commands.describe(
    user="Osoba do wyczyszczenia statusu (opcjonalnie)"
)
async def clear_slash(
    interaction: discord.Interaction,
    user: discord.Member = None
):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze.", ephemeral=True)
        return

    if user is None:
        # Czyścimy swój
        if interaction.user.id not in user_statuses:
            await interaction.response.send_message("Nie masz obecnie żadnego statusu do wyczyszczenia.", ephemeral=True)
            return
        data = user_statuses.pop(interaction.user.id)
        member = interaction.guild.get_member(interaction.user.id)
        if member:
            original_nick = data.get("original_nick") or (member.nick or member.name)
            if len(original_nick) > 32:
                original_nick = original_nick[:31] + "…"
            try:
                await member.edit(nick=original_nick)
            except (discord.Forbidden, discord.HTTPException):
                pass
        await interaction.response.send_message("Twój status został wyczyszczony.", ephemeral=False)
        save_data()
    else:
        # Czyścimy kogoś innego
        if not can_clear_others(interaction.user):
            await interaction.response.send_message(
                "Nie masz uprawnień (Manage Nicknames / Admin), by czyścić statusy innych.",
                ephemeral=True
            )
            return

        if user.id not in user_statuses:
            await interaction.response.send_message(
                f"Użytkownik {user.mention} nie ma żadnego statusu.",
                ephemeral=True
            )
            return

        data = user_statuses.pop(user.id)
        original_nick = data.get("original_nick") or (user.nick or user.name)
        if len(original_nick) > 32:
            original_nick = original_nick[:31] + "…"
        try:
            await user.edit(nick=original_nick)
        except (discord.Forbidden, discord.HTTPException):
            pass

        await interaction.response.send_message(
            f"Status użytkownika {user.mention} został wyczyszczony.",
            ephemeral=False
        )
        save_data()

@bot.tree.command(name="init_status_message", description="Tworzy wiadomość z reakcjami do dodawania spożycia.")
async def init_status_message_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze.", ephemeral=True)
        return

    global status_message_id

    text = (
        "**Kliknij w reakcję, aby dodać spożycie**:\n"
        "🍺 — Piwo - 0,5l (3h)\n"
        "🥃 — Whiskey - 100ml (2h)\n"
        "🍸 — Wódka - 50ml (2h)\n"
        "🍷 — Wino - 250ml (2h)\n"
        "🍹 — Drink - 350ml (2h)\n"
        "🍃 — Blunt (4h)\n"
        "❌ — Wyczyść status"
    )
    msg = await interaction.channel.send(text)
    status_message_id = msg.id

    for emoji in EMOJI_TO_TYPE:
        await msg.add_reaction(emoji)
    await msg.add_reaction("❌")

    save_data()
    await interaction.response.send_message("Utworzono wiadomość z reakcjami.", ephemeral=False)

@bot.command()
async def init_status_message(ctx: commands.Context):
    global status_message_id
    text = (
        "**Kliknij w reakcję, aby dodać spożycie**:\n"
        "🍺 — Piwo - 0,5l (3h)\n"
        "🥃 — Whiskey - 100ml (2h)\n"
        "🍸 — Wódka - 50ml (2h)\n"
        "🍷 — Wino - 250ml (2h)\n"
        "🍹 — Drink - 350ml (2h)\n"
        "🍃 — Blunt (4h)\n"
        "❌ — Wyczyść status"
    )
    message = await ctx.send(text)
    status_message_id = message.id

    for emoji in EMOJI_TO_TYPE:
        await message.add_reaction(emoji)
    await message.add_reaction("❌")

    save_data()

@bot.tree.command(name="setchannel", description="Ustawia kanał nasłuchu (admin).")
@app_commands.describe(channel="Kanał do nasłuchu")
@app_commands.checks.has_permissions(administrator=True)
async def setchannel_slash(interaction: discord.Interaction, channel: discord.TextChannel):
    global listening_channel_id
    listening_channel_id = channel.id
    save_data()
    await interaction.response.send_message(f"Ustawiono kanał nasłuchu na {channel.mention}.")

@bot.command()
@commands.has_permissions(administrator=True)
async def setchannel(ctx: commands.Context, channel: discord.TextChannel):
    global listening_channel_id
    listening_channel_id = channel.id
    save_data()
    await ctx.send(f"Ustawiono kanał nasłuchu na {channel.mention}.")

# ---------------------------------------------
# KOMENDA .LIVE_LEADERBOARD + SLASH
# ---------------------------------------------
@bot.command(name="live_leaderboard")
@commands.has_permissions(administrator=True)
async def live_leaderboard_cmd(ctx: commands.Context):
    """
    Tworzy wiadomość z embedowanym leaderboardem, aktualizowanym co minutę.
    """
    global live_leaderboard_message_id, live_leaderboard_channel_id

    embed = build_leaderboard_embed(ctx.guild)
    msg = await ctx.send(embed=embed)

    live_leaderboard_message_id = msg.id
    live_leaderboard_channel_id = msg.channel.id
    save_data()

    await ctx.send("Utworzono 'żywy' leaderboard. Będzie aktualizowany co minutę!")

@bot.tree.command(name="live_leaderboard", description="Tworzy wiadomość z embedowanym leaderboardem (admin).")
@app_commands.checks.has_permissions(administrator=True)
async def live_leaderboard_slash(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message("Ta komenda działa tylko na serwerze (guild).", ephemeral=True)
        return

    global live_leaderboard_message_id, live_leaderboard_channel_id

    embed = build_leaderboard_embed(interaction.guild)
    msg = await interaction.channel.send(embed=embed)

    live_leaderboard_message_id = msg.id
    live_leaderboard_channel_id = msg.channel.id
    save_data()

    await interaction.response.send_message("Utworzono 'żywy' leaderboard. Będzie aktualizowany co minutę!", ephemeral=False)

# ---------------------------------------------
# KOMENDA .LEADERBOARD i /leaderboard (już jest)
# ---------------------------------------------

# ---------------------------------------------
# OBSŁUGA REAKCJI - USUNIĘTO WIADOMOŚCI NA KANALE  # <-- ZMIANA
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
