import logging
import os
import queue
import subprocess
import sys
import threading
import time
import yaml
import shutil
import tkinter as tk
from functools import partial
from tkinter import messagebox
from tkinter import scrolledtext
from tkinter import ttk

import resource
import autodori
import version


# --- 关键部分：设置日志重定向 ---
class QueueHandler(logging.Handler):
    def __init__(self, log_queue):
        super().__init__()
        self.log_queue = log_queue
    def emit(self, record):
        msg = record.getMessage()
        if msg.startswith("send operation:"):
            return
        self.log_queue.put(self.format(record))

class FilteringFileHandler(logging.FileHandler):
    def emit(self, record):
        msg = record.getMessage()
        # The raw minitouch command stream is only useful when explicitly
        # debugging, and it is far too heavy to keep by default.
        if msg.startswith("send operation:") and not getattr(autodori, "DEBUG_LOGGING", False):
            return
        super().emit(record)

# 2. GUI应用主类
class AutodoriGUI:
    def __init__(self, master):
        self.master = master
        master.title(f"autodoriUI {version.VERSION}")
        master.geometry("700x550")

        self.bot_thread = None
        self.is_float_mode = False
        self.float_window = None # Toplevel 实例

        # --- 为UI控件创建Tkinter变量 ---
        self.mode_var = tk.StringVar(value='free_auto')
        self.difficulty_var = tk.StringVar(value=autodori.DIFFICULTY)
        self.human_var = tk.BooleanVar(value=autodori.HUMAN_DELAY_ENABLED)
        self.max_continuous_not_fc_var = tk.IntVar(value=autodori.MAX_CONTINUOUS_NOT_FC_COUNT)
        self.max_attempt_var = tk.IntVar(value=autodori.MAX_ATTEMPT_COUNT)
        self.play_count_var = tk.IntVar(value=autodori.PLAY_COUNT_TARGET)
        self.disclaimer_agreed_var = tk.BooleanVar(value=False)

        self.config_path = autodori.config_path  # 直接使用后端的 Path 对象
        self.editable_globals = {
            'PHOTOGATE_LATENCY': int,
            'MIN_LIVEBOOST': int,
            'DEFAULT_MOVE_SLICE_SIZE': int,
            'CMD_SLICE_SIZE': int,
            'MAX_CONTINUOUS_FAILED_TIMES': int,
            'STABLE_THRESHOLD': int,
            'CONSECUTIVE_FRAMES_NEEDED': int,
            'FREEZE_SLEEP_TIME': float,
            'CONFIDENCE_THRESHOLD_FAILURE': float,
            'CONFIDENCE_THRESHOLD_PLAY': float,
            'IS_FULL_SONG': bool,
            'IS_HIGH_DIFFICULTY': bool,
            'DEBUG_LOGGING': bool
        }
        self.load_settings()  # 初始化变量后加载配置

        # --- 绑定状态追踪 ---
        self.human_var.trace_add('write', self._update_warnings)

        # 新增：为所有控制面板上的变量绑定实时保存
        self.mode_var.trace_add('write', self._auto_save_callback)
        self.difficulty_var.trace_add('write', self._auto_save_callback)
        self.human_var.trace_add('write', self._auto_save_callback)
        self.max_continuous_not_fc_var.trace_add('write', self._auto_save_callback)
        self.max_attempt_var.trace_add('write', self._auto_save_callback)
        self.play_count_var.trace_add('write', self._auto_save_callback)

        # --- 设置日志队列 (用于GUI显示) ---
        self.log_queue = queue.Queue()
        queue_handler = QueueHandler(self.log_queue)
        # 注意：这里我们不再对 queue_handler 设置格式，让根记录器统一处理

        # --- 新增：设置文件日志 (用于输出到文件) ---
        debug_folder = autodori.data_path("debug")
        os.makedirs(debug_folder, exist_ok=True)  # 创建debug文件夹

        # 创建带时间戳的日志文件名
        log_filename = f"autodori_{time.strftime('%Y%m%d-%H%M%S')}.log"
        log_filepath = os.path.join(debug_folder, log_filename)

        # 创建文件处理器，指定路径和编码
        file_handler = FilteringFileHandler(log_filepath, encoding='utf-8')

        # --- 配置根记录器 (Logger) ---
        # 获取根记录器
        root_logger = logging.getLogger()
        root_logger.setLevel(logging.INFO)  # 设置日志级别

        # 创建一个通用的日志格式
        formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

        # 为两个处理器设置相同的格式
        queue_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)

        # 将两个处理器都添加到根记录器中
        root_logger.addHandler(queue_handler)
        root_logger.addHandler(file_handler)

        # --- 启动日志处理循环 ---
        self.master.after(100, self._process_log_queue)

        # --- 新增：用于存储全局变量UI控件的变量 ---
        self.global_vars_entries = {}

        if self.disclaimer_agreed_var.get():
            # 如果配置中记录已经同意过，直接创建主界面
            self._create_main_widgets()
        else:
            # 否则，按原计划显示免责声明界面
            self._create_disclaimer_view()

    def _create_disclaimer_view(self):
        """创建并显示欢迎/风险提示界面。"""
        # 设置主窗口的最小尺寸，防止用户缩得过小
        self.master.minsize(700, 550)

        self.disclaimer_frame = ttk.Frame(self.master, padding="15")
        self.disclaimer_frame.pack(fill=tk.BOTH, expand=True)

        # 标题
        title_label = ttk.Label(self.disclaimer_frame, text="使用须知", font=("", 16, "bold"))
        title_label.pack(pady=(10, 20))

        # --- 文案内容 ---
        info_text = (
            "在开始前，请仔细阅读以下使用说明与风险提示："
        )
        # 步骤1：将标签保存为实例变量，并移除固定的 wraplength
        self.info_label = ttk.Label(self.disclaimer_frame, text=info_text, justify=tk.LEFT, font=("", 11))
        self.info_label.pack(fill=tk.X, pady=5)

        usage_frame = ttk.LabelFrame(self.disclaimer_frame, text="使用方法", padding="10")
        usage_frame.pack(fill=tk.X, pady=10)
        usage_text = (
            "仅支持MuMu模拟器V5，分辨率请设置为1280x720以提高性能。\n"
            "歌曲难度请手动设置为与游戏中一致。\n"
            "自由演出——单曲模式：启动任务前，请手动进入自由演出模式下的「选择乐队」界面。\n"
            "自由演出——自动模式：启动任务前，请手动进入自由演出模式下的「选择乐队」界面。\n"
            "巡回演出——单曲模式：启动任务前，请手动进入巡回演出模式下的「第X曲开始」界面。"
        )
        self.usage_label = ttk.Label(usage_frame, text=usage_text, justify=tk.LEFT)
        self.usage_label.pack(fill=tk.X)

        warning_frame = ttk.LabelFrame(self.disclaimer_frame, text="风险提示", padding="10")
        warning_frame.pack(fill=tk.X, pady=10)
        warning_text = (
            "本程序仅于自由演出、巡回演出模式下进行开发与测试，切勿用于协力模式。\n"
            "程序运行期间，建议您时刻关注模拟器界面。若出现任何异常情况，请立即手动停止任务。\n"
            "使用本程序可能违反游戏的用户协议，您将自行承担一切潜在风险。开发者对由此产生的任何后果概不负责。"
        )
        self.warning_label = ttk.Label(warning_frame, text=warning_text, justify=tk.LEFT, foreground="red")
        self.warning_label.pack(fill=tk.X)

        # --- 同意与继续 ---
        self.agree_var = tk.BooleanVar()
        agree_check = ttk.Checkbutton(self.disclaimer_frame, text="我已阅读并理解以上条款，同意自行承担所有风险。",
                                      variable=self.agree_var, command=self._toggle_proceed_button_state)
        agree_check.pack(pady=15)

        self.proceed_button = ttk.Button(self.disclaimer_frame, text="进入控制面板", state=tk.DISABLED,
                                         command=self._show_main_app)
        self.proceed_button.pack(fill=tk.X, padx=100, ipady=5)

        ttk.Label(self.disclaimer_frame, text=f"版本 {version.VERSION}",
                  foreground="gray").pack(pady=(10, 0))

        # 步骤3：将事件处理函数绑定到容器的 <Configure> 事件
        self.disclaimer_frame.bind("<Configure>", self._on_disclaimer_resize)

    def _on_disclaimer_resize(self, event):
        """步骤2：创建事件处理函数，在窗口/框架尺寸变化时调用。"""
        # event.width 提供了容器当前的宽度
        # 减去一些像素作为边距(padding)，防止文本紧贴边缘
        new_wraplength = event.width - 40

        # 更新所有需要换行的标签
        self.info_label.config(wraplength=new_wraplength)

        # 对于在LabelFrame内部的标签，其可用宽度要更小一些
        self.usage_label.config(wraplength=new_wraplength - 20)
        self.warning_label.config(wraplength=new_wraplength - 20)

    def _toggle_proceed_button_state(self):
        """根据复选框状态切换“继续”按钮的可用性。"""
        if self.agree_var.get():
            self.proceed_button.config(state=tk.NORMAL)
        else:
            self.proceed_button.config(state=tk.DISABLED)

    def _show_main_app(self):
        self.disclaimer_agreed_var.set(True)
        # 如果你之前实现了 _auto_save_callback，由于 disclaimer_agreed_var 没有绑定 trace，
        # 所以我们需要在这里手动调用一次 save_settings()
        if hasattr(self, 'config_path'):
            self.save_settings()
        """销毁欢迎界面并构建主应用程序界面。"""
        self.disclaimer_frame.destroy()
        self._create_main_widgets()

    def _create_float_window(self):
        """创建并配置悬浮窗。"""
        self.float_window = tk.Toplevel(self.master)
        self.float_window.title("Autodori Float")

        # 移除窗口装饰 (标题栏和边框)
        self.float_window.overrideredirect(True)

        # 始终保持在最上方
        self.float_window.attributes('-topmost', True)

        # 设置透明度 (可选，但常用)
        # self.float_window.attributes('-alpha', 0.9)

        # 绑定鼠标拖动事件
        self.float_window.bind("<ButtonPress-1>", self._start_drag)
        self.float_window.bind("<B1-Motion>", self._drag_window)

        # 隐藏主窗口 (可选：如果想完全隐藏主UI)
        self.master.withdraw()

        self.float_window.protocol("WM_DELETE_WINDOW", self.exit_float_mode)

        # 标记当前模式
        self.is_float_mode = True

        self._create_float_widgets(self.float_window)

    def _create_float_widgets(self, parent):
        """填充悬浮窗的UI内容 (日志、按钮)，并使其紧凑。"""

        # 1. 控件容器 Frame：减小 padding 和 borderwidth
        # padding=1 边距非常小
        main_frame = ttk.Frame(parent, padding=1, relief=tk.RAISED, borderwidth=1)
        main_frame.pack(fill=tk.BOTH, expand=True)

        # 2. 按钮 Frame：移除垂直间距 pady
        button_frame = ttk.Frame(main_frame)
        button_frame.pack(fill=tk.X)  # 移除 pady=(0, 5)

        # 停止按钮：简化文本
        float_stop_button = ttk.Button(button_frame, text="停止", command=self.stop_bot)
        # padx=1 减小按钮间的水平间距
        float_stop_button.pack(side=tk.LEFT, padx=1, fill=tk.X, expand=True)
        self.float_stop_button = float_stop_button

        # 退出悬浮模式按钮：简化文本
        exit_float_button = ttk.Button(button_frame, text="主界面", command=self.exit_float_mode)
        exit_float_button.pack(side=tk.LEFT, padx=1, fill=tk.X, expand=True)

        # 退出程序按钮：简化文本
        exit_app_button = ttk.Button(button_frame, text="退出", command=self.master.destroy)
        exit_app_button.pack(side=tk.LEFT, padx=1)

        ttk.Label(button_frame, text=version.VERSION, foreground="gray").pack(side=tk.RIGHT, padx=(4, 2))

        # 3. 日志显示：减小默认尺寸
        # height=5, width=30 减少日志区域的最小尺寸
        LOG_FONT = ("Cascadia Mono", 8)  # 或 ("Arial", 10), ("Courier New", 7) 等

        self.float_log_display = scrolledtext.ScrolledText(main_frame, state='disabled', wrap=tk.WORD,
                                                           bg="#2b2b2b", fg="white",
                                                           height=10, width=40,
                                                           font=LOG_FONT)  # <<<--- 新增 font 参数

        self.float_log_display.pack(fill=tk.BOTH, expand=True)

        # 初始化按钮状态
        self._update_float_button_state()

    def exit_float_mode(self):
        """退出悬浮窗模式，返回主控制面板。"""
        if self.float_window:
            self.float_window.destroy()
            self.float_window = None
            self.is_float_mode = False
            self.master.deiconify()  # 显示主窗口

    # --- 悬浮窗拖动逻辑 ---
    def _start_drag(self, event):
        """记录鼠标点击时的初始位置。"""
        self._x = event.x
        self._y = event.y

    def _drag_window(self, event):
        """计算并移动窗口。"""
        deltax = event.x - self._x
        deltay = event.y - self._y
        x = self.float_window.winfo_x() + deltax
        y = self.float_window.winfo_y() + deltay
        self.float_window.geometry(f"+{x}+{y}")

    def _update_float_button_state(self):
        """同步更新悬浮窗的停止按钮状态。"""
        if self.is_float_mode and hasattr(self, 'float_stop_button'):
            is_running = (self.bot_thread and self.bot_thread.is_alive())
            self.float_stop_button.config(state=tk.NORMAL if is_running else tk.DISABLED)

    def _create_main_widgets(self):
        """创建主控制面板的所有控件。"""
        # --- 新增: 创建Notebook作为选项卡容器 ---
        self.notebook = ttk.Notebook(self.master)
        self.notebook.pack(fill=tk.BOTH, expand=True, padx=5, pady=5)

        # --- 创建第一个选项卡：控制面板 ---
        control_panel_frame = ttk.Frame(self.notebook, padding="5")
        self.notebook.add(control_panel_frame, text='控制面板')
        self._create_control_panel_tab(control_panel_frame)

        # --- 创建第二个选项卡：全局变量调试 ---
        globals_frame = ttk.Frame(self.notebook, padding="10")
        self.notebook.add(globals_frame, text='全局变量调试')
        self._create_globals_tab(globals_frame)

    def _create_control_panel_tab(self, parent_frame):
        """填充“控制面板”选项卡的内容"""
        self.paned_window = ttk.PanedWindow(parent_frame, orient=tk.VERTICAL)
        self.paned_window.pack(fill=tk.BOTH, expand=True)

        top_frame = ttk.Frame(self.paned_window, padding="5")
        self.paned_window.add(top_frame, weight=0)

        style = ttk.Style()
        style.configure("Warning.TLabel", foreground="red")

        config_frame = ttk.LabelFrame(top_frame, text=f"配置  ·  autodoriUI {version.VERSION}")
        config_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(0, 5))

        mode_frame = ttk.Frame(config_frame)
        mode_frame.pack(fill=tk.X, padx=2, pady=2)
        ttk.Label(mode_frame, text="模式:").pack(side=tk.LEFT)
        ttk.Radiobutton(mode_frame, text="自由演出——单曲模式", variable=self.mode_var, value='free_single', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="自由演出——全自动模式", variable=self.mode_var, value='free_auto', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        ttk.Radiobutton(mode_frame, text="巡回演出——单曲模式", variable=self.mode_var, value='medley_single', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        #ttk.Radiobutton(mode_frame, text="story", variable=self.mode_var, value='story', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        #ttk.Radiobutton(mode_frame, text="rouge", variable=self.mode_var, value='rouge', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)

        # 第二行：全自动一轮（开机引导 + 指定完成次数）
        mode_frame2 = ttk.Frame(config_frame)
        mode_frame2.pack(fill=tk.X, padx=2, pady=2)
        ttk.Radiobutton(mode_frame2, text="全自动一轮（启动模拟器→登录→跑满次数）", variable=self.mode_var,
                        value='boot_auto', command=self._on_mode_change).pack(side=tk.LEFT, padx=5)
        self.play_count_frame = ttk.Frame(mode_frame2)
        ttk.Label(self.play_count_frame, text="完成次数：").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Spinbox(self.play_count_frame, from_=1, to=99, textvariable=self.play_count_var, width=5).pack(
            side=tk.LEFT, padx=3)

        warnings_frame = ttk.Frame(mode_frame)
        warnings_frame.pack(side=tk.RIGHT, padx=(10, 0))

        options_frame = ttk.Frame(config_frame)
        options_frame.pack(fill=tk.X, padx=2, pady=2)

        ttk.Label(options_frame, text="难度:").pack(side=tk.LEFT)
        # --- 修改：将控件保存为实例变量，用于定位 ---
        self.difficulty_combobox = ttk.Combobox(options_frame, textvariable=self.difficulty_var,
                                                values=['easy', 'normal', 'hard', 'expert', 'special'], width=10)
        self.difficulty_combobox.pack(side=tk.LEFT, padx=3)

        # --- 修改：将FC相关控件放入独立的Frame中 ---
        self.fc_options_frame = ttk.Frame(options_frame)
        ttk.Label(self.fc_options_frame, text="未FC跳过阈值：").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Spinbox(self.fc_options_frame, from_=1, to=99, textvariable=self.max_continuous_not_fc_var, width=5).pack(side=tk.LEFT, padx=3)
        ttk.Label(self.fc_options_frame, text="最大尝试次数：").pack(side=tk.LEFT, padx=(5, 0))
        ttk.Spinbox(self.fc_options_frame, from_=1, to=99, textvariable=self.max_attempt_var, width=5).pack(side=tk.LEFT, padx=3)

        self.human_delay_frame = ttk.Frame(options_frame)
        self.human_delay_frame.pack(side=tk.LEFT, padx=5)
        ttk.Checkbutton(self.human_delay_frame, text="随机化按键", variable=self.human_var).pack(side=tk.LEFT)

        # 警告标签
        self.human_delay_warning_label = ttk.Label(warnings_frame, text="警告：可能导致不能FC", style="Warning.TLabel")

        self.bg_color = style.lookup("TFrame", "background")

        self.human_delay_warning_label.pack(anchor='w')

        self._update_warnings()

        control_frame = ttk.Frame(top_frame)
        control_frame.pack(fill=tk.X, expand=True, side=tk.TOP, pady=(5, 0))
        self.start_button = ttk.Button(control_frame, text="启动任务", command=self.start_bot)
        self.start_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.stop_button = ttk.Button(control_frame, text="停止任务", command=self.stop_bot, state=tk.DISABLED)
        self.stop_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2)
        self.float_button = ttk.Button(control_frame, text="进入悬浮窗", command=self._create_float_window)
        self.float_button.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=2) # 占据剩余空间

        log_frame = ttk.Frame(self.paned_window, padding="0")
        self.paned_window.add(log_frame, weight=1)
        self.log_display = scrolledtext.ScrolledText(log_frame, state='disabled', wrap=tk.WORD, bg="#2b2b2b",
                                                     fg="white")
        self.log_display.pack(fill=tk.BOTH, expand=True)

        self.paned_window.bind("<ButtonPress-1>", self._prevent_resize)
        self.paned_window.bind("<B1-Motion>", self._prevent_resize)

        # --- 新增：在UI创建完成后，调用一次模式切换函数来设置初始状态 ---
        self._on_mode_change()

    # --- 新增方法：用于根据模式动态显示/隐藏UI控件 ---
    def _on_mode_change(self):
        """当模式（单曲/全自动/全自动一轮）切换时，动态显示或隐藏相关配置项。"""
        mode = self.mode_var.get()
        if mode == 'free_auto':
            # 在全自动模式下：显示FC相关设置
            self.fc_options_frame.pack(side=tk.LEFT, after=self.difficulty_combobox)
        else:
            # 在单曲模式下：隐藏FC相关设置
            self.fc_options_frame.pack_forget()

        if mode == 'boot_auto':
            self.play_count_frame.pack(side=tk.LEFT, padx=5)
        else:
            self.play_count_frame.pack_forget()

    def _create_globals_tab(self, parent_frame):
        """填充“全局变量调试”选项卡的内容"""
        # 定义需要暴露的全局变量及其类型 (int, float, dict)
        self.editable_globals = {
            'PHOTOGATE_LATENCY': int,
            'MIN_LIVEBOOST': int,
            'DEFAULT_MOVE_SLICE_SIZE': int,
            'CMD_SLICE_SIZE': int,
            'MAX_CONTINUOUS_FAILED_TIMES': int,
            'STABLE_THRESHOLD': int,
            'CONSECUTIVE_FRAMES_NEEDED': int,
            'FREEZE_SLEEP_TIME': float,
            'CONFIDENCE_THRESHOLD_FAILURE': float,
            'CONFIDENCE_THRESHOLD_PLAY': float,
            'IS_FULL_SONG': bool,
            'IS_HIGH_DIFFICULTY': bool,
            'DEBUG_LOGGING': bool
        }

        # --- 新增：本地化文本映射 ---
        localization_map = {
            'PHOTOGATE_LATENCY': "光电门延迟 (ms)",
            'MIN_LIVEBOOST': "最小 LiveBoost 值",
            'DEFAULT_MOVE_SLICE_SIZE': "滑动音符切片大小",
            'CMD_SLICE_SIZE': "指令分片大小",
            'MAX_CONTINUOUS_FAILED_TIMES': "最大连续失败次数",
            'STABLE_THRESHOLD': "画面静止判定阈值",
            'CONSECUTIVE_FRAMES_NEEDED': "画面静止所需帧数",
            'FREEZE_SLEEP_TIME': "画面静止检测间隔时间 (s)",
            'CONFIDENCE_THRESHOLD_FAILURE': "失败检测置信度",
            'CONFIDENCE_THRESHOLD_PLAY': "歌曲开始检测置信度",
            'IS_FULL_SONG': "FULL乐曲支持",
            'IS_HIGH_DIFFICULTY': "超高难易度支持",
            'DEBUG_LOGGING': "调试日志（每片时序 + 指令流）"
        }

        # 使用 grid 布局
        parent_frame.columnconfigure(1, weight=1)

        # 动态创建 Label 和 Entry
        current_row = 0
        for var_name, var_type in self.editable_globals.items():
            # --- 修改：使用本地化文本 ---
            # 使用 .get() 方法，如果映射中没有找到，则安全地回退到原始变量名
            display_name = localization_map.get(var_name, var_name)
            ttk.Label(parent_frame, text=f"{display_name}:", font=("", 10)).grid(row=current_row, column=0, sticky='w',
                                                                                 padx=5, pady=5)

            if var_type == dict:
                dict_frame = ttk.Frame(parent_frame)
                dict_frame.grid(row=current_row, column=1, sticky='ew', padx=5, pady=2)
                self.global_vars_entries[var_name] = {}

                # 获取字典的当前值
                current_dict = getattr(autodori, var_name)

                col_count = 0
                for key, value in current_dict.items():
                    ttk.Label(dict_frame, text=key).grid(row=0, column=col_count, padx=(0, 2))
                    entry_var = tk.StringVar(value=str(value))
                    entry = ttk.Entry(dict_frame, textvariable=entry_var, width=8)
                    entry.grid(row=0, column=col_count + 1, padx=(0, 10))

                    self.global_vars_entries[var_name][key] = entry_var
                    col_count += 2

            elif var_type == bool:
                # 使用 tk.BooleanVar 来存储复选框的状态 (True/False)
                bool_var = tk.BooleanVar(value=getattr(autodori, var_name))
                checkbutton = ttk.Checkbutton(parent_frame, variable=bool_var)
                checkbutton.grid(row=current_row, column=1, sticky='w', padx=5, pady=2)  # 使用 sticky='w' 左对齐
                self.global_vars_entries[var_name] = bool_var

            else:  # int or float
                entry_var = tk.StringVar(value=str(getattr(autodori, var_name)))
                entry = ttk.Entry(parent_frame, textvariable=entry_var)
                entry.grid(row=current_row, column=1, sticky='ew', padx=5, pady=2)
                self.global_vars_entries[var_name] = entry_var

            current_row += 1

        # 添加分隔符
        ttk.Separator(parent_frame, orient='horizontal').grid(row=current_row, column=0, columnspan=2, sticky='ew',
                                                              pady=15)
        current_row += 1

        # 添加按钮
        button_frame = ttk.Frame(parent_frame)
        button_frame.grid(row=current_row, column=0, columnspan=2, sticky='e')

        apply_button = ttk.Button(button_frame, text="应用修改", command=self._apply_global_settings)
        apply_button.pack(side=tk.RIGHT, padx=5)

    def _apply_global_settings(self):
        """将UI中的值应用到后端的全局变量"""
        try:
            for var_name, controls in self.global_vars_entries.items():
                var_type = self.editable_globals[var_name]

                if var_type == dict:
                    current_dict = getattr(autodori, var_name)
                    for key, entry_var in controls.items():
                        new_val_str = entry_var.get()
                        # 尝试转换为 float，如果失败再转为 int
                        try:
                            current_dict[key] = float(new_val_str)
                        except ValueError:
                            current_dict[key] = int(new_val_str)
                else:  # int or float
                    new_val_str = controls.get()
                    if var_type == int:
                        setattr(autodori, var_name, int(new_val_str))
                    elif var_type == float:
                        setattr(autodori, var_name, float(new_val_str))
                    elif var_type == bool:
                        setattr(autodori, var_name, new_val_str)

            self.save_settings()  # <--- 新增：点击“应用修改”后同步保存到本地文件
            messagebox.showinfo("成功", "全局变量已成功更新并保存！")

        except ValueError as e:
            messagebox.showerror("输入错误", f"修改失败，请输入有效的数值。\n错误: {e}")
        except Exception as e:
            messagebox.showerror("未知错误", f"应用设置时发生错误: {e}")

    def load_settings(self):
        """从 yaml 文件加载用户配置和全局变量"""
        if self.config_path.exists():
            try:
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}

                # 覆盖控制面板的 Tkinter 变量
                if "mode" in config: self.mode_var.set(config["mode"])
                if "difficulty" in config: self.difficulty_var.set(config["difficulty"])
                if "human_delay" in config: self.human_var.set(config["human_delay"])
                if "max_continuous_not_fc_count" in config: self.max_continuous_not_fc_var.set(
                    config["max_continuous_not_fc_count"])
                if "max_attempt_count" in config: self.max_attempt_var.set(config["max_attempt_count"])
                if "play_count" in config: self.play_count_var.set(config["play_count"])
                if "disclaimer_agreed" in config: self.disclaimer_agreed_var.set(config["disclaimer_agreed"])
                # 覆盖后端的全局变量
                if "globals" in config:
                    for k, v in config["globals"].items():
                        if hasattr(autodori, k):
                            setattr(autodori, k, v)
            except Exception as e:
                logging.error(f"加载配置文件失败: {e}")

    def save_settings(self):
        """将当前UI的设置和后端的全局变量保存到 yaml 文件"""
        # 先尝试读取现有配置，避免覆盖文件中的其他非 GUI 内容
        try:
            if self.config_path.exists():
                with open(self.config_path, "r", encoding="utf-8") as f:
                    config = yaml.safe_load(f) or {}
            else:
                config = {}
        except Exception:
            config = {}

        # 准备全局变量数据
        globals_to_save = {}
        for var_name in self.editable_globals.keys():
            globals_to_save[var_name] = getattr(autodori, var_name)

        # 更新配置字典
        config.update({
            "mode": self.mode_var.get(),
            "difficulty": self.difficulty_var.get(),
            "human_delay": self.human_var.get(),
            "max_continuous_not_fc_count": self.max_continuous_not_fc_var.get(),
            "max_attempt_count": self.max_attempt_var.get(),
            "play_count": self.play_count_var.get(),
            "disclaimer_agreed": self.disclaimer_agreed_var.get(),  # 新增：保存免责声明状态
            "globals": globals_to_save
        })

        # 写入 YAML 文件
        try:
            with open(self.config_path, "w", encoding="utf-8") as f:
                # allow_unicode=True 确保中文正常显示，不被转码为 Unicode 字符串
                yaml.dump(config, f, allow_unicode=True, sort_keys=False, indent=4)
        except Exception as e:
            logging.error(f"保存配置文件失败: {e}")

    def _auto_save_callback(self, *args):
        """当控制面板上的 Tkinter 变量发生变化时，实时触发保存"""
        # 确保配置路径已初始化，防止在加载阶段意外报错
        if hasattr(self, 'config_path'):
            self.save_settings()

    def _process_log_queue(self):
        try:
            while True:
                record = self.log_queue.get(block=False)
                # --- 更新主窗口日志 ---
                if hasattr(self, 'log_display'):
                    self.log_display.configure(state='normal')
                    self.log_display.insert(tk.END, record + '\n')
                    self.log_display.see(tk.END)
                    self.log_display.configure(state='disabled')

                # --- 更新悬浮窗日志 ---
                # --- 更新悬浮窗日志 (格式化) ---
                if self.is_float_mode and hasattr(self, 'float_log_display'):

                    # 原始格式: YYYY-MM-DD HH:MM:SS - [LEVEL] - MESSAGE

                    try:
                        # 1. 查找第一个分隔符 ' - [' 的起始位置 (用于时间)
                        time_separator_index = record.find(' - [')

                        # 2. 查找第二个分隔符 '] - ' 的起始位置 (用于消息)
                        msg_separator_index = record.find('] - ')

                        if time_separator_index != -1 and msg_separator_index != -1:

                            # 提取 HH:MM:SS (从索引 11 开始到 time_separator_index 之前)
                            time_part = record[time_separator_index - 8:time_separator_index]  # 提取 HH:MM:SS

                            # 提取 [LEVEL] (从 time_separator_index + 3 开始，到 msg_separator_index 结束)
                            level_part = record[time_separator_index + 3: msg_separator_index + 1]  # 提取 [LEVEL]

                            # 提取 MESSAGE (从 msg_separator_index + 4 开始)
                            message_part = record[msg_separator_index + 4:].strip()  # 提取 MESSAGE 并去除首尾空格

                            # 组合新的紧凑格式: HH:MM:SS [LEVEL]消息
                            # 注意: level_part 已经是 '[LEVEL]' 的形式，后面紧跟 message_part
                            compact_record = f"{time_part}{level_part}{message_part}"
                        else:
                            compact_record = record.split(" - ", 2)[-1]  # 无法解析时，只保留消息

                    except Exception:
                        # 出现异常时（如索引错误），退回到只保留消息
                        compact_record = record.split(" - ", 2)[-1]

                    self.float_log_display.configure(state='normal')
                    self.float_log_display.insert(tk.END, compact_record + '\n')
                    self.float_log_display.see(tk.END)
                    self.float_log_display.configure(state='disabled')
        except queue.Empty:
            pass
        self.master.after(100, self._process_log_queue)

    def start_bot(self):
        self.save_settings()
        # A stop from the previous run leaves the event set; clear it or the new
        # startup path (emulator launch, controller connect) aborts immediately.
        autodori.stop_event.clear()
        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)
        self._update_float_button_state()
        self.notebook.tab(1, state='disabled')

        autodori.LIVEMODE = self.mode_var.get()
        autodori.DIFFICULTY = self.difficulty_var.get()
        autodori.HUMAN_DELAY_ENABLED = self.human_var.get()
        autodori.MAX_CONTINUOUS_NOT_FC_COUNT = self.max_continuous_not_fc_var.get()
        autodori.MAX_ATTEMPT_COUNT = self.max_attempt_var.get()
        # 只有“全自动一轮”受完成次数约束，其它模式保持无限循环
        autodori.PLAY_COUNT_TARGET = (
            self.play_count_var.get() if self.mode_var.get() == 'boot_auto' else 0
        )

        self.bot_thread = threading.Thread(target=self._run_bot_task, daemon=True)
        self.bot_thread.start()

    def stop_bot(self):
        # 优先设置停止信号，让内部循环能够尽快响应
        if hasattr(autodori, 'stop_event'):
            logging.info("Sending stop signal to internal loop.")
            autodori.stop_event.set()  # <-- 这是关键的新增行

        # 然后再通知maatasker停止任务流
        if hasattr(autodori, 'maatasker') and autodori.maatasker and autodori.maatasker.running:
            logging.info("Attempting to stop maatasker.")
            autodori.maatasker.post_stop()
        else:
            pass

    def auto_start(self):
        """
        Start a boot-auto session without a click.

        Used by `--auto`, which is what the Windows scheduled task runs at the
        configured time, so the whole session can run unattended.
        """
        if not self.disclaimer_agreed_var.get():
            logging.warning("--auto requested but the disclaimer has not been accepted; not starting.")
            return
        self.mode_var.set('boot_auto')
        self._on_mode_change()
        logging.info(
            f"Automatic start: boot_auto session begins now "
            f"(difficulty={self.difficulty_var.get()}, songs={self.play_count_var.get()})."
        )
        self.start_bot()

    def _run_bot_task(self):
        try:
            logging.info("Initialising.")
            autodori.init()
            logging.info("Initialisation complete.")

            # 直接把 config_data 传给统一的函数，无需再做 if 判断
            autodori.run_task_mode()

            logging.info("Task completed.")
        except Exception as e:
            if autodori.stop_event.is_set():
                logging.info("Task cancelled by user.")
            else:
                logging.error(f"Exception: {e}", exc_info=True)
        finally:
            self.start_button.config(state=tk.NORMAL)
            self.stop_button.config(state=tk.DISABLED)
            self.notebook.tab(1, state='normal')
            self._update_float_button_state()

    def _prevent_resize(self, event):
        """拦截并阻止PanedWindow的尺寸调整事件。"""
        return "break"

    def _update_warnings(self, *args):
        """
        通过改变文字颜色来显示或隐藏警告，而不是从布局中移除它们。
        这可以保持布局的稳定性。
        """
        # 检查 '随机化' 的状态
        if self.human_var.get():
            # 勾选时，设置为红色
            self.human_delay_warning_label.config(foreground='red')
        else:
            # 未勾选时，设置为背景色（隐形）
            self.human_delay_warning_label.config(foreground=self.bg_color)

    def on_app_exit(self):
        """应用程序退出的主处理函数。"""
        logging.info("Application exit requested. Cleaning up resources...")

        # 步骤1：检查并停止正在运行的任务（强制执行一次停止任务函数）
        if self.bot_thread and self.bot_thread.is_alive():
            logging.info("Active task found. Calling stop_bot() before exiting.")
            self.stop_bot()
            self.bot_thread.join(timeout=5)  # 等待任务线程响应停止信号

        # 步骤2：调用后端的最终资源清理函数
        logging.info("Shutting down backend resources (Minitouch, ADB)...")
        autodori.shutdown_resources()

        cache_dir = autodori.data_path("cache")
        if cache_dir.exists() and cache_dir.is_dir():
            try:
                # 使用 rmtree 可以删除非空文件夹
                shutil.rmtree(cache_dir)
                logging.info("Cache folder deleted successfully.")
            except Exception as e:
                logging.error(f"Failed to delete cache folder: {e}")

        # 步骤3：所有清理完成后，销毁主窗口，正式退出
        logging.info("Cleanup complete. Exiting GUI.")
        self.master.destroy()


# --- 资源加载窗口类 ---
class ResourceSplash(tk.Toplevel):
    def __init__(self, master, on_complete_callback):
        super().__init__(master)
        self.master = master
        self.on_complete = on_complete_callback

        self.title("正在检查必要资源...")
        self.geometry("450x150")
        self.resizable(False, False)
        # 居中显示
        self.update_idletasks()
        x = (self.winfo_screenwidth() // 2) - (450 // 2)
        y = (self.winfo_screenheight() // 2) - (150 // 2)
        self.geometry(f"+{x}+{y}")

        # 拦截关闭按钮，防止下载中断
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # UI 控件
        self.status_label = ttk.Label(self, text="正在初始化环境...", font=("", 10))
        self.status_label.pack(pady=(30, 10))

        self.progress = ttk.Progressbar(self, orient=tk.HORIZONTAL, length=350, mode='determinate')
        self.progress.pack()

        # 启动后台下载线程
        threading.Thread(target=self._run_check, daemon=True).start()

    def _on_close(self):
        # 如果用户强制关闭加载窗，则退出整个程序
        self.master.destroy()
        sys.exit(0)

    def _update_ui(self, text, percent):
        """线程安全的UI更新"""
        self.status_label.config(text=text)
        if percent == -1:
            # 无法获取总大小，进入来回弹跳的"忙碌"模式
            if self.progress['mode'] != 'indeterminate':
                self.progress.config(mode='indeterminate')
                self.progress.start(10)
        else:
            if self.progress['mode'] != 'determinate':
                self.progress.stop()
                self.progress.config(mode='determinate')
            self.progress['value'] = percent

    def _run_check(self):
        try:
            # 使用 lambda 结合 master.after 确保跨线程更新 Tkinter UI 不会崩溃
            update_callback = lambda text, percent: self.master.after(0, self._update_ui, text, percent)

            # 调用我们在 resource.py 中写好的函数
            resource.check_and_prepare_resources(progress_callback=update_callback)

            # 等待 0.5 秒让用户看到"就绪"提示，然后触发完成回调
            time.sleep(0.5)
            self.master.after(0, self._finish)
        except Exception as e:
            def show_error():
                messagebox.showerror("资源下载失败", f"无法下载或解压资源：\n{e}\n\n请检查网络连接后重启程序。")
                self.master.destroy()
                sys.exit(1)

            self.master.after(0, show_error)

    def _finish(self):
        self.destroy()  # 销毁加载窗
        self.on_complete()  # 呼出主界面


# --- 程序入口 ---
if __name__ == "__main__":
    if sys.platform == 'win32':
        startupinfo = subprocess.STARTUPINFO()
        startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        subprocess.Popen = partial(subprocess.Popen, startupinfo=startupinfo)

    # `--auto` starts the 全自动一轮 session as soon as the UI is ready, so a
    # scheduled task can run the whole thing unattended.
    AUTO_START = "--auto" in sys.argv

    # A scheduled run must not start on top of an instance that is already
    # open, or the two would fight over the emulator.
    if sys.platform == "win32":
        import ctypes

        _kernel32 = ctypes.windll.kernel32
        _instance_mutex = _kernel32.CreateMutexW(None, False, "Local\\autodoriUI_single_instance")
        if _kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            logging.warning("Another autodoriUI instance is already running; exiting.")
            if not AUTO_START:
                _r = tk.Tk()
                _r.withdraw()
                messagebox.showwarning("autodoriUI", "已经有一个 autodoriUI 在运行了。")
                _r.destroy()
            sys.exit(0)

    # 初始化一个隐藏的根窗口
    root = tk.Tk()
    root.withdraw()


    # 定义资源加载完成后的回调函数
    def launch_main_gui():
        # 显示主窗口并初始化主 GUI
        root.deiconify()
        app = AutodoriGUI(root)
        root.protocol("WM_DELETE_WINDOW", app.on_app_exit)
        if AUTO_START:
            root.after(2500, app.auto_start)


    # 唤起资源检查窗口
    splash = ResourceSplash(root, on_complete_callback=launch_main_gui)

    # 启动主事件循环
    root.mainloop()