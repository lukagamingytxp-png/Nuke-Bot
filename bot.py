# ── Imports ───────────────────────────────────────────────────────────────────
import discord
from discord.ext import commands
from flask import Flask
from threading import Thread
import asyncpg
import asyncio
import os
import re

# ── Config ────────────────────────────────────────────────────────────────────
PREFIX       = ","
TOKEN        = os.environ.get("DISCORD_TOKEN")   # Set in Render → Environment
DATABASE_URL = os.environ.get("DATABASE_URL")    # Auto-set by Render PostgreSQL

# ── Bot Setup ─────────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix=PREFIX, intents=intents, help_command=None)

# ── Flask Keep-Alive (UptimeRobot pings this) ─────────────────────────────────
flask_app = Flask(__name__)

@flask_app.route("/")
def home():
    return "TrialBoom is alive.", 200

@flask_app.route("/health")
def health():
    return {"status": "ok"}, 200

def run_flask():
    flask_app.run(host="0.0.0.0", port=8080)

def keep_alive():
    t = Thread(target=run_flask)
    t.daemon = True
    t.start()

# ── Database ──────────────────────────────────────────────────────────────────
db_pool = None

async def init_db():
    global db_pool
    if not DATABASE_URL:
        print("[DB] No DATABASE_URL set — skipping database.")
        return
    db_pool = await asyncpg.create_pool(DATABASE_URL)
    await db_pool.execute("""
        CREATE TABLE IF NOT EXISTS clone_logs (
            id          SERIAL PRIMARY KEY,
            guild_id    BIGINT NOT NULL,
            guild_name  TEXT,
            source_id   BIGINT NOT NULL,
            source_name TEXT,
            cloned_by   BIGINT NOT NULL,
            cloned_at   TIMESTAMPTZ DEFAULT NOW()
        )
    """)
    print("[DB] Connected and table ready.")

async def log_clone(guild_id, guild_name, source_id, source_name, cloned_by):
    if db_pool is None:
        return
    await db_pool.execute(
        """
        INSERT INTO clone_logs (guild_id, guild_name, source_id, source_name, cloned_by)
        VALUES ($1, $2, $3, $4, $5)
        """,
        guild_id, guild_name, source_id, source_name, cloned_by
    )

# ── Helpers ───────────────────────────────────────────────────────────────────
def make_embed(title, desc, color=0x2b2d31):
    return discord.Embed(title=title, description=desc, color=color)

# ── Events ────────────────────────────────────────────────────────────────────
@bot.event
async def on_ready():
    await init_db()
    print(f"[BOT] Logged in as {bot.user} ({bot.user.id})")
    await bot.change_presence(activity=discord.Activity(
        type=discord.ActivityType.watching,
        name=",trialboom"
    ))

# ── Commands ──────────────────────────────────────────────────────────────────
@bot.command(name="trialboom")
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def trialboom(ctx, source_guild_id: int):
    """
    ,trialboom <server id>
    Wipes current server channels/roles then clones them from the source server.
    Requires: Administrator | Bot must be in both servers.
    """

    source_guild: discord.Guild = bot.get_guild(source_guild_id)
    if source_guild is None:
        await ctx.send(embed=make_embed(
            "❌ Not Found",
            f"Bot is not in server `{source_guild_id}`.\nAdd the bot to that server first.",
            0xe74c3c
        ))
        return

    target_guild: discord.Guild = ctx.guild

    status_msg = await ctx.send(embed=make_embed(
        "⏳ Starting Clone...",
        f"Source: **{source_guild.name}**\nTarget: **{target_guild.name}**",
        0xf39c12
    ))

    # Step 1 — Delete all channels
    await status_msg.edit(embed=make_embed("⏳ Step 1/3", "Deleting existing channels...", 0xf39c12))
    for channel in list(target_guild.channels):
        try:
            await channel.delete(reason="trialboom — wipe")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    # Step 2 — Delete all non-default roles
    for role in list(target_guild.roles):
        if role.is_default() or role >= target_guild.me.top_role:
            continue
        try:
            await role.delete(reason="trialboom — wipe")
            await asyncio.sleep(0.5)
        except Exception:
            pass

    # Step 3 — Clone roles from source
    role_map: dict[int, discord.Role] = {}
    sorted_roles = sorted(
        [r for r in source_guild.roles if not r.is_default()],
        key=lambda r: r.position
    )
    for role in sorted_roles:
        try:
            new_role = await target_guild.create_role(
                name        = role.name,
                permissions = role.permissions,
                color       = role.color,
                hoist       = role.hoist,
                mentionable = role.mentionable,
                reason      = "trialboom — role clone"
            )
            role_map[role.id] = new_role
            await asyncio.sleep(0.5)
        except Exception:
            pass

    # Step 4 — Clone channels + categories
    category_map: dict[int, discord.CategoryChannel] = {}
    channels_sorted = sorted(source_guild.channels, key=lambda c: c.position)

    for channel in channels_sorted:
        overwrites = {}
        for target, overwrite in channel.overwrites.items():
            if isinstance(target, discord.Role):
                mapped = (
                    target_guild.default_role
                    if target.is_default()
                    else role_map.get(target.id)
                )
                if mapped:
                    overwrites[mapped] = overwrite

        try:
            if isinstance(channel, discord.CategoryChannel):
                new_cat = await target_guild.create_category(
                    name=channel.name, overwrites=overwrites, reason="trialboom"
                )
                category_map[channel.id] = new_cat
                await asyncio.sleep(0.5)

            elif isinstance(channel, discord.TextChannel):
                cat = category_map.get(channel.category_id)
                await target_guild.create_text_channel(
                    name=channel.name, topic=channel.topic,
                    slowmode_delay=channel.slowmode_delay, nsfw=channel.nsfw,
                    category=cat, overwrites=overwrites, reason="trialboom"
                )
                await asyncio.sleep(0.5)

            elif isinstance(channel, discord.VoiceChannel):
                cat = category_map.get(channel.category_id)
                await target_guild.create_voice_channel(
                    name=channel.name,
                    bitrate=min(channel.bitrate, target_guild.bitrate_limit),
                    user_limit=channel.user_limit,
                    category=cat, overwrites=overwrites, reason="trialboom"
                )
                await asyncio.sleep(0.5)

            elif isinstance(channel, discord.StageChannel):
                cat = category_map.get(channel.category_id)
                await target_guild.create_stage_channel(
                    name=channel.name, category=cat,
                    overwrites=overwrites, reason="trialboom"
                )
                await asyncio.sleep(0.5)

            elif isinstance(channel, discord.ForumChannel):
                cat = category_map.get(channel.category_id)
                await target_guild.create_forum(
                    name=channel.name, category=cat,
                    overwrites=overwrites, reason="trialboom"
                )
                await asyncio.sleep(0.5)

        except Exception:
            pass

    # Rename guild to match source
    try:
        await target_guild.edit(name=source_guild.name, reason="trialboom — sync")
    except Exception:
        pass

    # Log to database
    await log_clone(
        guild_id    = target_guild.id,
        guild_name  = target_guild.name,
        source_id   = source_guild.id,
        source_name = source_guild.name,
        cloned_by   = ctx.author.id
    )

    # Confirm in first available text channel
    confirm_ch = next(
        (c for c in target_guild.text_channels
         if c.permissions_for(target_guild.me).send_messages),
        None
    )
    if confirm_ch:
        await confirm_ch.send(embed=make_embed(
            "✅ Clone Complete",
            f"**{target_guild.name}** cloned from **{source_guild.name}**\n"
            f"Roles copied: `{len(role_map)}`\n"
            f"Source channels: `{len(source_guild.channels)}`",
            0x2ecc71
        ))

@trialboom.error
async def trialboom_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=make_embed("❌ No Permission", "You need **Administrator**.", 0xe74c3c))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=make_embed("❌ Usage", "`,trialboom <server id>`", 0xe74c3c))
    elif isinstance(error, commands.BadArgument):
        await ctx.send(embed=make_embed("❌ Bad ID", "Server ID must be a number.", 0xe74c3c))
    else:
        await ctx.send(embed=make_embed("❌ Error", str(error), 0xe74c3c))

@bot.command(name="trialspamall")
@commands.has_permissions(administrator=True)
@commands.guild_only()
async def trialspamall(ctx, *, args: str):
    """
    ,trialspamall (message) <number>
    Sends the message to every text channel N times.
    Example: ,trialspamall (you've been touched by trial) <20>
    """

    # Parse (message) and <number> from raw args
    msg_match = re.search(r'\((.+?)\)', args)
    num_match = re.search(r'<(\d+)>', args)

    if not msg_match or not num_match:
        await ctx.send(embed=make_embed(
            "❌ Wrong Format",
            "Usage: `,trialspamall (your message here) <number>`\n"
            "Example: `,trialspamall (you've been touched by trial) <20>`",
            0xe74c3c
        ))
        return

    message = msg_match.group(1)
    count   = int(num_match.group(1))

    # Cap at 100 per channel to avoid obliterating the server
    if count < 1:
        await ctx.send(embed=make_embed("❌ Invalid", "Number must be at least `1`.", 0xe74c3c))
        return
    if count > 100:
        await ctx.send(embed=make_embed("❌ Too Many", "Max is `100` per channel.", 0xe74c3c))
        return

    text_channels = [
        c for c in ctx.guild.text_channels
        if c.permissions_for(ctx.guild.me).send_messages
    ]

    if not text_channels:
        await ctx.send(embed=make_embed("❌ No Channels", "No text channels the bot can send in.", 0xe74c3c))
        return

    await ctx.send(embed=make_embed(
        "📨 Spamming...",
        f"Message: **{message}**\n"
        f"Times: `{count}` × `{len(text_channels)}` channels",
        0xf39c12
    ))

    for channel in text_channels:
        for _ in range(count):
            try:
                await channel.send(message)
                await asyncio.sleep(0.3)   # avoid rate limits
            except Exception:
                break   # skip channel if no perms / other error

@trialspamall.error
async def trialspamall_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send(embed=make_embed("❌ No Permission", "You need **Administrator**.", 0xe74c3c))
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(embed=make_embed(
            "❌ Usage",
            "`,trialspamall (your message) <number>`",
            0xe74c3c
        ))
    else:
        await ctx.send(embed=make_embed("❌ Error", str(error), 0xe74c3c))

@bot.command(name="help")
@commands.guild_only()
async def help_cmd(ctx):
    e = discord.Embed(color=0x2b2d31)
    e.description = (
        "## cmds\n\n"
        "`,trialboom <server id>`\n"
        "wipes this server and copies channels + roles from the server u give it\n"
        "bot needs to be in both servers, u need admin\n\n"
        "`,trialspamall (message) <number>`\n"
        "sends ur message to every channel n amount of times\n"
        "example: `,trialspamall (touched by trial) <20>` — max 100\n\n"
        "`,help`\n"
        "shows this\n"
    )
    await ctx.send(embed=e)

# ── Run ───────────────────────────────────────────────────────────────────────
keep_alive()
bot.run(TOKEN)
