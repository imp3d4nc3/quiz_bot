import os
import sys
import subprocess
import threading
import tkinter as tk
from tkinter import messagebox, ttk
from tkinter.scrolledtext import ScrolledText

# .env ファイルのパス
ENV_PATH = ".env"

class BotLauncherApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Discord クイズBot ランチャー")
        self.root.geometry("650x600")
        self.root.minsize(550, 500)
        
        self.bot_process = None
        self.log_thread = None
        self.is_running = False

        # GUIのテーマとスタイル設定
        self.style = ttk.Style()
        self.style.theme_use("vista" if "vista" in self.style.theme_names() else "default")
        
        # 画面の構築
        self.create_widgets()
        
        # 初期設定の読み込み
        self.load_env_settings()

        # ウィンドウを閉じる時のイベントハンドラを設定
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    def create_widgets(self):
        # メインフレーム
        main_frame = ttk.Frame(self.root, padding="10")
        main_frame.pack(fill=tk.BOTH, expand=True)

        # ------------------ ステータスエリア ------------------
        status_frame = ttk.LabelFrame(main_frame, text="稼働ステータス", padding="10")
        status_frame.pack(fill=tk.X, pady=(0, 10))

        self.status_label = tk.Label(
            status_frame, 
            text="● 停止中", 
            fg="red", 
            font=("MS Gothic", 12, "bold")
        )
        self.status_label.pack(side=tk.LEFT)

        # ------------------ 設定エリア (.env) ------------------
        config_frame = ttk.LabelFrame(main_frame, text="Bot設定 (.env)", padding="10")
        config_frame.pack(fill=tk.X, pady=(0, 10))

        # トークン入力
        ttk.Label(config_frame, text="DISCORD_TOKEN:").grid(row=0, column=0, sticky=tk.W, pady=5)
        self.token_entry = ttk.Entry(config_frame, width=50, show="*")
        self.token_entry.grid(row=0, column=1, padx=5, pady=5, sticky=tk.EW)
        
        # トークンの表示/非表示切り替えボタン
        self.show_token_var = tk.BooleanVar(value=False)
        self.show_token_cb = ttk.Checkbutton(
            config_frame, 
            text="表示", 
            variable=self.show_token_var, 
            command=self.toggle_token_visibility
        )
        self.show_token_cb.grid(row=0, column=2, padx=5, pady=5)

        # ギルドID入力
        ttk.Label(config_frame, text="GUILD_ID:").grid(row=1, column=0, sticky=tk.W, pady=5)
        self.guild_entry = ttk.Entry(config_frame, width=50)
        self.guild_entry.grid(row=1, column=1, padx=5, pady=5, sticky=tk.EW)
        ttk.Label(config_frame, text="(ローカル同期用ID)").grid(row=1, column=2, padx=5, pady=5, sticky=tk.W)

        # 保存ボタン
        self.save_btn = ttk.Button(config_frame, text="設定を保存", command=self.save_env_settings)
        self.save_btn.grid(row=2, column=1, sticky=tk.E, pady=5, padx=5)

        config_frame.columnconfigure(1, weight=1)

        # ------------------ 操作ボタンエリア ------------------
        control_frame = ttk.Frame(main_frame)
        control_frame.pack(fill=tk.X, pady=(0, 10))

        self.start_btn = ttk.Button(control_frame, text="▶ Botを起動する", command=self.start_bot)
        self.start_btn.pack(side=tk.LEFT, padx=(0, 10), fill=tk.X, expand=True)

        self.stop_btn = ttk.Button(control_frame, text="■ Botを停止する", command=self.stop_bot, state=tk.DISABLED)
        self.stop_btn.pack(side=tk.LEFT, fill=tk.X, expand=True)

        # ------------------ ログ表示エリア ------------------
        log_frame = ttk.LabelFrame(main_frame, text="コンソールログ", padding="5")
        log_frame.pack(fill=tk.BOTH, expand=True)

        self.log_area = ScrolledText(
            log_frame, 
            wrap=tk.WORD, 
            bg="black", 
            fg="lightgreen", 
            font=("Consolas", 9),
            state=tk.DISABLED
        )
        self.log_area.pack(fill=tk.BOTH, expand=True)

    def toggle_token_visibility(self):
        """トークンの伏せ字表示を切り替える"""
        if self.show_token_var.get():
            self.token_entry.config(show="")
        else:
            self.token_entry.config(show="*")

    def load_env_settings(self):
        """ .env ファイルから設定を読み込む """
        token = ""
        guild_id = ""
        
        if os.path.exists(ENV_PATH):
            try:
                with open(ENV_PATH, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line.startswith("DISCORD_TOKEN="):
                            token = line.split("=", 1)[1]
                        elif line.startswith("GUILD_ID="):
                            guild_id = line.split("=", 1)[1]
            except Exception as e:
                self.log_to_area(f"[SYSTEM ERROR] .envの読み込みに失敗しました: {e}\n")

        self.token_entry.delete(0, tk.END)
        self.token_entry.insert(0, token)
        
        self.guild_entry.delete(0, tk.END)
        self.guild_entry.insert(0, guild_id)

    def save_env_settings(self):
        """ 設定を .env ファイルに保存する """
        token = self.token_entry.get().strip()
        guild_id = self.guild_entry.get().strip()

        try:
            with open(ENV_PATH, "w", encoding="utf-8") as f:
                f.write(f"DISCORD_TOKEN={token}\n")
                f.write(f"GUILD_ID={guild_id}\n")
            messagebox.showinfo("保存完了", "設定を .env ファイルに保存しました。")
            self.log_to_area("[SYSTEM] 設定が正常に保存されました。\n")
        except Exception as e:
            messagebox.showerror("エラー", f"設定の保存に失敗しました:\n{e}")
            self.log_to_area(f"[SYSTEM ERROR] 設定の保存に失敗しました: {e}\n")

    def log_to_area(self, text):
        """ ログエリアにテキストを出力する """
        self.log_area.config(state=tk.NORMAL)
        self.log_area.insert(tk.END, text)
        self.log_area.see(tk.END)
        self.log_area.config(state=tk.DISABLED)

    def start_bot(self):
        """ Botプロセスを起動する """
        if self.is_running:
            return

        token = self.token_entry.get().strip()
        if not token or token == "YOUR_BOT_TOKEN_HERE":
            messagebox.showwarning("警告", "有効な DISCORD_TOKEN を設定してください。")
            return

        # ボタンとステータス表示の更新
        self.is_running = True
        self.status_label.config(text="● 稼働中", fg="green")
        self.start_btn.config(state=tk.DISABLED)
        self.stop_btn.config(state=tk.NORMAL)
        self.token_entry.config(state=tk.DISABLED)
        self.guild_entry.config(state=tk.DISABLED)
        self.save_btn.config(state=tk.DISABLED)

        self.log_to_area("[SYSTEM] Discord Botの起動を試みています...\n")

        # bot.py を subprocess で実行
        try:
            # Windowsで黒いコンソール画面が出ないようにする
            creation_flags = 0
            if os.name == "nt":
                creation_flags = subprocess.CREATE_NO_WINDOW

            self.bot_process = subprocess.Popen(
                [sys.executable, "bot.py"],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                creationflags=creation_flags
            )

            # ログ読み込みスレッドの開始
            self.log_thread = threading.Thread(target=self.read_bot_output, daemon=True)
            self.log_thread.start()

        except Exception as e:
            self.is_running = False
            self.status_label.config(text="● 停止中", fg="red")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.token_entry.config(state=tk.NORMAL)
            self.guild_entry.config(state=tk.NORMAL)
            self.save_btn.config(state=tk.NORMAL)
            
            messagebox.showerror("起動エラー", f"Botの実行に失敗しました:\n{e}")
            self.log_to_area(f"[SYSTEM ERROR] 起動失敗: {e}\n")

    def read_bot_output(self):
        """ bot.py の標準出力を別スレッドで読み取りログエリアに出力する """
        while self.bot_process and self.bot_process.poll() is None:
            line = self.bot_process.stdout.readline()
            if line:
                # GUIスレッドセーフにテキストを挿入
                self.root.after(0, self.log_to_area, line)
        
        # プロセスが終了した場合のクリーンアップ
        self.root.after(0, self.handle_bot_termination)

    def handle_bot_termination(self):
        """ Botプロセスが終了した際のGUI更新 """
        if self.is_running:
            self.is_running = False
            self.status_label.config(text="● 停止中", fg="red")
            self.start_btn.config(state=tk.NORMAL)
            self.stop_btn.config(state=tk.DISABLED)
            self.token_entry.config(state=tk.NORMAL)
            self.guild_entry.config(state=tk.NORMAL)
            self.save_btn.config(state=tk.NORMAL)
            self.log_to_area("[SYSTEM] Botプロセスが終了しました。\n")
            self.bot_process = None

    def stop_bot(self):
        """ Botプロセスを停止する """
        if not self.is_running or not self.bot_process:
            return

        self.log_to_area("[SYSTEM] Botを停止しています...\n")
        try:
            self.bot_process.terminate()
            # プロセスの終了を少し待つ
            self.bot_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.bot_process.kill()
            self.log_to_area("[SYSTEM] プロセスが強制終了されました。\n")
        except Exception as e:
            self.log_to_area(f"[SYSTEM ERROR] 停止プロセス中にエラーが発生しました: {e}\n")

    def on_closing(self):
        """ アプリ終了時にBotプロセスも確実に終了させる """
        if self.is_running and self.bot_process:
            if messagebox.askokcancel("確認", "Botが稼働中です。アプリを閉じてBotを終了しますか？"):
                self.stop_bot()
                self.root.destroy()
        else:
            self.root.destroy()


if __name__ == "__main__":
    root = tk.Tk()
    app = BotLauncherApp(root)
    root.mainloop()
