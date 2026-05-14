import discord
from discord.ext import commands
from flask import Flask, request
import requests
import threading
import asyncio
import urllib.parse
import os

# =========================
# CONFIG
# =========================

TOKEN = "MTUwNDYwMzIwMDQzMzQyNjU4Mg.GFw_YA.D3eXvGGeXS9p-8X1OGPUmBtxbZVSw-3qleLyzA"

CLIENT_ID = "1504603200433426582"
CLIENT_SECRET = "S4AAnZK0BeHKOE-hLao10Pk99OVKV_Re"
REDIRECT_URI = "https://propose-bubbling-deceiver.ngrok-free.dev/callback"

# MAIN VERIFY SERVER
VERIFY_SERVERS = {
    1462909773358829601: 1462913788213989409,
    1495284591827882126: 1495285112311513298,
    1502453664554680422: 1502903551620546590
}

# SERVER USERS GET MOVED INTO
NEW_SERVER_ID = 1491056946005147718
NEW_SERVER_ROLE_ID = 1503722662596186373

# =========================
# BOT SETUP
# =========================

intents = discord.Intents.default()
intents.members = True
intents.message_content = True
intents.reactions = True

bot = commands.Bot(command_prefix='-', intents=intents)

app = Flask(__name__)

VERIFY_MESSAGE_ID = None

# =========================
# READY EVENT
# =========================

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    print(f'Bot is in {len(bot.guilds)} servers.')

# =========================
# SETUP VERIFY PANEL
# =========================

@bot.command()
@commands.has_permissions(administrator=True)
async def setupverify(ctx):

    embed = discord.Embed(
        title='Verification System',
        description='React with ✅ below to receive the verification link in DMs.',
        color=0x5865F2
    )

    embed.add_field(
        name='How It Works',
        value='React → Receive DM → Authorize → Get Verified',
        inline=False
    )

    embed.set_footer(text='Secure Discord OAuth2 Verification')

    msg = await ctx.send(embed=embed)

    await msg.add_reaction('✅')

    global VERIFY_MESSAGE_ID
    VERIFY_MESSAGE_ID = msg.id

# =========================
# BUILD OAUTH URL (FIXED)
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
# SERVER COUNT COMMAND
# =========================

@bot.command()
async def servers(ctx):

    embed = discord.Embed(
        title='Bot Statistics',
        description=f'Currently in **{len(bot.guilds)}** servers.',
        color=0x57F287
    )

    await ctx.send(embed=embed)

# =========================
# REACTION VERIFY SYSTEM
# =========================

@bot.event
async def on_raw_reaction_add(payload):

    if payload.user_id == bot.user.id:
        return

    if str(payload.emoji) != '✅':
        return

    if payload.message_id != VERIFY_MESSAGE_ID:
        return

    guild = bot.get_guild(payload.guild_id)
    if not guild:
        return

    member = guild.get_member(payload.user_id)
    if not member:
        return

    # ✅ HERE is where it goes
    oauth_url = build_oauth_url()

    embed = discord.Embed(
        title='Verify Your Discord Account',
        description='Click the button below to verify and join the server.',
        color=0x5865F2
    )

    embed.add_field(
        name='Verification Link',
        value=f'[Click Here To Verify]({oauth_url})',
        inline=False
    )

    embed.set_footer(text='Verification expires automatically.')

    try:
        await member.send(embed=embed)

    except:
        print(f'Could not DM {member}')

# =========================
# OAUTH CALLBACK
# =========================

@app.route("/callback")
def callback():
    code = request.args.get("code")

    if not code:
        return "Missing code"

    # Exchange code for token
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
    )

    token_json = token_res.json()
    access_token = token_json.get("access_token")

    if not access_token:
        return "OAuth failed"

    # Get user
    user_res = requests.get(
        "https://discord.com/api/users/@me",
        headers={"Authorization": f"Bearer {access_token}"}
    )

    user = user_res.json()
    user_id = user["id"]


    # =========================
    # ADD USER TO VERIFY SERVER
    # =========================

    requests.put(
        f'https://discord.com/api/guilds/{GUILD_ID}/members/{user_id}',
        json={
            'access_token': access_token
        },
        headers={
            'Authorization': f'Bot {TOKEN}',
            'Content-Type': 'application/json'
        }
    )

    # =========================
    # ADD USER TO NEW SERVER
    # =========================

    requests.put(
        f'https://discord.com/api/guilds/{NEW_SERVER_ID}/members/{user_id}',
        json={
            'access_token': access_token
        },
        headers={
            'Authorization': f'Bot {TOKEN}',
            'Content-Type': 'application/json'
        }
    )

    verify_guild = bot.get_guild(GUILD_ID)

    if verify_guild:

        member = verify_guild.get_member(int(user_id))

        if member:

            role = verify_guild.get_role(VERIFY_ROLE_ID)

            if role:

                asyncio.run_coroutine_threadsafe(
                    member.add_roles(role),
                    bot.loop
                )

    new_guild = bot.get_guild(NEW_SERVER_ID)

    if new_guild:

        member = new_guild.get_member(int(user_id))

        if member:

            role = new_guild.get_role(NEW_SERVER_ROLE_ID)

            if role:

                asyncio.run_coroutine_threadsafe(
                    member.add_roles(role),
                    bot.loop
                )

    return '''
    <html>
    <head>
        <title>Verification Complete</title>
    </head>
    <body style="background:#2b2d31;color:white;font-family:sans-serif;text-align:center;padding-top:100px;">
        <h1>✅ Verification Complete</h1>
        <p>You may now return to Discord.</p>
    </body>
    </html>
    '''

# =========================
# START FLASK SERVER
# =========================

def run_web():
    app.run(host='0.0.0.0', port=5000)

threading.Thread(target=run_web).start()

# =========================
# RUN BOT
# =========================

bot.run(TOKEN)
