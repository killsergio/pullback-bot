from __future__ import annotations

import asyncio
import os
import secrets
import time
from pathlib import Path
from urllib.parse import urlencode

import aiohttp
import aiosqlite
import discord
from aiohttp import web
from discord.ext import commands


DB_PATH = Path(__file__).with_name("verification_system.db")
OAUTH_AUTHORIZE_URL = "https://discord.com/oauth2/authorize"
OAUTH_TOKEN_URL = "https://discord.com/api/oauth2/token"
OAUTH_ME_URL = "https://discord.com/api/users/@me"
DISCORD_API = "https://discord.com/api/v10"
STATE_TTL_SECONDS = 600


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing required environment variable: {name}")
    return value


class VerificationView(discord.ui.LayoutView):
    def __init__(self, cog: "Verification", guild_id: int) -> None:
        super().__init__(timeout=None)
        verify_url = f"{cog.public_url}/verify/start/{guild_id}"
        self.add_item(
            discord.ui.Container(
                discord.ui.TextDisplay(
                    "# 🔒 Community Verification\n"
                    "Verify to stay connected with our community. If this server is ever deleted, "
                    "unavailable, or moved, verified members can receive an invite to our new official "
                    "server so you don't lose your place."
                ),
                discord.ui.Separator(visible=True, spacing=discord.SeparatorSpacing.small),
                discord.ui.ActionRow(
                    discord.ui.Button(
                        label="Authenticate via Discord",
                        emoji="✅",
                        style=discord.ButtonStyle.link,
                        url=verify_url,
                    )
                ),
            )
        )


class PullProgress(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=None)
        self.author_id = author_id
        self.cancelled = asyncio.Event()

    @discord.ui.button(label="Stop Pull", style=discord.ButtonStyle.danger)
    async def stop_pull(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message("Only the bot owner who started this pull can stop it.", ephemeral=True)
            return
        self.cancelled.set()
        button.disabled = True
        await interaction.response.edit_message(view=self)


def pull_progress_embed(processed: int, total: int, added: int, existing: int, failed: int, state: str = "") -> discord.Embed:
    percentage = int(processed * 100 / total) if total else 0
    filled = int(processed * 12 / total) if total else 0
    bar = "▰" * filled + "─" * (12 - filled)
    return discord.Embed(
        title=f"Server Restore - {state or str(percentage) + '%'}",
        description=f"**Progress:** {processed}/{total}\n`[{bar}]`\n\n> Added: **{added}** | Already in: **{existing}** | Failed: **{failed}**",
        colour=0x000000,
    )


class Verification(commands.Cog):
    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.db: aiosqlite.Connection | None = None
        self.web_runner: web.AppRunner | None = None
        self.web_session: aiohttp.ClientSession | None = None
        self._db_lock = asyncio.Lock()
        self._pulls: dict[int, PullProgress] = {}

    @property
    def public_url(self) -> str:
        return _required("VERIFY_PUBLIC_URL").rstrip("/")

    @property
    def redirect_uri(self) -> str:
        return _required("VERIFY_REDIRECT_URI")

    async def cog_load(self) -> None:
        configured_path = os.getenv("VERIFY_DB_PATH", "").strip()
        db_path = Path(configured_path).expanduser() if configured_path else DB_PATH
        if configured_path and not db_path.is_absolute():
            raise RuntimeError("VERIFY_DB_PATH must be an absolute path on persistent storage.")
        self.db = await aiosqlite.connect(db_path)
        await self.db.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS verification_owners(
                user_id INTEGER PRIMARY KEY,
                added_by INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verification_settings(
                guild_id INTEGER PRIMARY KEY,
                role_id INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS oauth_states(
                state TEXT PRIMARY KEY,
                guild_id INTEGER NOT NULL,
                created_at INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS verified_users(
                user_id INTEGER PRIMARY KEY,
                access_token TEXT NOT NULL,
                refresh_token TEXT NOT NULL,
                expires_at INTEGER NOT NULL,
                verified_at INTEGER NOT NULL
            );
            """
        )
        await self.db.commit()
        self.web_session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=20))
        await self._start_web_server()

    async def cog_unload(self) -> None:
        for view in self._pulls.values():
            view.cancelled.set()
        if self.web_runner:
            await self.web_runner.cleanup()
        if self.web_session:
            await self.web_session.close()
        if self.db:
            await self.db.close()

    async def _start_web_server(self) -> None:
        # Railway and most hosts pass the port through PORT.  The service stays
        # disabled until all OAuth settings are supplied, rather than crashing
        # the bot at startup.
        if not all(os.getenv(name, "").strip() for name in (
            "DISCORD_CLIENT_ID", "DISCORD_CLIENT_SECRET", "VERIFY_PUBLIC_URL", "VERIFY_REDIRECT_URI"
        )):
            return
        app = web.Application()
        app.router.add_get("/verify/start/{guild_id}", self.oauth_start)
        app.router.add_get("/verify/callback", self.oauth_callback)
        self.web_runner = web.AppRunner(app)
        await self.web_runner.setup()
        port = int(os.getenv("PORT", "8080"))
        site = web.TCPSite(self.web_runner, host="0.0.0.0", port=port)
        await site.start()

    async def _role_id(self, guild_id: int) -> int | None:
        assert self.db
        async with self.db.execute("SELECT role_id FROM verification_settings WHERE guild_id=?", (guild_id,)) as cursor:
            row = await cursor.fetchone()
        return int(row[0]) if row else None

    async def _set_role(self, guild_id: int, role_id: int) -> None:
        assert self.db
        async with self._db_lock:
            await self.db.execute(
                "INSERT OR REPLACE INTO verification_settings(guild_id, role_id) VALUES(?, ?)",
                (guild_id, role_id),
            )
            await self.db.commit()

    async def oauth_start(self, request: web.Request) -> web.StreamResponse:
        try:
            guild_id = int(request.match_info["guild_id"])
            _required("DISCORD_CLIENT_ID")
            self.redirect_uri
        except (ValueError, RuntimeError):
            raise web.HTTPBadRequest(text="Verification is not configured correctly.")
        if not self.bot.get_guild(guild_id) or not await self._role_id(guild_id):
            raise web.HTTPNotFound(text="This verification link is no longer active.")

        state = secrets.token_urlsafe(32)
        assert self.db
        async with self._db_lock:
            await self.db.execute(
                "INSERT INTO oauth_states(state, guild_id, created_at) VALUES(?, ?, ?)",
                (state, guild_id, int(time.time())),
            )
            await self.db.execute("DELETE FROM oauth_states WHERE created_at < ?", (int(time.time()) - STATE_TTL_SECONDS,))
            await self.db.commit()
        params = urlencode({
            "client_id": _required("DISCORD_CLIENT_ID"),
            "redirect_uri": self.redirect_uri,
            "response_type": "code",
            "scope": "identify guilds.join",
            "state": state,
            "prompt": "consent",
        })
        raise web.HTTPFound(f"{OAUTH_AUTHORIZE_URL}?{params}")

    async def oauth_callback(self, request: web.Request) -> web.StreamResponse:
        if request.query.get("error"):
            return web.Response(text="Verification was cancelled. You can close this page.", content_type="text/plain")
        code = request.query.get("code")
        state = request.query.get("state")
        if not code or not state:
            raise web.HTTPBadRequest(text="Missing OAuth response data.")
        assert self.db
        async with self._db_lock:
            async with self.db.execute("SELECT guild_id, created_at FROM oauth_states WHERE state=?", (state,)) as cursor:
                row = await cursor.fetchone()
            await self.db.execute("DELETE FROM oauth_states WHERE state=?", (state,))
            await self.db.commit()
        if not row or int(row[1]) < int(time.time()) - STATE_TTL_SECONDS:
            raise web.HTTPBadRequest(text="This verification link expired. Please try again from Discord.")
        guild_id = int(row[0])

        try:
            token_data = await self._exchange_code(code)
            user = await self._oauth_user(token_data["access_token"])
            user_id = int(user["id"])
            await self._save_verified_user(user_id, token_data)
            await self._give_role(guild_id, user_id)
        except (KeyError, aiohttp.ClientError, discord.HTTPException, RuntimeError, ValueError) as exc:
            # Never reveal OAuth data or tokens to the browser.
            raise web.HTTPBadGateway(text="Verification could not be completed. Please return to Discord and try again.") from exc
        return web.Response(
            text="You are verified. You can safely close this page and return to Discord.",
            content_type="text/plain",
        )

    async def _exchange_code(self, code: str) -> dict:
        assert self.web_session
        payload = {
            "client_id": _required("DISCORD_CLIENT_ID"),
            "client_secret": _required("DISCORD_CLIENT_SECRET"),
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": self.redirect_uri,
        }
        async with self.web_session.post(OAUTH_TOKEN_URL, data=payload) as response:
            response.raise_for_status()
            return await response.json()

    async def _oauth_user(self, access_token: str) -> dict:
        assert self.web_session
        async with self.web_session.get(OAUTH_ME_URL, headers={"Authorization": f"Bearer {access_token}"}) as response:
            response.raise_for_status()
            return await response.json()

    async def _save_verified_user(self, user_id: int, data: dict) -> None:
        assert self.db
        async with self._db_lock:
            await self.db.execute(
                """INSERT OR REPLACE INTO verified_users(user_id, access_token, refresh_token, expires_at, verified_at)
                   VALUES(?, ?, ?, ?, ?)""",
                (
                    user_id,
                    data["access_token"],
                    data.get("refresh_token", ""),
                    int(time.time()) + int(data.get("expires_in", 0)),
                    int(time.time()),
                ),
            )
            await self.db.commit()

    async def _give_role(self, guild_id: int, user_id: int) -> None:
        guild = self.bot.get_guild(guild_id)
        role_id = await self._role_id(guild_id)
        if guild is None or role_id is None:
            raise RuntimeError("Guild verification settings are missing")
        role = guild.get_role(role_id)
        bot_member = guild.me
        if role is None or bot_member is None or role >= bot_member.top_role:
            raise RuntimeError("Verification role cannot be assigned")
        member = guild.get_member(user_id) or await guild.fetch_member(user_id)
        if role not in member.roles:
            await member.add_roles(role, reason="Member completed Discord OAuth verification")

    async def _refresh_access_token(self, refresh_token: str) -> dict:
        assert self.web_session
        payload = {
            "client_id": _required("DISCORD_CLIENT_ID"),
            "client_secret": _required("DISCORD_CLIENT_SECRET"),
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
        }
        async with self.web_session.post(OAUTH_TOKEN_URL, data=payload) as response:
            response.raise_for_status()
            return await response.json()

    async def _valid_token(self, user_id: int, access_token: str, refresh_token: str, expires_at: int) -> str:
        if expires_at > int(time.time()) + 60:
            return access_token
        data = await self._refresh_access_token(refresh_token)
        await self._save_verified_user(user_id, data)
        return data["access_token"]

    async def _join_guild(self, guild: discord.Guild, user_id: int, token: str, role_id: int | None) -> bool:
        assert self.web_session
        bot_token = getattr(self.bot.http, "token", None)
        if not bot_token:
            raise RuntimeError("Bot token is not available")
        body: dict[str, object] = {"access_token": token}
        if role_id:
            body["roles"] = [str(role_id)]
        async with self.web_session.put(
            f"{DISCORD_API}/guilds/{guild.id}/members/{user_id}",
            headers={"Authorization": f"Bot {bot_token}"},
            json=body,
        ) as response:
            if response.status == 201:
                return True
            if response.status == 204:
                return False
            response.raise_for_status()
            return False

    def _can_manage(self, ctx: commands.Context) -> bool:
        return bool(ctx.guild and (ctx.author.guild_permissions.manage_guild or ctx.author.id == ctx.guild.owner_id))

    async def is_verification_owner(self, user: discord.abc.User) -> bool:
        owner_check = getattr(self.bot, "_sunset_original_is_owner", self.bot.is_owner)
        if await owner_check(user):
            return True
        assert self.db
        async with self.db.execute("SELECT 1 FROM verification_owners WHERE user_id=?", (user.id,)) as cursor:
            return await cursor.fetchone() is not None

    async def require_verification_owner(self, ctx) -> None:
        if not await self.is_verification_owner(ctx.author):
            raise commands.NotOwner("Only verification bot owners can use this command.")

    @commands.group(name="verifyowner", aliases=["vowner"], invoke_without_command=True)
    async def verify_owner(self, ctx):
        await self.require_verification_owner(ctx)
        await ctx.send("Use `verifyowner add @user`, `verifyowner remove @user`, or `verifyowner list`.")

    @verify_owner.command(name="add")
    async def verify_owner_add(self, ctx, user: discord.User):
        await self.require_verification_owner(ctx)
        if user.bot:
            raise commands.BadArgument("Choose a human user.")
        assert self.db
        async with self._db_lock:
            await self.db.execute("INSERT OR IGNORE INTO verification_owners(user_id,added_by) VALUES(?,?)", (user.id, ctx.author.id))
            await self.db.commit()
        await ctx.send(f"<:approve:1556620985640165396> Added {user.mention} as a verification bot owner.", allowed_mentions=discord.AllowedMentions.none())

    @verify_owner.command(name="remove")
    async def verify_owner_remove(self, ctx, user: discord.User):
        await self.require_verification_owner(ctx)
        assert self.db
        async with self._db_lock:
            cursor = await self.db.execute("DELETE FROM verification_owners WHERE user_id=?", (user.id,))
            removed = cursor.rowcount > 0
            await self.db.commit()
        text = f"Removed {user.mention} from additional verification owners." if removed else "That user isn't an additional verification owner. Configured owners must be removed from the host configuration."
        await ctx.send(text, allowed_mentions=discord.AllowedMentions.none())

    @verify_owner.command(name="list")
    async def verify_owner_list(self, ctx):
        await self.require_verification_owner(ctx)
        assert self.db
        async with self.db.execute("SELECT user_id FROM verification_owners ORDER BY user_id") as cursor:
            rows = await cursor.fetchall()
        description = "\n".join(f"<@{row[0]}> (`{row[0]}`)" for row in rows) or "No additional owners."
        for offset in range(0, len(description), 1900):
            await ctx.send(description[offset:offset + 1900], allowed_mentions=discord.AllowedMentions.none())

    @commands.command(name="verifysend")
    @commands.guild_only()
    async def verify_send(self, ctx: commands.Context, channel: discord.TextChannel | None = None, role: discord.Role | None = None) -> None:
        if not self._can_manage(ctx) and not await self.is_verification_owner(ctx.author):
            raise commands.MissingPermissions(["manage_guild"])
        # This deliberately checks all settings before publishing a link.
        self.public_url
        _required("DISCORD_CLIENT_ID")
        _required("DISCORD_CLIENT_SECRET")
        self.redirect_uri
        channel = channel or (ctx.channel if isinstance(ctx.channel, discord.TextChannel) else None)
        if channel is None:
            raise commands.BadArgument("Choose a text channel for the verification panel.")
        role = role or ctx.guild.get_role(await self._role_id(ctx.guild.id) or 0) or discord.utils.get(ctx.guild.roles, name="Verified")
        if role is None:
            role = await ctx.guild.create_role(name="Verified", reason=f"Verification setup by {ctx.author}")
        if ctx.guild.me is None or role >= ctx.guild.me.top_role:
            raise commands.CheckFailure("The verification role must be below my top role.")
        await self._set_role(ctx.guild.id, role.id)
        await channel.send(view=VerificationView(self, ctx.guild.id), allowed_mentions=discord.AllowedMentions.none())
        await ctx.message.add_reaction("👍")

    @commands.command(name="pull")
    @commands.guild_only()
    async def pull(self, ctx: commands.Context, target: str | None = None) -> None:
        """Add every OAuth-verified user, or one selected user, to this server."""
        if not await self.is_verification_owner(ctx.author):
            raise commands.NotOwner("Only bot owners can use pull.")
        assert self.db
        if target and target.casefold() == "list":
            async with self.db.execute("SELECT COUNT(*) FROM verified_users") as cursor:
                row = await cursor.fetchone()
            return await ctx.send(f"**{row[0]:,}** saved verified users in the pull list.")
        if ctx.guild.id in self._pulls:
            raise commands.BadArgument("A pull is already running in this server.")
        role_id = await self._role_id(ctx.guild.id)
        if target and target.lower() not in {"all", "everyone"}:
            try:
                user = await commands.UserConverter().convert(ctx, target)
            except commands.UserNotFound:
                raise commands.BadArgument("Use `-pull everyone`, `-pull all`, or provide a valid user.")
            async with self.db.execute(
                "SELECT user_id, access_token, refresh_token, expires_at FROM verified_users WHERE user_id=?", (user.id,)
            ) as cursor:
                rows = await cursor.fetchall()
        else:
            async with self.db.execute("SELECT user_id, access_token, refresh_token, expires_at FROM verified_users") as cursor:
                rows = await cursor.fetchall()
        if not rows:
            return await ctx.send("No OAuth-verified users are saved yet.")

        view = PullProgress(ctx.author.id)
        self._pulls[ctx.guild.id] = view
        added = existing = failed = processed = 0
        message = None
        last_update = time.monotonic()
        try:
            message = await ctx.send(embed=pull_progress_embed(0, len(rows), 0, 0, 0), view=view)
            for row in rows:
                if view.cancelled.is_set():
                    break
                user_id, access_token, refresh_token, expires_at = int(row[0]), row[1], row[2], int(row[3])
                try:
                    if ctx.guild.get_member(user_id) is not None:
                        existing += 1
                    else:
                        token = await self._valid_token(user_id, access_token, refresh_token, expires_at)
                        if view.cancelled.is_set():
                            break
                        if await self._join_guild(ctx.guild, user_id, token, role_id):
                            added += 1
                        else:
                            existing += 1
                except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, RuntimeError, ValueError):
                    failed += 1
                processed += 1
                if time.monotonic() - last_update >= 3:
                    await message.edit(embed=pull_progress_embed(processed, len(rows), added, existing, failed), view=view)
                    last_update = time.monotonic()
                try:
                    await asyncio.wait_for(view.cancelled.wait(), timeout=0.25)
                except asyncio.TimeoutError:
                    pass
            await message.edit(embed=pull_progress_embed(processed, len(rows), added, existing, failed, "Stopped" if view.cancelled.is_set() else "Complete"), view=None)
        finally:
            self._pulls.pop(ctx.guild.id, None)
            view.stop()


async def setup_verification(bot: commands.Bot) -> None:
    if bot.get_cog("Verification") is None:
        await bot.add_cog(Verification(bot))
