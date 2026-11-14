import os
import asyncio
import random
import discord
from discord import app_commands
from discord.ext import commands
import yt_dlp
from gtts import gTTS
from keep_alive import start_web_server

# --- yt-dlp の設定（音声抽出の安定化） ---
ytdl_format_options = {
    'format': 'bestaudio/best',
    'outtmpl': '%(extractor)s-%(id)s-%(title)s.%(ext)s',
    'restrictfilenames': True,
    'noplaylist': True,
    'default_search': 'ytsearch',
    'source_address': '0.0.0.0',
    'quiet': True,
    'no_warnings': True,
    'extract_flat': False,
}

ytdl = yt_dlp.YoutubeDL(ytdl_format_options)

# --- FFmpegのオプション設定（音切れ防止） ---
FFMPEG_OPTIONS = {
    'before_options': '-reconnect 1 -reconnect_streamed 1 -reconnect_delay_max 5 -nostdin',
    'options': '-vn -b:a 192k'
}

# --- Botの設定 ---
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True

class MusicTTSBot(commands.Bot):
    def __init__(self):
        super().__init__(command_prefix='!', intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("スラッシュコマンドを同期しました。")

bot = MusicTTSBot()

# サーバーごとの状態管理
guild_states = {}

def get_state(guild_id):
    if guild_id not in guild_states:
        guild_states[guild_id] = {
            'queue': [],
            'current': None,
            'volume': 0.5,
            'loop': False,
            'loop_queue': False,
            'nightcore': False,
            'bassboost': False,
            'tts_enabled': False,
            'tts_channel': None,
            'tts_queue': asyncio.Queue(),
            'is_speaking_tts': False
        }
    return guild_states[guild_id]

def get_ffmpeg_source(url, state):
    filters = []
    if state['nightcore']:
        filters.append("asetrate=44100*1.25,aresample=44100,atempo=1.05")
    if state['bassboost']:
        filters.append("bass=g=10:f=110:w=0.6")
    
    options = FFMPEG_OPTIONS['options']
    if filters:
        filter_str = ','.join(filters)
        options = f'-vn -af "{filter_str}"'

    return discord.FFmpegPCMAudio(
        url,
        executable="ffmpeg",
        before_options=FFMPEG_OPTIONS['before_options'],
        options=options
    )

# --- 操作用ボタン（UI View） ---
class MusicControls(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="一時停止/再開", style=discord.ButtonStyle.primary, emoji="⏯️")
    async def pause_resume(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if not vc:
            return await interaction.response.send_message("Botがボイスチャンネルにいません。", ephemeral=True)
        
        if vc.is_playing():
            vc.pause()
            await interaction.response.send_message("⏸️ 一時停止しました。", ephemeral=True)
        elif vc.is_paused():
            vc.resume()
            await interaction.response.send_message("▶️ 再生を再開しました。", ephemeral=True)
        else:
            await interaction.response.send_message("再生中の曲がありません。", ephemeral=True)

    @discord.ui.button(label="スキップ", style=discord.ButtonStyle.secondary, emoji="⏭️")
    async def skip(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
            await interaction.response.send_message("⏭️ スキップしました。", ephemeral=True)
        else:
            await interaction.response.send_message("スキップできる曲がありません。", ephemeral=True)

    @discord.ui.button(label="ストップ", style=discord.ButtonStyle.danger, emoji="⏹️")
    async def stop(self, interaction: discord.Interaction, button: discord.ui.Button):
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            state = get_state(interaction.guild.id)
            state['loop'] = False
            vc.stop()
            await interaction.response.send_message("⏹️ 再生を停止しました。", ephemeral=True)
        else:
            await interaction.response.send_message("再生中の曲がありません。", ephemeral=True)

    @discord.ui.button(label="キュー確認", style=discord.ButtonStyle.success, emoji="📜")
    async def queue_list(self, interaction: discord.Interaction, button: discord.ui.Button):
        state = get_state(interaction.guild.id)
        if not state['current'] and not state['queue']:
            return await interaction.response.send_message("キューは空です。", ephemeral=True)
        
        msg = ""
        if state['current']:
            msg += f"**現在再生中:** {state['current']['title']}\n\n"
        if state['queue']:
            msg += "**再生待ちリスト:**\n"
            for i, song in enumerate(state['queue'][:10], 1):
                msg += f"`{i}.` {song['title']}\n"
        await interaction.response.send_message(msg, ephemeral=True)


async def play_next(guild: discord.Guild, channel: discord.abc.Messageable):
    state = get_state(guild.id)
    vc = guild.voice_client
    if not vc:
        return

    if state['loop'] and state['current']:
        song = state['current']
    else:
        if state['loop_queue'] and state['current']:
            state['queue'].append(state['current'])
        
        if len(state['queue']) > 0:
            song = state['queue'].pop(0)
            state['current'] = song
        else:
            state['current'] = None
            return

    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(song['webpage_url'], download=False))
        if 'entries' in data:
            data = data['entries'][0]
        url = data['url']
    except Exception as e:
        await channel.send(f"⚠️ 再生エラーが発生しました（スキップします）: {e}")
        return await play_next(guild, channel)

    try:
        source = get_ffmpeg_source(url, state)
        source = discord.PCMVolumeTransformer(source, volume=state['volume'])
    except Exception as e:
        await channel.send(f"⚠️ 音声ソースの作成に失敗しました: {e}")
        return

    def after_playing(error):
        if error:
            print(f"Player error: {error}")
        coro = play_next(guild, channel)
        fut = asyncio.run_coroutine_threadsafe(coro, bot.loop)
        try:
            fut.result()
        except Exception as e:
            print(f"Error in after_playing: {e}")

    vc.play(source, after=after_playing)
    view = MusicControls()
    await channel.send(f"🎶 再生中: **{song['title']}**", view=view)

# --- TTS（読み上げ）処理 ---
async def process_tts(guild: discord.Guild):
    state = get_state(guild.id)
    if state['is_speaking_tts']:
        return
    state['is_speaking_tts'] = True

    while not state['tts_queue'].empty():
        text = await state['tts_queue'].get()
        vc = guild.voice_client
        if vc and vc.is_connected() and not vc.is_playing():
            filename = f"tts_{guild.id}.mp3"
            try:
                loop = asyncio.get_event_loop()
                await loop.run_in_executor(None, lambda: gTTS(text=text, lang='ja').save(filename))
                
                done_event = asyncio.Event()
                source = discord.FFmpegPCMAudio(filename, executable="ffmpeg")
                vc.play(source, after=lambda e: bot.loop.call_soon_threadsafe(done_event.set))
                await done_event.wait()
            except Exception as e:
                print(f"TTS Error: {e}")
            finally:
                if os.path.exists(filename):
                    os.remove(filename)
        state['tts_queue'].task_done()

    state['is_speaking_tts'] = False

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user.name}')

@bot.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return

    state = get_state(message.guild.id)
    if state['tts_enabled'] and message.channel.id == state['tts_channel']:
        if not message.content.startswith('/') and not message.content.startswith('http'):
            clean_text = message.clean_content[:100]
            await state['tts_queue'].put(clean_text)
            await process_tts(message.guild)

# ================= スラッシュコマンド（音楽20個 ＋ TTS ＋ パネル） =================

@bot.tree.command(name="join", description="ボイスチャンネルに接続します")
async def join(interaction: discord.Interaction):
    if not interaction.user.voice:
        return await interaction.response.send_message("先にボイスチャンネルに入ってください。", ephemeral=True)
    channel = interaction.user.voice.channel
    if interaction.guild.voice_client:
        await interaction.guild.voice_client.move_to(channel)
    else:
        await channel.connect()
    await interaction.response.send_message(f"🔊 **{channel.name}** に接続しました。")

@bot.tree.command(name="leave", description="ボイスチャンネルから切断し、キューをクリアします")
async def leave(interaction: discord.Interaction):
    if interaction.guild.voice_client:
        state = get_state(interaction.guild.id)
        state['queue'].clear()
        state['current'] = None
        state['tts_enabled'] = False
        await interaction.guild.voice_client.disconnect()
        await interaction.response.send_message("👋 ボイスチャンネルから切断しました。")
    else:
        await interaction.response.send_message("ボイスチャンネルに接続していません。", ephemeral=True)

@bot.tree.command(name="play", description="URLまたは検索ワードで音楽を再生・キューに追加します")
@app_commands.describe(search="YouTubeのURLまたは検索キーワード")
async def play(interaction: discord.Interaction, search: str):
    if not interaction.guild.voice_client:
        if interaction.user.voice:
            await interaction.user.voice.channel.connect()
        else:
            return await interaction.response.send_message("先にボイスチャンネルに入ってください。", ephemeral=True)

    await interaction.response.defer()

    state = get_state(interaction.guild.id)
    loop = asyncio.get_event_loop()
    try:
        data = await loop.run_in_executor(None, lambda: ytdl.extract_info(search, download=False))
        if 'entries' in data:
            data = data['entries'][0]
        
        song = {
            'title': data.get('title', '不明なタイトル'),
            'webpage_url': data.get('webpage_url')
        }
        state['queue'].append(song)

        vc = interaction.guild.voice_client
        if not vc.is_playing() and not vc.is_paused():
            await interaction.followup.send(f"🔍 読み込み完了: **{song['title']}**")
            await play_next(interaction.guild, interaction.channel)
        else:
            await interaction.followup.send(f"➕ キューに追加しました: **{song['title']}**")
    except Exception as e:
        await interaction.followup.send(f"エラーが発生しました: {e}")

@bot.tree.command(name="pause", description="再生を一時停止します")
async def pause(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.pause()
        await interaction.response.send_message("⏸️ 一時停止しました。")
    else:
        await interaction.response.send_message("再生中の曲はありません。", ephemeral=True)

@bot.tree.command(name="resume", description="一時停止中の曲を再開します")
async def resume(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_paused():
        vc.resume()
        await interaction.response.send_message("▶️ 再生を再開しました。")
    else:
        await interaction.response.send_message("一時停止中の曲はありません。", ephemeral=True)

@bot.tree.command(name="stop", description="再生を停止します")
async def stop(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        state = get_state(interaction.guild.id)
        state['loop'] = False
        vc.stop()
        await interaction.response.send_message("⏹️ 再生を停止しました。")
    else:
        await interaction.response.send_message("再生中の曲はありません。", ephemeral=True)

@bot.tree.command(name="skip", description="現在再生中の曲をスキップします")
async def skip(interaction: discord.Interaction):
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
        await interaction.response.send_message("⏭️ スキップしました。")
    else:
        await interaction.response.send_message("スキップできる曲がありません。", ephemeral=True)

@bot.tree.command(name="skipto", description="指定したキュー番号の曲までスキップします")
@app_commands.describe(index="スキップ先のキュー番号")
async def skipto(interaction: discord.Interaction, index: int):
    state = get_state(interaction.guild.id)
    if index < 1 or index > len(state['queue']):
        return await interaction.response.send_message("有効なキュー番号を指定してください。", ephemeral=True)
    state['queue'] = state['queue'][index-1:]
    vc = interaction.guild.voice_client
    if vc and vc.is_playing():
        vc.stop()
    await interaction.response.send_message(f"⏭️ {index}番目の曲までスキップしました。")

@bot.tree.command(name="replay", description="現在の曲を最初から再生し直します")
async def replay(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if state['current']:
        state['queue'].insert(0, state['current'])
        vc = interaction.guild.voice_client
        if vc and vc.is_playing():
            vc.stop()
        await interaction.response.send_message("🔄 現在の曲を最初から再生します。")
    else:
        await interaction.response.send_message("再生中の曲がありません。", ephemeral=True)

@bot.tree.command(name="queue", description="現在の再生待ちキューを表示します")
async def queue(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if not state['current'] and not state['queue']:
        return await interaction.response.send_message("キューは空です。")
    
    msg = ""
    if state['current']:
        msg += f"**現在再生中:** {state['current']['title']}\n\n"
    if state['queue']:
        msg += "**再生待ちリスト:**\n"
        for i, song in enumerate(state['queue'][:15], 1):
            msg += f"`{i}.` {song['title']}\n"
    await interaction.response.send_message(msg)

@bot.tree.command(name="nowplaying", description="現在再生中の曲名を表示します")
async def nowplaying(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if state['current']:
        await interaction.response.send_message(f"🎵 現在再生中: **{state['current']['title']}**")
    else:
        await interaction.response.send_message("現在再生中の曲はありません。", ephemeral=True)

@bot.tree.command(name="remove", description="キューから指定番号の曲を削除します")
@app_commands.describe(index="削除するキュー番号")
async def remove(interaction: discord.Interaction, index: int):
    state = get_state(interaction.guild.id)
    if 1 <= index <= len(state['queue']):
        removed = state['queue'].pop(index - 1)
        await interaction.response.send_message(f"🗑️ キューから削除しました: **{removed['title']}**")
    else:
        await interaction.response.send_message("無効な番号です。", ephemeral=True)

@bot.tree.command(name="clear", description="再生待ちキューをすべて空にします")
async def clear(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state['queue'].clear()
    await interaction.response.send_message("🧹 キューをすべて空にしました。")

@bot.tree.command(name="shuffle", description="キュー内の曲をランダムにシャッフルします")
async def shuffle(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    if len(state['queue']) > 1:
        random.shuffle(state['queue'])
        await interaction.response.send_message("🔀 キューをシャッフルしました。")
    else:
        await interaction.response.send_message("シャッフルする曲が足りません。", ephemeral=True)

@bot.tree.command(name="move", description="キュー内の曲の順番を入れ替えます")
@app_commands.describe(from_idx="移動元の番号", to_idx="移動先の番号")
async def move(interaction: discord.Interaction, from_idx: int, to_idx: int):
    state = get_state(interaction.guild.id)
    q_len = len(state['queue'])
    if 1 <= from_idx <= q_len and 1 <= to_idx <= q_len:
        song = state['queue'].pop(from_idx - 1)
        state['queue'].insert(to_idx - 1, song)
        await interaction.response.send_message(f"↕️ **{song['title']}** を {to_idx} 番目に移動しました。")
    else:
        await interaction.response.send_message("有効な番号を指定してください。", ephemeral=True)

@bot.tree.command(name="loop", description="1曲リピートのON/OFFを切り替えます")
async def loop_cmd(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state['loop'] = not state['loop']
    status = "ON" if state['loop'] else "OFF"
    await interaction.response.send_message(f"🔂 1曲リピートを **{status}** にしました。")

@bot.tree.command(name="loopqueue", description="キュー全体リピートのON/OFFを切り替えます")
async def loopqueue_cmd(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state['loop_queue'] = not state['loop_queue']
    status = "ON" if state['loop_queue'] else "OFF"
    await interaction.response.send_message(f"🔁 キュー全体リピートを **{status}** にしました。")

@bot.tree.command(name="volume", description="音量を1〜100%の間で変更します")
@app_commands.describe(vol="音量（1〜100）")
async def volume(interaction: discord.Interaction, vol: app_commands.Range[int, 1, 100]):
    state = get_state(interaction.guild.id)
    state['volume'] = vol / 100.0
    vc = interaction.guild.voice_client
    if vc and vc.source:
        vc.source.volume = state['volume']
    await interaction.response.send_message(f"🔊 音量を **{vol}%** に設定しました。")

@bot.tree.command(name="nightcore", description="ナイトコアエフェクトのON/OFFを切り替えます")
async def nightcore(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state['nightcore'] = not state['nightcore']
    status = "ON" if state['nightcore'] else "OFF"
    await interaction.response.send_message(f"⚡ ナイトコアモードを **{status}** にしました。")

@bot.tree.command(name="bassboost", description="重低音ブーストエフェクトのON/OFFを切り替えます")
async def bassboost(interaction: discord.Interaction):
    state = get_state(interaction.guild.id)
    state['bassboost'] = not state['bassboost']
    status = "ON" if state['bassboost'] else "OFF"
    await interaction.response.send_message(f"🎸 重低音ブーストを **{status}** にしました。")

@bot.tree.command(name="tts", description="このチャンネルのテキスト読み上げ（TTS）のON/OFFを切り替えます")
async def tts_toggle(interaction: discord.Interaction):
    if not interaction.guild.voice_client:
        if interaction.user.voice:
            await interaction.user.voice.channel.connect()
        else:
            return await interaction.response.send_message("先にボイスチャンネルに入ってください。", ephemeral=True)
    
    state = get_state(interaction.guild.id)
    state['tts_enabled'] = not state['tts_enabled']
    if state['tts_enabled']:
        state['tts_channel'] = interaction.channel.id
        await interaction.response.send_message(f"🗣️ このチャンネルの読み上げを **ON** にしました。")
    else:
        state['tts_channel'] = None
        await interaction.response.send_message("🤐 読み上げを **OFF** にしました。")

@bot.tree.command(name="panel", description="いつでも操作できる音楽コントロールパネルを表示します")
async def panel(interaction: discord.Interaction):
    view = MusicControls()
    await interaction.response.send_message("🎛️ **音楽コントローラーパネル**", view=view)

# --- 起動処理 ---
async def main():
    await start_web_server()
    token = os.environ.get("DISCORD_TOKEN")
    if not token:
        print("エラー: 環境変数 DISCORD_TOKEN が設定されていません。")
        return
    await bot.start(token)

if __name__ == "__main__":
    asyncio.run(main())
