import discord
from discord.ext import commands
from flask import Flask, request
import requests
import threading
import asyncio
import urllib.parse
import os

# =========================
# ENV CONFIG (RAILWAY SAFE)
# =========================

TOKEN = os.getenv("DISCORD_TOKEN")
CLIENT_ID = os.getenv("CLIENT_ID")
CLIENT_SECRET = os.getenv("CLIENT_SECRET")
REDIRECT_URI = os.getenv("REDIRECT_URI")

GUILD_ID = int(os.getenv("VERIFY_GUILD_ID"))
VERIFY_ROLE_ID = int(os.getenv("VERIFY_ROLE_ID"))

NEW_SERVER_ID = int(os.getenv("NEW_SERVER_ID"))
NEW_SERVER_ROLE_ID = int(os.getenv("NEW_SERVER_ROLE_ID"))

# =========================
# BOT SETUP
# =========================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True

bot = commands.Bot(command_prefix='-', intents=intents)

app = Flask(__name__)

VERIFY_MESSAGE_ID = None

# =========================
# OAUTH URL
# =========================

def build_oauth_url():
    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": "identify guilds.join"
    }
    return "https://discord.com/oauth2/authorize?" + urllib.parse.urlencode(params)

# =========================
# READY EVENT
# =========================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

# =========================
# VERIFY PANEL
# =========================

@bot.command()
@commands.has_permissions(administrator=True)
async def setupverify(ctx):

    embed = discord.Embed(
        title="Verification System",
        description="React with ✅ to get verified.",
        color=0x5865F2
    )

    msg = await ctx.send(embed=embed)
    await msg.add_reaction("✅")

    global VERIFY_MESSAGE_ID
    VERIFY_MESSAGE_ID = msg.id

# =========================
# REACTION EVENT
# =========================

@bot.event
async def on_raw_reaction_add(payload):

    if payload.user_id == bot.user.id:
        return

    if str(payload.emoji) != "✅":
        return

    if payload.message_id != VERIFY_MESSAGE_ID:
        return

    guild = bot.get_guild(payload.guild_id)
    member = guild.get_member(payload.user_id) if guild else None

    if not member:
        return

    oauth_url = build_oauth_url()

    try:
        await member.send(
            embed=discord.Embed(
                title="Verify Account",
                description=f"[Click here to verify]({oauth_url})",
                color=0x5865F2
            )
        )
    except:
        print("DM failed")

# =========================
# OAUTH CALLBACK
# =========================

@app.route("/callback")
def callback():
    code = request.args.get("code")
    if not code:
        return "Missing code"

    data = {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
    }

    headers = {"Content-Type": "application/x-www-form-urlencoded"}

    token_res = requests.post(
        "https://discord.com/api/oauth2/token",
        data=data,
        headers=headers
    ).json()

    access_token = token_res.get("access_token")

    if not access_token:
        return "OAuth failed"

    user = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    ).json()

    user_id = user["id"]

    # =========================
    # ADD TO VERIFY SERVER
    # =========================

    requests.put(
        f"https://discord.com/api/guilds/{GUILD_ID}/members/{user_id}",
        json={"access_token": access_token},
        headers={"Authorization": f"Bot {TOKEN}"}
    )

    # ROLE ADD (VERIFY SERVER)
    guild = bot.get_guild(GUILD_ID)
    if guild:
        member = guild.get_member(int(user_id))
        role = guild.get_role(VERIFY_ROLE_ID)

        if member and role:
            asyncio.run_coroutine_threadsafe(
                member.add_roles(role),
                bot.loop
            )

    # =========================
    # ADD TO NEW SERVER
    # =========================

    requests.put(
        f"https://discord.com/api/guilds/{NEW_SERVER_ID}/members/{user_id}",
        json={"access_token": access_token},
        headers={"Authorization": f"Bot {TOKEN}"}
    )

    new_guild = bot.get_guild(NEW_SERVER_ID)

    if new_guild:
        member = new_guild.get_member(int(user_id))
        role = new_guild.get_role(NEW_SERVER_ROLE_ID)

        if member and role:
            asyncio.run_coroutine_threadsafe(
                member.add_roles(role),
                bot.loop
            )

    return "Verification complete. You can return to Discord."

# =========================
# RUN FLASK
# =========================

def run_web():
    app.run(host="0.0.0.0", port=int(os.getenv("PORT", 5000)))

threading.Thread(target=run_web).start()

bot.run(TOKEN)
