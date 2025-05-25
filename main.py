# discord.py を使った「同期ゲームセッション」ボット

import json
import random
import time
import asyncio

import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import View, Button

# config.json から BOT_TOKEN, APPLICATION_ID, GUILD_ID を読み込む
with open("config.json", encoding="utf-8") as f:
    config = json.load(f)

BOT_TOKEN      = config.get("token")
# APPLICATION_ID = config.get("applicationId")
APPLICATION_ID = int(config.get("applicationId"))
# GUILD_ID       = config.get("guildId", 0)
GUILD_ID       = int(config.get("guildId", 0))

if not BOT_TOKEN or not APPLICATION_ID or not GUILD_ID:
    print("config.json に token, applicationId, guildId のいずれかが設定されていません。")
    exit(1)

# JSON ファイル読み込み
with open("locations.json", encoding="utf-8") as f:
    locations = json.load(f)
with open("settings.json", encoding="utf-8") as f:
    settings = json.load(f)

# 有効化されたロケーションだけ抽出
active_locations = [loc for loc in locations if settings.get(loc["area"], False)]

# ゲームに参加する人数設定
nmember = 4

# ゲーム状態を保持する辞書: channel_id -> state
# state = {
#   "host": int,
#   "members": [int, ...],
#   "assignments": { user_id: loc_dict, ... },
#   "start_time": float
# }
games: dict[int, dict] = {}

# ランダム重複なし抽出
def choose_random_unique(lst: list, n: int) -> list:
    return random.sample(lst, k=min(n, len(lst)))

class GameView(View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="参加", style=discord.ButtonStyle.success, custom_id="join_game")
    async def join_button(self, interaction: discord.Interaction, button: Button):
        state = games.setdefault(self.channel_id, {
            "host": None,
            "members": [],
            "assignments": {},
            "start_time": None
        })
        user_id = interaction.user.id

        # ホスト未設定なら最初の参加者をホストに
        if state["host"] is None:
            state["host"] = user_id

        if user_id in state["members"]:
            return await interaction.response.send_message("既に参加しています。", ephemeral=True)

        if len(state["members"]) >= nmember:
            return await interaction.response.send_message("参加人数が多すぎて参加できません。", ephemeral=True)

        state["members"].append(user_id)

        # DM でキャンセルボタン付きメッセージ
        dm_view = CancelView(self.channel_id)
        await interaction.user.send(
            "参加を承認しました。キャンセルするにはボタンを押してください。",
            view=dm_view
        )
        await interaction.response.send_message("参加できました。DMを確認してください。", ephemeral=True)

        # 人数が揃ったらスタート準備
        if len(state["members"]) == nmember:
            await notify_ready(self.channel_id, state)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.danger, custom_id="leave_game")
    async def leave_button(self, interaction: discord.Interaction, button: Button):
        state = games.get(self.channel_id)
        user_id = interaction.user.id
        if not state or user_id not in state["members"]:
            return await interaction.response.send_message("参加していません。", ephemeral=True)

        state["members"].remove(user_id)
        if state["host"] == user_id:
            state["host"] = state["members"][0] if state["members"] else None

        await interaction.response.send_message("参加をキャンセルしました。", ephemeral=True)

class CancelView(View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="参加キャンセル", style=discord.ButtonStyle.danger, custom_id="leave_game")
    async def cancel_button(self, interaction: discord.Interaction, button: Button):
        # DM 内のキャンセルは join と同じ leave ボタン処理を呼び出す
        await GameView(self.channel_id).leave_button(interaction, button)

class StartCancelView(View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="開始", style=discord.ButtonStyle.primary, custom_id="start_game")
    async def start_button(self, interaction: discord.Interaction, button: Button):
        state = games.get(self.channel_id)
        user_id = interaction.user.id
        if not state or user_id != state["host"]:
            return await interaction.response.send_message("ホストのみ開始できます。", ephemeral=True)
        if len(state["members"]) < nmember:
            return await interaction.response.send_message("まだメンバーが揃っていません。", ephemeral=True)

        picks = choose_random_unique(active_locations, nmember)
        # 割り当て
        for uid, loc in zip(state["members"], picks):
            state["assignments"][uid] = loc
        state["start_time"] = time.time()

        # 各メンバーに割り当て DM
        for uid in state["members"]:
            user = await interaction.client.fetch_user(uid)
            loc = state["assignments"][uid]
            await user.send(f"あなたの初期ワープポイントは **{loc['area']} {loc['type']}: {loc['description']}** です")

        # ホストに「ゲーム終了」ボタン
        end_view = EndView(self.channel_id)
        await interaction.user.send("ゲームを開始しました。終了するにはボタンを押してください。", view=end_view)
        await interaction.response.send_message("ゲームを開始しました！", ephemeral=True)

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary, custom_id="end_cancel")
    async def cancel_start(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_message("ゲーム開始をキャンセルしました。", ephemeral=True)

class EndView(View):
    def __init__(self, channel_id: int):
        super().__init__(timeout=None)
        self.channel_id = channel_id

    @discord.ui.button(label="ゲーム終了", style=discord.ButtonStyle.danger, custom_id="end_game")
    async def end_button(self, interaction: discord.Interaction, button: Button):
        state = games.get(self.channel_id)
        user_id = interaction.user.id
        if not state or user_id != state["host"] or not state["start_time"]:
            return await interaction.response.send_message("ゲームは開始されていません。", ephemeral=True)

        elapsed = time.time() - state["start_time"]
        h, rem = divmod(int(elapsed), 3600)
        m, s = divmod(rem, 60)
        timestr = f"{h}:{m:02d}:{s:02d}"

        # 全員に終了通知
        for uid in state["members"]:
            user = await interaction.client.fetch_user(uid)
            await user.send(f"ゲーム終了。記録は {timestr} でした")

        # 状態をクリア
        games.pop(self.channel_id, None)
        await interaction.response.send_message(f"ゲームを終了しました。記録は {timestr} です。", ephemeral=True)

async def notify_ready(channel_id: int, state: dict):
    """メンバーが揃ったらホストと他のメンバーに通知する"""
    guild = bot.get_guild(GUILD_ID)
    host = await bot.fetch_user(state["host"])
    member_mentions = "\n".join(f"{i+1}. <@{uid}>" for i, uid in enumerate(state["members"]))

    # ホストへ開始/キャンセルボタン
    await host.send(f"メンバーが揃いました！\n{member_mentions}", view=StartCancelView(channel_id))

    # 他のメンバーへ待機メッセージ
    for uid in state["members"]:
        if uid == state["host"]:
            continue
        user = await bot.fetch_user(uid)
        await user.send(f"ホストが開始ボタンを押すのを待っています。\n{member_mentions}")

# Bot 本体
bot = commands.Bot(
    command_prefix="/", 
    intents=discord.Intents.default(),
    application_id=APPLICATION_ID
)

@bot.event
async def on_ready():
    # スラッシュコマンド登録
    # await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    # print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    # ギルドコマンドを即時同期
    synced = await bot.tree.sync(guild=discord.Object(id=GUILD_ID))
    print(f"Synced {len(synced)} commands to guild {GUILD_ID}")

@bot.tree.command(
    guild=discord.Object(id=GUILD_ID),
    name="game",
    description="同期ゲームセッションを開始します"
)
async def game(interaction: discord.Interaction):
    """/game コマンド"""
    channel_id = interaction.channel_id
    # 初期状態を設定
    games[channel_id] = {"host": None, "members": [], "assignments": {}, "start_time": None}
    # 参加・キャンセルボタンを返信
    await interaction.response.send_message("ゲームに参加しますか？", view=GameView(channel_id))

bot.run(BOT_TOKEN)
