import os
import asyncio
from typing import Dict, Set, List, Optional, Union
import discord
from discord import app_commands
from discord.ext import commands
from discord.ui import Button, View, Modal, TextInput
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
        # ステート一覧:
        # WAITING_QUESTION_TEXT, WAITING_TYPE, WAITING_CHOICES, WAITING_CORRECT_IDX, WAITING_CORRECT_TEXT, WAITING_POINTS
        self.state: str = "WAITING_QUESTION_TEXT"
        self.question_text: str = ""
        self.question_type: str = "CHOICE"  # CHOICE or TEXT
        self.choices: List[str] = []
        self.correct_idx: int = -1
        self.correct_text: str = ""
        self.points: int = 1


class QuizSession:
    """クイズセッションの状態を管理するクラス"""
    def __init__(self, guild_id: int, channel_id: int, host: discord.Member, total_questions: int):
        self.guild_id: int = guild_id
        self.channel_id: int = channel_id
        self.host: discord.Member = host
        self.total_questions: int = total_questions
        self.current_question_index: int = 1
        
        self.state: str = "JOINING"  # JOINING, WAITING_QUESTION_DM, ANSWERING, GRADING, ENDED
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
        self.current_question_type: str = "CHOICE"   # CHOICE or TEXT
        self.current_choices: List[str] = []         # 選択肢用
        self.current_correct_idx: int = -1           # 選択肢用 (1-based index)
        self.current_correct_text: str = ""          # 記述式用 (模範解答)
        self.current_points: int = 1                 # 配点
        
        # 回答データ
        self.answers: Dict[int, Union[int, str]] = {} # ユーザーID -> 回答 (選択肢ならint, 記述ならstr)
        
        # 記述式の手動採点データ
        self.grading_queue: List[int] = []            # 採点対象のプレイヤーIDリスト
        self.current_grading_idx: int = 0             # 現在採点中のインデックス
        self.grading_results: Dict[int, bool] = {}    # ユーザーID -> 正否 (True=正解, False=不正解)
        self.grading_dm_message: Optional[discord.Message] = None # 出題者DMの採点メッセージオブジェクト
        
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


# ==================== UI Views & Modals ====================

class QuizTypeSelectView(View):
    """問題登録時に問題タイプを選択するDM用ボタンUI"""
    def __init__(self, user_id: int):
        super().__init__(timeout=300)
        self.user_id = user_id

    @discord.ui.button(label="🔢 選択肢式", style=discord.ButtonStyle.primary, custom_id="type_choice")
    async def type_choice_button(self, interaction: discord.Interaction, button: Button):
        dm_state = host_dm_states.get(self.user_id)
        if not dm_state:
            await interaction.response.send_message("セッションの有効期限が切れているか、無効です。", ephemeral=True)
            return

        dm_state.question_type = "CHOICE"
        dm_state.state = "WAITING_CHOICES"
        
        # メッセージを編集して次のステップを案内
        await interaction.response.edit_message(
            content="📝 **問題タイプに「🔢 選択肢式」を選択しました。**\n\n次に、選択肢を **半角カンマ (,)** で区切って入力して送信してください。\n例: `織田信長, 豊臣秀吉, 徳川家康`",
            view=None
        )

    @discord.ui.button(label="📝 記述式", style=discord.ButtonStyle.success, custom_id="type_text")
    async def type_text_button(self, interaction: discord.Interaction, button: Button):
        dm_state = host_dm_states.get(self.user_id)
        if not dm_state:
            await interaction.response.send_message("セッションの有効期限が切れているか、無効です。", ephemeral=True)
            return

        dm_state.question_type = "TEXT"
        dm_state.state = "WAITING_CORRECT_TEXT"

        # メッセージを編集して次のステップを案内
        await interaction.response.edit_message(
            content="📝 **問題タイプに「📝 記述式（手動採点）」を選択しました。**\n\n次に、この問題の **【模範解答】** を入力して送信してください。\n※正解発表時にチャンネルに表示されます。",
            view=None
        )


class QuizAnswerModal(Modal):
    """記述式問題の回答入力ポップアップ"""
    def __init__(self, session: QuizSession):
        super().__init__(title="📝 記述式問題 回答入力")
        self.session = session

        self.answer_input = TextInput(
            label="あなたの回答を記述してください",
            style=discord.TextStyle.short,
            placeholder="ここに入力...",
            max_length=100,
            required=True
        )
        self.add_item(self.answer_input)

    async def on_submit(self, interaction: discord.Interaction):
        user_id = interaction.user.id
        answer_text = self.answer_input.value.strip()

        if user_id not in self.session.participants:
            await interaction.response.send_message("あなたはクイズに参加登録していません。", ephemeral=True)
            return

        if user_id in self.session.answers:
            await interaction.response.send_message("すでに回答を送信しています。変更はできません。", ephemeral=True)
            return

        # 回答を記録
        self.session.answers[user_id] = answer_text
        
        # ウルト発動ステータスを含むテキスト
        ult_active_text = " (⚡ウルト適用中！)" if user_id in self.session.active_ult_this_turn else ""
        await interaction.response.send_message(f"回答「{answer_text}」を送信しました！{ult_active_text}", ephemeral=True)

        # チャンネル画面の回答状況表示を更新
        # 呼び出し元がViewなので、sessionに保存されているメッセージを編集する
        question_view = QuizQuestionView(self.session)
        await question_view.update_question_message()

        # 全員が回答したら自動で採点フェーズに進む
        if len(self.session.answers) == len(self.session.participants):
            await question_view.start_grading_phase()


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
        
        # 問題タイプに応じてボタン構成を変更
        if session.current_question_type == "CHOICE":
            # 選択肢ボタンを動的に追加
            for i in range(len(session.current_choices)):
                num = i + 1
                button = Button(
                    label=str(num),
                    style=discord.ButtonStyle.primary,
                    custom_id=f"quiz_choice_{num}"
                )
                button.callback = self.make_choice_callback(num)
                self.add_item(button)
        else:
            # 記述式用の「回答を記入する」ボタンを追加
            input_button = Button(
                label="📝 回答を記入する",
                style=discord.ButtonStyle.primary,
                custom_id="quiz_input_answer"
            )
            input_button.callback = self.input_answer_callback
            self.add_item(input_button)

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

    def make_choice_callback(self, choice_num: int):
        async def callback(interaction: discord.Interaction):
            user_id = interaction.user.id
            
            if user_id not in self.session.participants:
                await interaction.response.send_message("あなたはクイズに参加登録していません。", ephemeral=True)
                return

            if user_id in self.session.answers:
                await interaction.response.send_message("すでに回答を送信しています。変更はできません。", ephemeral=True)
                return

            # 回答を記録
            self.session.answers[user_id] = choice_num
            
            ult_active_text = " (⚡ウルト適用中！)" if user_id in self.session.active_ult_this_turn else ""
            await interaction.response.send_message(f"選択肢 {choice_num} を選択しました！{ult_active_text}", ephemeral=True)
            
            await self.update_question_message()

            # 全員が回答したら自動締め切り（選択肢式は直接正解発表へ）
            if len(self.session.answers) == len(self.session.participants):
                await self.reveal_answers()

        return callback

    async def input_answer_callback(self, interaction: discord.Interaction):
        """記述式問題でボタンが押された際にモーダルを開く"""
        user_id = interaction.user.id
        
        if user_id not in self.session.participants:
            await interaction.response.send_message("あなたはクイズに参加登録していません。", ephemeral=True)
            return

        if user_id in self.session.answers:
            await interaction.response.send_message("すでに回答を送信しています。変更はできません。", ephemeral=True)
            return

        # モーダルのポップアップを表示
        modal = QuizAnswerModal(self.session)
        await interaction.response.send_modal(modal)

    async def use_ult_callback(self, interaction: discord.Interaction):
        user_id = interaction.user.id

        if user_id not in self.session.participants:
            await interaction.response.send_message("あなたはクイズに参加登録していません。", ephemeral=True)
            return

        if user_id in self.session.answers:
            await interaction.response.send_message("すでに回答を送信したため、この問題ではウルトを発動できません。", ephemeral=True)
            return

        if user_id in self.session.active_ult_this_turn:
            await interaction.response.send_message("すでにこの問題でウルトを発動しています。", ephemeral=True)
            return

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

        channel = bot.get_channel(self.session.channel_id)
        await channel.send(f"⚡ **<@{user_id}> がウルトを発動した！正解なら得点が2倍になります！**")

        await self.update_question_message()

    async def force_close_callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.session.host.id:
            await interaction.response.send_message("回答を締め切ることができるのは出題者のみです。", ephemeral=True)
            return

        if len(self.session.answers) == 0:
            await interaction.response.send_message("まだ誰も回答していないため、締め切ることはできません。", ephemeral=True)
            return

        await interaction.response.send_message("回答を締め切りました。", ephemeral=True)

        if self.session.current_question_type == "CHOICE":
            await self.reveal_answers()
        else:
            await self.start_grading_phase()

    async def update_question_message(self):
        if not self.session.question_message:
            return

        # 回答した人の一覧
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

        # 問題文と説明の構築
        if self.session.current_question_type == "CHOICE":
            choices_text = "\n".join([f"**{i+1}.** {choice}" for i, choice in enumerate(self.session.current_choices)])
            question_desc = f"**問題:**\n{self.session.current_question_text}\n\n**選択肢:**\n{choices_text}"
            footer_text = "自分が正しいと思う番号のボタンを押してください。回答前にウルトを発動することもできます。"
        else:
            question_desc = f"**記述式問題:**\n{self.session.current_question_text}\n\n※ボタンを押して回答を入力してください（非公開入力）。"
            footer_text = "「回答を記入する」ボタンを押して回答を入力してください。回答前にウルトを発動することもできます。"

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
            description=f"**出題者:** <@{self.session.host.id}>\n\n{question_desc}",
            color=discord.Color.orange()
        )
        embed.add_field(name=f"回答済み ({len(self.session.answers)} / {len(self.session.participants)}人)", value=answered_text, inline=False)
        if unanswered_mentions:
            embed.add_field(name="未回答", value=unanswered_text, inline=False)
        
        embed.add_field(name="⚡ ウルト残り回数", value=ult_status_text, inline=False)
        embed.set_footer(text=footer_text)

        try:
            await self.session.question_message.edit(embed=embed, view=self)
        except Exception as e:
            print(f"Error updating question message: {e}")

    async def start_grading_phase(self):
        """記述式問題の回答を締め切り、出題者のDMで採点フェーズを開始する"""
        self.session.state = "GRADING"

        # チャンネルのボタンをすべて無効化
        for child in self.children:
            child.disabled = True
        try:
            await self.session.question_message.edit(view=self)
        except Exception as e:
            print(f"Error disabling buttons: {e}")

        channel = bot.get_channel(self.session.channel_id)
        await channel.send(f"📢 **全員の回答が揃いました（または締め切られました）。出題者の採点を待っています...**")

        # 採点キューの構築
        self.session.grading_queue = list(self.session.answers.keys())
        self.session.current_grading_idx = 0
        self.session.grading_results.clear()

        # 出題者にDMで採点プロンプトを送信
        await send_grading_dm_prompt(self.session)

    async def reveal_answers(self):
        """正解発表とスコア反映"""
        # ボタンをすべて無効化 (選択肢式用)
        for child in self.children:
            child.disabled = True
        try:
            await self.session.question_message.edit(view=self)
        except Exception as e:
            print(f"Error disabling buttons: {e}")

        base_points = self.session.current_points
        correct_users_text = []
        wrong_users_text = []

        # 1. 選択肢問題の正否判定
        if self.session.current_question_type == "CHOICE":
            correct_idx = self.session.current_correct_idx
            correct_choice_text = self.session.current_choices[correct_idx - 1]
            correct_ans_preview = f"**{correct_idx}. {correct_choice_text}**"

            for uid in self.session.participants:
                ans_idx = self.session.answers.get(uid)
                if ans_idx == correct_idx:
                    points_gained = base_points
                    ult_text = ""
                    if uid in self.session.active_ult_this_turn:
                        points_gained *= 2
                        ult_text = " (⚡ウルト適用: 2倍!)"
                    
                    self.session.scores[uid] += points_gained
                    self.session.correct_answers_count[uid] = self.session.correct_answers_count.get(uid, 0) + 1
                    correct_users_text.append(f"<@{uid}> (+{points_gained}点){ult_text}")
                else:
                    ans_text = f"選択肢 {ans_idx}" if ans_idx else "未回答"
                    wrong_users_text.append(f"<@{uid}> (回答: {ans_text})")

        # 2. 記述式問題の正否判定 (すでに grading_results が出題者によって入力されている)
        else:
            correct_ans_preview = f"**{self.session.current_correct_text}**"

            for uid in self.session.participants:
                is_correct = self.session.grading_results.get(uid, False)
                ans_text = self.session.answers.get(uid, "未回答")
                
                if is_correct:
                    points_gained = base_points
                    ult_text = ""
                    if uid in self.session.active_ult_this_turn:
                        points_gained *= 2
                        ult_text = " (⚡ウルト適用: 2倍!)"
                    
                    self.session.scores[uid] += points_gained
                    self.session.correct_answers_count[uid] = self.session.correct_answers_count.get(uid, 0) + 1
                    correct_users_text.append(f"<@{uid}> (+{points_gained}点){ult_text}\n> 回答: 「{ans_text}」")
                else:
                    ans_disp = f"「{ans_text}」" if ans_text != "未回答" else "未回答"
                    wrong_users_text.append(f"<@{uid}> \n> 回答: {ans_disp}")

        # 正解者・不正解者の表示組み立て
        correct_mentions = "\n".join(correct_users_text) if correct_users_text else "なし"
        wrong_mentions = "\n".join(wrong_users_text) if wrong_users_text else "なし"

        # 中間順位の組み立て
        scores_sorted = sorted(self.session.scores.items(), key=lambda x: x[1], reverse=True)
        scores_text = "\n".join([f"🏆 <@{uid}>: {score}点" for uid, score in scores_sorted])

        channel = bot.get_channel(self.session.channel_id)
        
        reveal_embed = discord.Embed(
            title=f"📢 第 {self.session.current_question_index} 問 正解発表！",
            description=f"正解 (模範解答) は... {correct_ans_preview} でした！ 🎉",
            color=discord.Color.green()
        )
        reveal_embed.add_field(name="⭕ 正解者", value=correct_mentions, inline=False)
        reveal_embed.add_field(name="❌ 不正解者", value=wrong_mentions, inline=False)
        reveal_embed.add_field(name="📊 現在のスコア（ポイント順）", value=scores_text, inline=False)

        await channel.send(embed=reveal_embed)

        # ターンで使用したウルト状態をクリア
        self.session.active_ult_this_turn.clear()

        # 5秒待機してから次のステップへ
        await asyncio.sleep(5)

        # 次の問題へ進行
        self.session.current_question_index += 1
        if self.session.current_question_index > self.session.total_questions:
            await self.end_quiz()
        else:
            # 救済ウルトの付与判定
            remaining_questions = self.session.total_questions - self.session.current_question_index + 1
            remaining_ratio = remaining_questions / self.session.total_questions
            
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
            self.session.grading_results.clear()
            
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


class QuizGradingView(View):
    """出題者DMで動作する手動採点用ボタンUI"""
    def __init__(self, session: QuizSession):
        super().__init__(timeout=600)
        self.session = session

    @discord.ui.button(label="⭕ 正解", style=discord.ButtonStyle.green, custom_id="grade_correct")
    async def correct_button(self, interaction: discord.Interaction, button: Button):
        await self.process_grade(interaction, True)

    @discord.ui.button(label="❌ 不正解", style=discord.ButtonStyle.red, custom_id="grade_incorrect")
    async def incorrect_button(self, interaction: discord.Interaction, button: Button):
        await self.process_grade(interaction, False)

    async def process_grade(self, interaction: discord.Interaction, is_correct: bool):
        # 現在の採点対象ユーザーID
        queue = self.session.grading_queue
        idx = self.session.current_grading_idx
        
        if idx >= len(queue):
            await interaction.response.send_message("すべての採点はすでに完了しています。", ephemeral=True)
            return

        current_uid = queue[idx]
        self.session.grading_results[current_uid] = is_correct

        # インデックスを進める
        self.session.current_grading_idx += 1
        next_idx = self.session.current_grading_idx

        if next_idx < len(queue):
            # 次の人の採点メッセージへ編集
            next_uid = queue[next_idx]
            next_ans = self.session.answers.get(next_uid, "未回答")
            
            # ウルト使用マーク
            ult_mark = " (⚡ウルト発動中!)" if next_uid in self.session.active_ult_this_turn else ""
            
            await interaction.response.edit_message(
                content=(
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"📝 **記述式 採点中 ({next_idx + 1}/{len(queue)}人目)**\n"
                    f"問題: **{self.session.current_question_text}**\n"
                    f"模範解答: `{self.session.current_correct_text}`\n\n"
                    f"プレイヤー: <@{next_uid}>{ult_mark}\n"
                    f"回答: **「{next_ans}」**\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
                ),
                view=self
            )
        else:
            # すべての採点が完了
            await interaction.response.edit_message(
                content="🎉 **すべてのプレイヤーの採点が完了しました！チャンネルに結果を送信します。**",
                view=None
            )
            
            # クイズセッションを進行させ、チャンネルで正解発表を動かす
            # チャンネルのメッセージを更新するためにダミーのViewから呼ぶ
            question_view = QuizQuestionView(self.session)
            await question_view.reveal_answers()


# ==================== ユーティリティ関数 ====================

async def send_dm_prompt(user: discord.User, session: QuizSession):
    """出題者にDMで次の問題の入力を促す"""
    try:
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


async def send_grading_dm_prompt(session: QuizSession):
    """出題者のDMに採点開始のメッセージと最初の採点ボタンを送信する"""
    host = session.host
    queue = session.grading_queue
    
    if not queue:
        # 回答者が誰もいない等のイレギュラー
        question_view = QuizQuestionView(session)
        await question_view.reveal_answers()
        return

    first_uid = queue[0]
    first_ans = session.answers.get(first_uid, "未回答")
    ult_mark = " (⚡ウルト発動中!)" if first_uid in session.active_ult_this_turn else ""

    view = QuizGradingView(session)
    try:
        msg = await host.send(
            content=(
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 **記述式 採点中 (1/{len(queue)}人目)**\n"
                f"問題: **{session.current_question_text}**\n"
                f"模範解答: `{session.current_correct_text}`\n\n"
                f"プレイヤー: <@{first_uid}>{ult_mark}\n"
                f"回答: **「{first_ans}」**\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━━━━"
            ),
            view=view
        )
        session.grading_dm_message = msg
    except discord.Forbidden:
        # 出題者がDMを受け取れない場合は、チャンネルに警告を出して自動で全員正解にするなどのフォールバック
        channel = bot.get_channel(session.channel_id)
        await channel.send(
            f"⚠️ <@{host.id}> さんへ採点用のDMが送信できませんでした。\n"
            f"採点を行えないため、クイズを強制中止します。"
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
    if message.author.bot:
        return

    # DMの処理
    if isinstance(message.channel, discord.DMChannel):
        user_id = message.author.id
        
        if user_id in host_dm_states:
            dm_state = host_dm_states[user_id]
            session = active_sessions.get(dm_state.session_channel_id)
            
            if not session or session.state != "WAITING_QUESTION_DM" or session.host.id != user_id:
                del host_dm_states[user_id]
                await message.channel.send("現在有効なクイズセッションの出題者ではありません。")
                return

            await handle_host_dm_input(message, dm_state, session)
            return

    await bot.process_commands(message)


async def handle_host_dm_input(message: discord.Message, dm_state: DMState, session: QuizSession):
    """出題者からのDM入力を状態遷移に基づいて処理する"""
    content = message.content.strip()

    if dm_state.state == "WAITING_QUESTION_TEXT":
        if not content:
            await message.channel.send("問題文を入力してください。空の入力は受け付けられません。")
            return
        
        dm_state.question_text = content
        dm_state.state = "WAITING_TYPE"
        
        # 問題タイプ選択ボタンを送信
        view = QuizTypeSelectView(message.author.id)
        await message.channel.send(
            f"問題文を登録しました：\n> {content}\n\n"
            f"次に、問題のタイプを下のボタンから選んでください。",
            view=view
        )

    elif dm_state.state == "WAITING_CHOICES":
        # 選択肢のパース
        choices = [c.strip() for c in content.split(",") if c.strip()]
        
        if len(choices) < 2:
            await message.channel.send("選択肢は最低でも 2 つ以上入力してください。")
            return
        if len(choices) > 25:
            await message.channel.send("選択肢は最大 25 個までです。再度入力してください。")
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

    elif dm_state.state == "WAITING_CORRECT_TEXT":
        # 記述式の模範解答
        if not content:
            await message.channel.send("模範解答を入力してください。")
            return

        dm_state.correct_text = content
        dm_state.state = "WAITING_POINTS"
        await message.channel.send(
            f"模範解答を登録しました：**{content}**\n\n"
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
        
        # セッションに問題情報を反映
        session.current_question_text = dm_state.question_text
        session.current_question_type = dm_state.question_type
        session.current_points = dm_state.points
        
        if dm_state.question_type == "CHOICE":
            session.current_choices = dm_state.choices
            session.current_correct_idx = dm_state.correct_idx
            type_label = "選択肢式"
        else:
            session.current_correct_text = dm_state.correct_text
            type_label = "記述式（手動採点）"
        
        await message.channel.send(
            f"🎉 問題の登録が完了しました！\n"
            f"タイプ: {type_label}\n"
            f"配点: {val}点\n\n"
            f"チャンネルに出題します。"
        )

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

    # 問題タイプごとの表示組み立て
    if session.current_question_type == "CHOICE":
        choices_text = "\n".join([f"**{i+1}.** {choice}" for i, choice in enumerate(session.current_choices)])
        question_desc = f"**問題:**\n{session.current_question_text}\n\n**選択肢:**\n{choices_text}"
        footer_text = "自分が正しいと思う番号のボタンを押してください。回答前にウルトを発動することもできます。"
    else:
        question_desc = f"**記述式問題:**\n{session.current_question_text}\n\n※ボタンを押して回答を入力してください（非公開入力）。"
        footer_text = "「回答を記入する」ボタンを押して回答を入力してください。回答前にウルトを発動することもできます。"

    embed = discord.Embed(
        title=f"❓ 第 {session.current_question_index} 問 / 全 {session.total_questions} 問 (配点: {session.current_points}点)",
        description=f"**出題者:** <@{session.host.id}>\n\n{question_desc}",
        color=discord.Color.orange()
    )
    embed.add_field(name=f"回答済み (0 / {len(session.participants)}人)", value="なし", inline=False)
    embed.add_field(name="未回答", value=unanswered_mentions, inline=False)
    embed.add_field(name="⚡ ウルト残り回数", value=ult_status_text, inline=False)
    embed.set_footer(text=footer_text)

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
    
    await interaction.response.send_message(f"<#{target_channel.id}> でクイズのセットアップを開始しました！", ephemeral=True)
    
    msg = await target_channel.send(embed=embed, view=view)
    session.join_message = msg


@bot.tree.command(name="quiz_abort", description="実行中のクイズセッションを強制終了します（管理者または出題者用）")
async def quiz_abort(interaction: discord.Interaction):
    channel_id = interaction.channel_id
    if channel_id not in active_sessions:
        await interaction.response.send_message("このチャンネルで実行中のクイズセッションはありません。", ephemeral=True)
        return

    session = active_sessions[channel_id]
    
    is_admin = interaction.user.guild_permissions.administrator
    if interaction.user.id != session.host.id and not is_admin:
        await interaction.response.send_message("クイズを中止できるのは、出題者またはサーバー管理者のみです。", ephemeral=True)
        return

    del active_sessions[channel_id]
    
    if session.host.id in host_dm_states:
        del host_dm_states[session.host.id]

    await interaction.response.send_message("🚨 クイズセッションが強制終了されました。")


if __name__ == "__main__":
    if not TOKEN or TOKEN == "YOUR_BOT_TOKEN_HERE":
        print("ERROR: DISCORD_TOKEN が設定されていません。.envファイルを確認してください。")
    else:
        bot.run(TOKEN)
