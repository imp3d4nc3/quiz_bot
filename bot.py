import os
import asyncio
from typing import Dict, Set, List, Optional
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View
from dotenv import load_dotenv

# 環境変数の読み込み
load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
GUILD_ID = os.getenv("GUILD_ID")

# Botのインテント設定
intents = discord.Intents.default()
intents.message_content = True  # DMやメッセージ取得に必要
intents.members = True          # メンバーリスト取得に必要

bot = commands.Bot(command_prefix="!", intents=intents)

# グローバルなセッション管理
# key: channel_id -> QuizSession
active_sessions: Dict[int, "QuizSession"] = {}

# 出題者のDM入力状態管理
# key: user_id -> DMState
host_dm_states: Dict[int, "DMState"] = {}


class DMState:
    """出題者のDMでの問題登録プロセスを管理するクラス"""
    def __init__(self, session_channel_id: int):
        self.session_channel_id = session_channel_id
        self.state: str = "WAITING_QUESTION_TEXT"  # WAITING_QUESTION_TEXT, WAITING_CHOICES, WAITING_CORRECT_IDX, WAITING_POINTS
        self.question_text: str = ""
        self.choices: List[str] = []
        self.correct_idx: int = -1
        self.points: int = 1


class QuizSession:
    """クイズセッションの状態を管理するクラス"""
    def __init__(self, guild_id: int, channel_id: int, host: discord.Member, total_questions: int):
        self.guild_id: int = guild_id
        self.channel_id: int = channel_id
        self.host: discord.Member = host
        self.total_questions: int = total_questions
        self.current_question_index: int = 1
        
        self.state: str = "JOINING"  # JOINING, WAITING_QUESTION_DM, ANSWERING, ENDED
        self.participants: Set[int] = set()  # 参加登録したユーザーID
        self.scores: Dict[int, int] = {}     # ユーザーID -> 総獲得ポイント (ポイント部門用)
        self.correct_answers_count: Dict[int, int] = {} # ユーザーID -> 正解数 (正答率部門用)
        
        # ウルトシステム管理用データ
        self.user_ult_allowance: Dict[int, int] = {}  # ユーザーID -> 使用可能ウルト数（初期値は2）
        self.user_ult_used_count: Dict[int, int] = {} # ユーザーID -> 使用したウルト数（初期値は0）
        self.active_ult_this_turn: Set[int] = set()   # 現在の問題でウルトを発動したプレイヤーID
        self.ult_bonus_awarded: bool = False          # 救済ウルトを既に付与したかどうかのフラグ
        
        # 現在の問題データ
        self.current_question_text: str = ""
        self.current_choices: List[str] = []
        self.current_correct_idx: int = -1  # 1-based index
        self.current_points: int = 1         # 配点
        self.answers: Dict[int, int] = {}    # ユーザーID -> 選択肢番号(1-based)
        
        self.join_message: Optional[discord.Message] = None
        self.question_message: Optional[discord.Message] = None

    def add_participant(self, user_id: int) -> bool:
        if user_id == self.host.id:
            return False  # 出題者は回答に参加できない
        if user_id not in self.participants:
            self.participants.add(user_id)
            if user_id not in self.scores:
                self.scores[user_id] = 0
            if user_id not in self.correct_answers_count:
                self.correct_answers_count[user_id] = 0
            
            # ウルト初期回数を2に設定
            self.user_ult_allowance[user_id] = 2
            self.user_ult_used_count[user_id] = 0
            return True
        return False

    def remove_participant(self, user_id: int) -> bool:
        if user_id in self.participants:
            self.participants.remove(user_id)
            if user_id in self.scores:
                del self.scores[user_id]
            if user_id in self.correct_answers_count:
                del self.correct_answers_count[user_id]
            if user_id in self.user_ult_allowance:
                del self.user_ult_allowance[user_id]
            if user_id in self.user_ult_used_count:
                del self.user_ult_used_count[user_id]
            return True
        return False

    def award_worst_second_ult(self) -> List[int]:
        """ワースト2位のプレイヤーにウルトを1回分追加付与する。付与されたプレイヤーのIDリストを返す。"""
        if len(self.scores) < 2:
            return []

        # スコアのユニークな値を昇順でソート
        unique_scores = sorted(list(set(self.scores.values())))
        
        if len(unique_scores) < 2:
            # スコアのユニーク値が1つ（全員同点）の場合は、ワースト2位が定義できないため付与しない
            return []
        
        # 下から2番目のスコア
        worst_second_score = unique_scores[1]
        
        awarded_users = []
        for uid, score in self.scores.items():
            if score == worst_second_score:
                self.user_ult_allowance[uid] = self.user_ult_allowance.get(uid, 2) + 1
                awarded_users.append(uid)
                
        return awarded_users


# ==================== UI Views ====================

class QuizJoinView(View):
    """参加表明フェーズのボタンUI"""
    def __init__(self, session: QuizSession):
        super().__init__(timeout=None)
        self.session = session

    @discord.ui.button(label="参加する", style=discord.ButtonStyle.green, custom_id="quiz_join")
    async def join_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id == self.session.host.id:
            await interaction.response.send_message("出題者はクイズに参加できません。", ephemeral=True)
            return

        added = self.session.add_participant(interaction.user.id)
        if added:
            await interaction.response.send_message("クイズに参加登録しました！\n（ウルト使用権: 初期2回）", ephemeral=True)
            await self.update_join_message()
        else:
            await interaction.response.send_message("すでに参加登録しています。", ephemeral=True)

    @discord.ui.button(label="参加を取り消す", style=discord.ButtonStyle.grey, custom_id="quiz_leave")
    async def leave_button(self, interaction: discord.Interaction, button: Button):
        removed = self.session.remove_participant(interaction.user.id)
        if removed:
            await interaction.response.send_message("参加を取り消しました。", ephemeral=True)
            await self.update_join_message()
        else:
            await interaction.response.send_message("参加登録していません。", ephemeral=True)

    @discord.ui.button(label="参加受付を締め切る (出題者用)", style=discord.ButtonStyle.red, custom_id="quiz_close_join")
    async def close_join_button(self, interaction: discord.Interaction, button: Button):
        if interaction.user.id != self.session.host.id:
            await interaction.response.send_message("参加受付を締め切ることができるのは出題者のみです。", ephemeral=True)
            return

        if len(self.session.participants) == 0:
            await interaction.response.send_message("参加者がいないため、締め切ることができません。", ephemeral=True)
            return

        # 受付終了
        self.session.state = "WAITING_QUESTION_DM"
        
        # ボタンを無効化
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(view=self)

        # チャンネルに通知
        channel = bot.get_channel(self.session.channel_id)
        participant_mentions = ", ".join([f"<@{uid}>" for uid in self.session.participants])
        await channel.send(
            f"✅ 参加受付を締め切りました！\n"
            f"**参加者:** {participant_mentions}\n\n"
            f"出題者の <@{self.session.host.id}> さん、DMで第1問目の登録をお願いします。"
        )

        # 出題者にDMを送信
        await send_dm_prompt(self.session.host, self.session)

    async def update_join_message(self):
        if not self.session.join_message:
            return

        participants_list = "\n".join([f"- <@{uid}> (ウルト: 2/2)" for uid in self.session.participants])
        if not participants_list:
            participants_list = "なし"

        embed = discord.Embed(
            title="🎮 クイズ大会 参加受付中！",
            description=f"出題者: <@{self.session.host.id}>\n目標問題数: {self.session.total_questions}問\n\n**【ウルトシステム】**\nクイズ中、正解時の配点が2倍になるウルトを各自初期で2回使えます！",
            color=discord.Color.blue()
        )
        embed.add_field(name=f"現在の参加者 ({len(self.session.participants)}人)", value=participants_list, inline=False)
        embed.set_footer(text="参加する方は下のボタンを押してください。")

        try:
            await self.session.join_message.edit(embed=embed, view=self)
        except Exception as e:
            print(f"Error updating join message: {e}")


class QuizQuestionView(View):
    """出題フェーズのボタンUI"""
    def __init__(self, session: QuizSession):
        super().__init__(timeout=None)
        self.session = session
        
        # 選択肢ボタンを動的に追加
        for i in range(len(session.current_choices)):
            num = i + 1
            button = Button(
                label=str(num),
                style=discord.ButtonStyle.primary,
                custom_id=f"quiz_choice_{num}"
            )
            button.callback = self.make_callback(num)
            self.add_item(button)

        # ⚡ウルト発動ボタンを追加
        ult_button = Button(
            label="⚡ウルトを発動",
            style=discord.ButtonStyle.success,
            custom_id="quiz_use_ult",
            row=1
        )
        ult_button.callback = self.use_ult_callback
        self.add_item(ult_button)

        # 出題者用の強制締め切りボタンを追加
        close_button = Button(
            label="回答を締め切る (出題者用)",
            style=discord.ButtonStyle.danger,
            custom_id="quiz_force_close",
            row=1
        )
        close_button.callback = self.force_close_callback
        self.add_item(close_button)

    def make_callback(self, choice_num: int):
        async def callback(interaction: discord.Interaction):
            user_id = interaction.user.id
            
            # 参加登録者かチェック
            if user_id not in self.session.participants:
                await interaction.response.send_message("あなたはクイズに参加登録していません。", ephemeral=True)
                return

            # すでに回答済みかチェック
            if user_id in self.session.answers:
                await interaction.response.send_message("すでに回答を送信しています。変更はできません。", ephemeral=True)
                return

            # 回答を記録
            self.session.answers[user_id] = choice_num
            
            # ウルト発動ステータスを含むテキスト
            ult_active_text = " (⚡ウルト適用中！)" if user_id in self.session.active_ult_this_turn else ""
            await interaction.response.send_message(f"選択肢 {choice_num} を選択しました！{ult_active_text}", ephemeral=True)
            
            # 回答状況表示を更新
            await self.update_question_message()

            # 全員が回答したら自動締め切り
            if len(self.session.answers) == len(self.session.participants):
                await self.reveal_answers()

        return callback

    async def use_ult_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        # 参加登録者かチェック
        if user_id not in self.session.participants:
            await interaction.response.send_message("あなたはクイズに参加登録していません。", ephemeral=True)
            return

        # すでに回答済みかチェック（回答送信後のウルト発動は不可）
        if user_id in self.session.answers:
            await interaction.response.send_message("すでに回答を送信したため、この問題ではウルトを発動できません。", ephemeral=True)
            return

        # すでにこの問題で発動済みかチェック
        if user_id in self.session.active_ult_this_turn:
            await interaction.response.send_message("すでにこの問題でウルトを発動しています。", ephemeral=True)
            return

        # ウルトの使用回数制限チェック
        allowance = self.session.user_ult_allowance.get(user_id, 2)
        used = self.session.user_ult_used_count.get(user_id, 0)
        if used >= allowance:
            await interaction.response.send_message(
                f"ウルトの使用可能回数がありません。（使用済み: {used}/{allowance}回）", 
                ephemeral=True
            )
            return

        # ウルトの発動処理
        self.session.active_ult_this_turn.add(user_id)
        self.session.user_ult_used_count[user_id] = used + 1
        
        rem = allowance - (used + 1)
        await interaction.response.send_message(
            f"⚡ ウルトを発動しました！この問題で正解すると得点が2倍になります。\n（残りウルト: {rem}/{allowance}回）", 
            ephemeral=True
        )

        # チャンネルに発動をアナウンス
        channel = bot.get_channel(self.session.channel_id)
        await channel.send(f"⚡ **<@{user_id}> がウルトを発動した！正解なら得点が2倍になります！**")

        # 表示更新
        await self.update_question_message()

    async def force_close_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.session.host.id:
            await interaction.response.send_message("回答を締め切ることができるのは出題者のみです。", ephemeral=True)
            return

        if len(self.session.answers) == 0:
            await interaction.response.send_message("まだ誰も回答していないため、締め切ることはできません。", ephemeral=True)
            return

        await interaction.response.send_message("回答を締め切りました。", ephemeral=True)
        await self.reveal_answers()

    async def update_question_message(self):
        if not self.session.question_message:
            return

        # 回答した人の一覧（ウルト使用者は名前の横にボルトマークを表示）
        answered_mentions = []
        unanswered_mentions = []
        for uid in self.session.participants:
            ult_mark = " ⚡" if uid in self.session.active_ult_this_turn else ""
            
            if uid in self.session.answers:
                answered_mentions.append(f"<@{uid}>{ult_mark}")
            else:
                unanswered_mentions.append(f"<@{uid}>{ult_mark}")

        answered_text = ", ".join(answered_mentions) if answered_mentions else "なし"
        unanswered_text = ", ".join(unanswered_mentions) if unanswered_mentions else "なし"

        choices_text = "\n".join([f"**{i+1}.** {choice}" for i, choice in enumerate(self.session.current_choices)])

        # ウルトの残り回数リストを作成
        ult_status_lines = []
        for uid in self.session.participants:
            allowance = self.session.user_ult_allowance.get(uid, 2)
            used = self.session.user_ult_used_count.get(uid, 0)
            rem = max(0, allowance - used)
            ult_status_lines.append(f"<@{uid}>: 残り{rem}/{allowance}回")
        ult_status_text = ", ".join(ult_status_lines)

        embed = discord.Embed(
            title=f"❓ 第 {self.session.current_question_index} 問 / 全 {self.session.total_questions} 問 (配点: {self.session.current_points}点)",
            description=f"**出題者:** <@{self.session.host.id}>\n\n**問題:**\n{self.session.current_question_text}\n\n**選択肢:**\n{choices_text}",
            color=discord.Color.orange()
        )
        embed.add_field(name=f"回答済み ({len(self.session.answers)} / {len(self.session.participants)}人)", value=answered_text, inline=False)
        if unanswered_mentions:
            embed.add_field(name="未回答", value=unanswered_text, inline=False)
        
        embed.add_field(name="⚡ ウルト残り回数", value=ult_status_text, inline=False)
        embed.set_footer(text="自分が正しいと思う番号のボタンを押してください。回答前にウルトを発動することもできます。")

        try:
            await self.session.question_message.edit(embed=embed, view=self)
        except Exception as e:
            print(f"Error updating question message: {e}")

    async def reveal_answers(self):
        # ボタンをすべて無効化
        for child in self.children:
            child.disabled = True
        
        try:
            await self.session.question_message.edit(view=self)
        except Exception as e:
            print(f"Error disabling buttons: {e}")

        correct_idx = self.session.current_correct_idx
        correct_choice_text = self.session.current_choices[correct_idx - 1]
        base_points = self.session.current_points
        
        correct_users_text = []
        
        for uid, ans_idx in self.session.answers.items():
            if ans_idx == correct_idx:
                points_gained = base_points
                ult_text = ""
                
                # ウルト発動時の処理
                if uid in self.session.active_ult_this_turn:
                    points_gained *= 2
                    ult_text = " (⚡ウルト適用: 2倍!)"
                    
                self.session.scores[uid] += points_gained
                self.session.correct_answers_count[uid] = self.session.correct_answers_count.get(uid, 0) + 1
                correct_users_text.append(f"<@{uid}> (+{points_gained}点){ult_text}")

        # 正解者リスト作成
        correct_mentions = ", ".join(correct_users_text) if correct_users_text else "なし"

        # 中間順位の組み立て（ポイント順）
        scores_sorted = sorted(self.session.scores.items(), key=lambda x: x[1], reverse=True)
        scores_text = "\n".join([f"🏆 <@{uid}>: {score}点" for uid, score in scores_sorted])

        channel = bot.get_channel(self.session.channel_id)
        
        reveal_embed = discord.Embed(
            title=f"📢 第 {self.session.current_question_index} 問 正解発表！",
            description=f"正解は... **{correct_idx}. {correct_choice_text}** でした！ 🎉",
            color=discord.Color.green()
        )
        reveal_embed.add_field(name="⭕ 正解者", value=correct_mentions, inline=False)
        reveal_embed.add_field(name="📊 現在のスコア（ポイント順）", value=scores_text, inline=False)

        await channel.send(embed=reveal_embed)

        # ターンで使用したウルト状態をクリア
        self.session.active_ult_this_turn.clear()

        # 5秒待機してから次のステップへ
        await asyncio.sleep(5)

        # 次の問題番号へ進める
        self.session.current_question_index += 1
        if self.session.current_question_index > self.session.total_questions:
            # クイズ終了、最終ランキング発表
            await self.end_quiz()
        else:
            # 救済ウルトの付与判定
            remaining_questions = self.session.total_questions - self.session.current_question_index + 1
            remaining_ratio = remaining_questions / self.session.total_questions
            
            # 残り問題数が25%以下になったら、一度だけワースト2位に付与
            if not self.session.ult_bonus_awarded and remaining_ratio <= 0.25:
                awarded_uids = self.session.award_worst_second_ult()
                if awarded_uids:
                    mentions_str = ", ".join([f"<@{uid}>" for uid in awarded_uids])
                    
                    bonus_embed = discord.Embed(
                        title="📢 【救済措置】ウルト追加付与！",
                        description=(
                            f"残り問題数が全体の25%以下になりました（残り {remaining_questions} 問）。\n"
                            f"現在ワースト2位の {mentions_str} さんに、救済アイテムとして **ウルト使用権が1回分追加** されました！ ⚡"
                        ),
                        color=discord.Color.purple()
                    )
                    await channel.send(embed=bonus_embed)
                self.session.ult_bonus_awarded = True

            # 次の問題をDMで募集
            self.session.state = "WAITING_QUESTION_DM"
            self.session.answers.clear()
            
            await channel.send(f"次は第 {self.session.current_question_index} 問です。出題者の <@{self.session.host.id}> さん、DMで次の問題の登録をお願いします。")
            await send_dm_prompt(self.session.host, self.session)

    async def end_quiz(self):
        self.session.state = "ENDED"
        channel = bot.get_channel(self.session.channel_id)

        # 1. ポイント部門の集計とランキング表示
        scores_sorted = sorted(self.session.scores.items(), key=lambda x: x[1], reverse=True)
        
        points_ranking_lines = []
        rank = 1
        prev_score = -1
        for i, (uid, score) in enumerate(scores_sorted):
            if score != prev_score:
                rank = i + 1
            prev_score = score
            
            medal = "🏆" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🎖️"
            points_ranking_lines.append(f"{medal} **{rank}位**: <@{uid}> ({score}点)")

        points_ranking_text = "\n".join(points_ranking_lines) if points_ranking_lines else "参加者がいませんでした。"

        # 2. 正答率部門の集計とランキング表示
        corrects_sorted = sorted(self.session.correct_answers_count.items(), key=lambda x: x[1], reverse=True)
        
        accuracy_ranking_lines = []
        rank = 1
        prev_correct = -1
        total_q = self.session.total_questions
        for i, (uid, correct_count) in enumerate(corrects_sorted):
            if correct_count != prev_correct:
                rank = i + 1
            prev_correct = correct_count
            
            accuracy = (correct_count / total_q * 100) if total_q > 0 else 0.0
            medal = "🏆" if rank == 1 else "🥈" if rank == 2 else "🥉" if rank == 3 else "🎖️"
            accuracy_ranking_lines.append(f"{medal} **{rank}位**: <@{uid}> (正答率: {accuracy:.1f}% - {total_q}問中{correct_count}問正解)")

        accuracy_ranking_text = "\n".join(accuracy_ranking_lines) if accuracy_ranking_lines else "参加者がいませんでした。"

        end_embed = discord.Embed(
            title="🏁 クイズ大会 終了！",
            description="全問題が終了しました！皆様お疲れ様でした。部門別の最終順位は以下の通りです！",
            color=discord.Color.gold()
        )
        end_embed.add_field(name="📊 ポイント部門 (配点・ウルト考慮)", value=points_ranking_text, inline=False)
        end_embed.add_field(name="🎯 正答率部門 (純粋な正答数)", value=accuracy_ranking_text, inline=False)

        await channel.send(embed=end_embed)

        # セッションのクリーンアップ
        if self.session.channel_id in active_sessions:
            del active_sessions[self.session.channel_id]


# ==================== ユーティリティ関数 ====================

async def send_dm_prompt(user: discord.User, session: QuizSession):
    """出題者にDMで次の問題の入力を促す"""
    try:
        # DMStateを初期化または取得
        host_dm_states[user.id] = DMState(session.channel_id)
        
        await user.send(
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🎮 **第 {session.current_question_index} 問目の登録を開始します**\n"
            f"まずは **【問題文】** を入力して送信してください。\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
        )
    except discord.Forbidden:
        # DMが送れない場合、チャンネルにエラーメッセージを出す
        channel = bot.get_channel(session.channel_id)
        await channel.send(
            f"⚠️ <@{user.id}> さんへのDMの送信に失敗しました。\n"
            f"DMの受信拒否設定を解除するか、サーバーのメンバーからのDMを許可してください。\n"
            f"クイズセッションを中止します。"
        )
        if session.channel_id in active_sessions:
            del active_sessions[session.channel_id]


# ==================== イベントハンドラー ====================

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} ({bot.user.id})")
    try:
        # スラッシュコマンドを同期 (GUILD_IDが指定されている場合はギルド限定で即時反映)
        if GUILD_ID and GUILD_ID != "YOUR_GUILD_ID_HERE":
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"Synced {len(synced)} command(s) to Guild ID: {GUILD_ID} (Local)")
        else:
            synced = await bot.tree.sync()
            print(f"Synced {len(synced)} global command(s) (Global command sync might take up to an hour)")
    except Exception as e:
        print(f"Failed to sync commands: {e}")


@bot.event
async def on_message(message: discord.Message):
    # Bot自身のメッセージは無視
    if message.author.bot:
        return

    # DM（プライベートメッセージ）の処理
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        
        # ユーザーがクイズの出題状態にあるか確認
        if user_id in host_dm_states:
            dm_state = host_dm_states[user_id]
            session = active_sessions.get(dm_state.session_channel_id)
            
            # セッションが無効か、状態が不一致なら無視
            if not session or session.state != "WAITING_QUESTION_DM" or session.host.id != user_id:
                del host_dm_states[user_id]
                await message.channel.send("現在有効なクイズセッションの出題者ではありません。")
                return

            await handle_host_dm_input(message, dm_state, session)
            return

    # ギルド内メッセージならコマンド等を処理
    await bot.process_commands(message)


async def handle_host_dm_input(message: discord.Message, dm_state: DMState, session: QuizSession):
    """出題者からのDM入力を状態遷移に基づいて処理する"""
    content = message.content.strip()

    if dm_state.state == "WAITING_QUESTION_TEXT":
        if not content:
            await message.channel.send("問題文を入力してください。空の入力は受け付けられません。")
            return
        
        dm_state.question_text = content
        dm_state.state = "WAITING_CHOICES"
        await message.channel.send(
            f"問題文を登録しました：\n> {content}\n\n"
            f"次に、選択肢を **半角カンマ (,)** で区切って入力して送信してください。\n"
            f"例: `りんご, みかん, ぶどう, ばなな`"
        )

    elif dm_state.state == "WAITING_CHOICES":
        # 選択肢をパース
        choices = [c.strip() for c in content.split(",") if c.strip()]
        
        if len(choices) < 2:
            await message.channel.send("選択肢は最低でも 2 つ以上入力してください。")
            return
        if len(choices) > 25:
            await message.channel.send("選択肢は最大 25 個までです（Discordの仕様上）。再度入力してください。")
            return

        dm_state.choices = choices
        dm_state.state = "WAITING_CORRECT_IDX"
        
        choices_preview = "\n".join([f"{i+1}. {choice}" for i, choice in enumerate(choices)])
        await message.channel.send(
            f"選択肢を登録しました：\n{choices_preview}\n\n"
            f"次に、正解の番号を **1 から {len(choices)} の数字** で入力して送信してください。"
        )

    elif dm_state.state == "WAITING_CORRECT_IDX":
        # 正解番号のバリデーション
        try:
            val = int(content)
            if val < 1 or val > len(dm_state.choices):
                raise ValueError
        except ValueError:
            await message.channel.send(f"正解番号は 1 から {len(dm_state.choices)} の範囲の半角数字で入力してください。")
            return

        dm_state.correct_idx = val
        dm_state.state = "WAITING_POINTS"
        await message.channel.send(
            f"正解番号を登録しました：**{val}. {dm_state.choices[val-1]}**\n\n"
            f"最後に、この問題の **配点（得点）を半角数字（1以上）** で入力して送信してください。"
        )

    elif dm_state.state == "WAITING_POINTS":
        # 配点のバリデーション
        try:
            val = int(content)
            if val <= 0:
                raise ValueError
        except ValueError:
            await message.channel.send("配点は 1 以上の半角数字で入力してください。")
            return

        dm_state.points = val
        
        # セッションに問題情報を保存
        session.current_question_text = dm_state.question_text
        session.current_choices = dm_state.choices
        session.current_correct_idx = dm_state.correct_idx
        session.current_points = dm_state.points
        
        await message.channel.send(f"🎉 問題の登録が完了しました！（配点: {val}点）\nチャンネルに出題します。")

        # DM状態のクリア
        del host_dm_states[message.author.id]

        # チャンネルへ出題
        await post_question_to_channel(session)


async def post_question_to_channel(session: QuizSession):
    """登録された問題をチャンネルに投稿する"""
    channel = bot.get_channel(session.channel_id)
    if not channel:
        print(f"Error: Channel {session.channel_id} not found.")
        return

    session.state = "ANSWERING"

    # 選択肢の表示用テキスト
    choices_text = "\n".join([f"**{i+1}.** {choice}" for i, choice in enumerate(session.current_choices)])

    # 初期表示用の未回答者リスト
    unanswered_mentions = ", ".join([f"<@{uid}>" for uid in session.participants])

    # ウルトの残り回数リスト
    ult_status_lines = []
    for uid in session.participants:
        allowance = session.user_ult_allowance.get(uid, 2)
        used = session.user_ult_used_count.get(uid, 0)
        rem = max(0, allowance - used)
        ult_status_lines.append(f"<@{uid}>: 残り{rem}/{allowance}回")
    ult_status_text = ", ".join(ult_status_lines)

    embed = discord.Embed(
        title=f"❓ 第 {session.current_question_index} 問 / 全 {session.total_questions} 問 (配点: {session.current_points}点)",
        description=f"**出題者:** <@{session.host.id}>\n\n**問題:**\n{session.current_question_text}\n\n**選択肢:**\n{choices_text}",
        color=discord.Color.orange()
    )
    embed.add_field(name=f"回答済み (0 / {len(session.participants)}人)", value="なし", inline=False)
    embed.add_field(name="未回答", value=unanswered_mentions, inline=False)
    embed.add_field(name="⚡ ウルト残り回数", value=ult_status_text, inline=False)
    embed.set_footer(text="自分が正しいと思う番号 of ボタンを押してください。回答前にウルトを発動することもできます。")

    # 回答用ビューの作成と送信
    view = QuizQuestionView(session)
    msg = await channel.send(embed=embed, view=view)
    session.question_message = msg


# ==================== スラッシュコマンド ====================

@bot.tree.command(name="quiz_start", description="クイズ大会を開始します。参加受付を開始します。")
@app_commands.describe(
    host="問題を出題するメンバーを指定します",
    questions="合計の問題数を指定します（1以上）",
    channel="クイズを行うチャンネルを指定します（指定しない場合は現在のチャンネル）"
)
async def quiz_start(
    interaction: discord.Interaction,
    host: discord.Member,
    questions: int,
    channel: Optional[discord.TextChannel] = None
):
    target_channel = channel or interaction.channel
    
    # バリデーション
    if questions <= 0:
        await interaction.response.send_message("問題数は1問以上に設定してください。", ephemeral=True)
        return

    if target_channel.id in active_sessions:
        await interaction.response.send_message(
            f"このチャンネルではすでに別のクイズセッションが実行中です。\n"
            f"終了するか、別のチャンネルで開始してください。",
            ephemeral=True
        )
        return

    # セッションの初期化
    session = QuizSession(
        guild_id=interaction.guild_id,
        channel_id=target_channel.id,
        host=host,
        total_questions=questions
    )
    active_sessions[target_channel.id] = session

    # 参加受付の埋め込み作成
    embed = discord.Embed(
        title="🎮 クイズ大会 参加受付中！",
        description=f"出題者: <@{host.id}>\n目標問題数: {questions}問\n\n**【ウルトシステム】**\nクイズ中、正解時の配点が2倍になるウルトを各自初期で2回使えます！",
        color=discord.Color.blue()
    )
    embed.add_field(name="現在の参加者 (0人)", value="なし", inline=False)
    embed.set_footer(text="参加する方は下のボタンを押してください。")

    view = QuizJoinView(session)
    
    # まずインタラクションに応答する
    await interaction.response.send_message(f"<#{target_channel.id}> でクイズのセットアップを開始しました！", ephemeral=True)
    
    # クイズチャンネルに参加受付メッセージを送信
    msg = await target_channel.send(embed=embed, view=view)
    session.join_message = msg


@bot.tree.command(name="quiz_abort", description="実行中のクイズセッションを強制終了します（管理者または出題者用）")
async def quiz_abort(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id not in active_sessions:
        await interaction.response.send_message("このチャンネルで実行中のクイズセッションはありません。", ephemeral=True)
        return

    session = active_sessions[channel_id]
    
    # 管理者権限または出題者本人かチェック
    is_admin = interaction.user.guild_permissions.administrator
    if interaction.user.id != session.host.id and not is_admin:
        await interaction.response.send_message("クイズを中止できるのは、出題者またはサーバー管理者のみです。", ephemeral=True)
        return

    # セッション破棄
    del active_sessions[channel_id]
    
    # 出題者のDM入力状態も削除
    if session.host.id in host_dm_states:
        del host_dm_states[session.host.id]

    await interaction.response.send_message("🚨 クイズセッションが強制終了されました。")


if __name__ == "__main__":
    if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: DISCORD_TOKEN が設定されていません。.envファイルを確認してください。")
    else:
        bot.run(TOKEN)
