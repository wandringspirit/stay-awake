"""Stay Awake - stop Windows from sleeping, right now or on a schedule.

    stay_awake.pyw            open the window (double-click the file)
    stay_awake.pyw --agent    run only the background agent, no window

The window manages a list of "arms" (things that keep the PC awake) saved in
%APPDATA%\\StayAwake\\arms.json. A tiny background agent reads that file and holds
a Windows wake request (SetThreadExecutionState) whenever an armed entry is
active, so schedules keep working after the window is closed.
"""

import ctypes
import hashlib
import json
import os
import subprocess
import sys
import time
import uuid
import winreg
from ctypes import wintypes
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path

APP_NAME = "Stay Awake"
DATA_DIR = Path(os.environ.get("STAY_AWAKE_HOME") or Path(os.environ.get("APPDATA") or Path.home()) / "StayAwake")
ARMS_FILE = DATA_DIR / "arms.json"
LOG_FILE = DATA_DIR / "agent.log"
STOP_FILE = DATA_DIR / "agent.stop"

# One agent and one window per data folder.
_TAG = hashlib.md5(str(DATA_DIR).lower().encode()).hexdigest()[:8]
AGENT_MUTEX = f"Local\\StayAwake.Agent.{_TAG}"
UI_MUTEX = f"Local\\StayAwake.UI.{_TAG}"

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "StayAwakeAgent"
AGENT_POLL_SECONDS = 2

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_DISPLAY_REQUIRED = 0x00000002
ERROR_ALREADY_EXISTS = 183
SYNCHRONIZE = 0x00100000

NO_END = datetime.max  # "active_end" of an arm that never ends on its own
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
ONCE = "Once (on a date)"
CUSTOM = "Custom days"
REPEAT_PRESETS = {
    "Every day": [0, 1, 2, 3, 4, 5, 6],
    "Weekdays (Mon-Fri)": [0, 1, 2, 3, 4],
    "Weekends (Sat-Sun)": [5, 6],
}

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.CreateMutexW.argtypes = [wintypes.LPVOID, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.CreateMutexW.restype = wintypes.HANDLE
kernel32.OpenMutexW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
kernel32.OpenMutexW.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.SetThreadExecutionState.argtypes = [wintypes.DWORD]
kernel32.SetThreadExecutionState.restype = wintypes.DWORD


# --------------------------------------------------------------------------- storage

def file_stamp():
    try:
        st = ARMS_FILE.stat()
        return st.st_mtime_ns, st.st_size
    except FileNotFoundError:
        return None


def load_arms():
    """The saved arms, or None if the file is unreadable right now."""
    try:
        with open(ARMS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    except (OSError, ValueError):
        return None
    arms = data.get("arms") if isinstance(data, dict) else None
    return [a for a in arms if isinstance(a, dict) and "id" in a] if isinstance(arms, list) else []


def save_arms(arms):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ARMS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps({"version": 1, "arms": arms}, indent=2), encoding="utf-8")
    for _ in range(40):  # the agent may be reading the file at this exact moment
        try:
            os.replace(tmp, ARMS_FILE)
            return
        except PermissionError:
            time.sleep(0.05)
    os.replace(tmp, ARMS_FILE)


def new_arm(**fields):
    arm = {
        "id": uuid.uuid4().hex[:10],
        "name": "",
        "kind": "window",
        "enabled": True,
        "screen": True,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    arm.update(fields)
    return arm


def log(message):
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if LOG_FILE.exists() and LOG_FILE.stat().st_size > 512_000:
            LOG_FILE.replace(LOG_FILE.with_name("agent.old.log"))
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S}  {message}\n")
    except OSError:
        pass


# --------------------------------------------------------------------------- schedule logic
#
# Arm kinds:
#   always  - awake until disarmed
#   until   - awake from now until arm["until"]
#   window  - awake between arm["start"] and arm["end"] (HH:MM; an end at or before
#             the start runs past midnight), either once on arm["date"] or weekly
#             on arm["days"] (0 = Monday).

def parse_hm(text):
    h, m = text.split(":")
    return dtime(int(h), int(m))


def starts_on(arm, day):
    if arm.get("repeat") == "once":
        return arm.get("date") == day.isoformat()
    return day.weekday() in arm.get("days", [])


def window_span(arm, day):
    start = datetime.combine(day, parse_hm(arm["start"]))
    end = datetime.combine(day, parse_hm(arm["end"]))
    if end <= start:
        end += timedelta(days=1)
    return start, end


def active_end(arm, now):
    """When `arm` stops keeping the PC awake, if it is doing so at `now`.

    Returns a datetime (NO_END if it never stops on its own), or None if inactive.
    """
    if not arm.get("enabled"):
        return None
    kind = arm.get("kind")
    if kind == "always":
        return NO_END
    if kind == "until":
        until = datetime.fromisoformat(arm["until"])
        return until if now < until else None
    if kind == "window":
        ends = []
        for day in (now.date() - timedelta(days=1), now.date()):
            if starts_on(arm, day):
                start, end = window_span(arm, day)
                if start <= now < end:
                    ends.append(end)
        return max(ends) if ends else None
    return None


def next_start(arm, now):
    """The next time a window arm begins, or None."""
    if not arm.get("enabled") or arm.get("kind") != "window":
        return None
    for offset in range(8):
        day = now.date() + timedelta(days=offset)
        if starts_on(arm, day):
            start, _ = window_span(arm, day)
            if start > now:
                return start
    return None


def is_expired(arm, now):
    kind = arm.get("kind")
    if kind == "until":
        return now >= datetime.fromisoformat(arm["until"])
    if kind == "window" and arm.get("repeat") == "once":
        return now >= window_span(arm, date.fromisoformat(arm["date"]))[1]
    return False


def safe(fn, *args, default=None):
    """Run a schedule helper, treating a malformed arm as inactive."""
    try:
        return fn(*args)
    except (KeyError, ValueError, TypeError, AttributeError):
        return default


def short_date(d):
    return f"{d:%a} {d.day} {d:%b}"


def fmt_when(dt, now):
    days = (dt.date() - now.date()).days
    if days == 0:
        label = "today"
    elif days == 1:
        label = "tomorrow"
    elif 1 < days < 7:
        label = f"{dt:%a}"
    else:
        label = short_date(dt)
    return f"{label} {dt:%H:%M}"


def days_label(days):
    days = sorted(set(days))
    for label, preset in REPEAT_PRESETS.items():
        if days == preset:
            return label.split(" (")[0]
    return ", ".join(DAY_NAMES[d] for d in days) or "No days"


def describe(arm, now):
    kind = arm.get("kind")
    if kind == "always":
        return "Indefinitely"
    if kind == "until":
        return f"Until {fmt_when(datetime.fromisoformat(arm['until']), now)}"
    span = f"{arm['start']} - {arm['end']}"
    if parse_hm(arm["end"]) <= parse_hm(arm["start"]):
        span += " (next day)"
    if arm.get("repeat") == "once":
        return f"{short_date(date.fromisoformat(arm['date']))}, {span}"
    return f"{days_label(arm.get('days', []))}, {span}"


def default_name(arm):
    kind = arm.get("kind")
    if kind == "always":
        return "Indefinitely"
    if kind == "until":
        until = datetime.fromisoformat(arm["until"])
        return f"Until {short_date(until)} {until:%H:%M}"
    return describe(arm, datetime.now())


# --------------------------------------------------------------------------- agent

def run_agent():
    """Hold a wake request whenever an armed entry is active. Runs until agent.stop appears."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    ctypes.set_last_error(0)
    mutex = kernel32.CreateMutexW(None, False, AGENT_MUTEX)
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        return  # another agent is already on duty
    STOP_FILE.unlink(missing_ok=True)
    log(f"Agent started (pid {os.getpid()})")
    arms, stamp, last_state = [], None, None
    try:
        while not STOP_FILE.exists():
            new_stamp = file_stamp()
            if new_stamp != stamp:
                loaded = load_arms()
                if loaded is not None:
                    arms, stamp = loaded, new_stamp

            now = datetime.now()
            active = [a for a in arms if safe(active_end, a, now)]
            keep_system = bool(active)
            keep_display = any(a.get("screen") for a in active)
            kernel32.SetThreadExecutionState(
                ES_CONTINUOUS
                | (ES_SYSTEM_REQUIRED if keep_system else 0)
                | (ES_DISPLAY_REQUIRED if keep_display else 0)
            )

            names = sorted(str(a.get("name") or a["id"]) for a in active)
            state = (keep_system, keep_display, names)
            if state != last_state:
                if keep_system:
                    screen = "screen on" if keep_display else "screen may turn off"
                    log(f"Keeping awake ({screen}): {', '.join(names)}")
                else:
                    log("Released - normal sleep settings apply")
                last_state = state
            time.sleep(AGENT_POLL_SECONDS)
    finally:
        kernel32.SetThreadExecutionState(ES_CONTINUOUS)
        STOP_FILE.unlink(missing_ok=True)
        log("Agent stopped")
        kernel32.CloseHandle(mutex)


def agent_running():
    handle = kernel32.OpenMutexW(SYNCHRONIZE, False, AGENT_MUTEX)
    if handle:
        kernel32.CloseHandle(handle)
        return True
    return False


def agent_command():
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return [str(windowless if windowless.exists() else exe), str(Path(__file__).resolve()), "--agent"]


def start_agent():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.unlink(missing_ok=True)
    flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    kwargs = dict(cwd=str(DATA_DIR), close_fds=True, stdin=subprocess.DEVNULL,
                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:  # escape the launching console's job so closing it doesn't kill the agent
        subprocess.Popen(agent_command(), creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB, **kwargs)
    except OSError:
        subprocess.Popen(agent_command(), creationflags=flags, **kwargs)


def stop_agent():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    STOP_FILE.touch()


def autostart_command():
    return subprocess.list2cmdline(agent_command())


def autostart_value():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_VALUE)[0]
    except FileNotFoundError:
        return None


def set_autostart(enabled):
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if enabled:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass


# --------------------------------------------------------------------------- UI

import tkinter as tk  # noqa: E402  (the agent never needs Tk)
from tkinter import font as tkfont, messagebox, ttk  # noqa: E402

THEME = {
    "awake": ("#E6F4EA", "#0F5132", "#1E9E4A"),   # background, text, dot
    "idle": ("#EEF1F4", "#33404D", "#8B96A1"),
    "warn": ("#FFF3E0", "#7A4100", "#E58A00"),
}
ROW_TAGS = {
    "active": {"background": "#E6F4EA", "foreground": "#0F5132"},
    "armed": {"foreground": "#1B2733"},
    "disarmed": {"foreground": "#8B96A1"},
    "expired": {"foreground": "#A9B1B9"},
    "broken": {"foreground": "#B42318"},
}


def round_up(dt, minutes):
    dt = dt.replace(second=0, microsecond=0)
    return dt + timedelta(minutes=-dt.minute % minutes)


def hm_text(hours, minutes):
    try:
        h, m = int(hours), int(minutes)
    except ValueError:
        raise ValueError("Times need numbers, e.g. 09:30.") from None
    if not (0 <= h < 24 and 0 <= m < 60):
        raise ValueError("Hours go 0-23 and minutes 0-59.")
    return f"{h:02d}:{m:02d}"


def parse_date(text):
    try:
        return date.fromisoformat(text.strip())
    except ValueError:
        raise ValueError("Dates look like 2026-10-08 (year-month-day).") from None


def dot_icon(color, size):
    img = tk.PhotoImage(width=size, height=size)
    c, r = (size - 1) / 2, size / 2 - 1
    for y in range(size):
        dy = y - c
        if abs(dy) <= r:
            half = (r * r - dy * dy) ** 0.5
            img.put(color, to=(int(round(c - half)), y, int(round(c + half)) + 1, y + 1))
    return img


class App:
    def __init__(self, root):
        self.root = root
        self.arms = load_arms() or []
        self.stamp = file_stamp()
        self.icon_state = None
        self.banner_action = None

        root.title(APP_NAME)
        self.setup_style()
        root.geometry(f"{self.px(900)}x{self.px(600)}")
        root.minsize(self.px(760), self.px(480))
        self.icons = {k: dot_icon(v[2], self.px(32)) for k, v in THEME.items()}

        self.build_banner()
        body = ttk.Frame(root, padding=(self.px(14), self.px(10), self.px(14), self.px(10)))
        body.pack(fill="both", expand=True)
        self.build_quick(body)
        self.build_list(body)
        self.build_footer(body)

        self.agent_launched = None
        if not agent_running():
            self.launch_agent()
        if autostart_value() not in (None, autostart_command()):
            self.try_set_autostart(True)  # the script moved; keep the sign-in entry pointing at it
        self.tick()

    # ---- setup
    def px(self, n):
        return int(round(n * self.scale))

    def setup_style(self):
        self.scale = self.root.winfo_fpixels("1i") / 96
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont"):
            tkfont.nametofont(name).configure(family="Segoe UI", size=10)
        style = ttk.Style(self.root)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        self.bg = style.lookup("TFrame", "background") or "#F0F0F0"
        self.root.configure(background=self.bg)
        style.configure("Treeview", rowheight=self.px(30))
        style.configure("Treeview.Heading", font=("Segoe UI Semibold", 10))
        style.configure("TLabelframe.Label", font=("Segoe UI Semibold", 10))
        style.configure("Hint.TLabel", foreground="#5F6B76")
        style.configure("Error.TLabel", foreground="#B42318")
        style.configure("Preview.TLabel", foreground="#0F5132")
        style.configure("Empty.TLabel", foreground="#5F6B76", background="white")

    def build_banner(self):
        self.banner = tk.Frame(self.root, padx=self.px(18), pady=self.px(14))
        self.banner.pack(fill="x")
        d = self.px(16)
        self.banner_dot = tk.Canvas(self.banner, width=d, height=d, highlightthickness=0)
        self.banner_dot.pack(side="left", anchor="n", pady=(self.px(6), 0))
        self.banner_oval = self.banner_dot.create_oval(1, 1, d - 1, d - 1, width=0)
        text = tk.Frame(self.banner)
        text.pack(side="left", fill="x", expand=True, padx=(self.px(12), 0))
        self.banner_title = tk.Label(text, font=("Segoe UI Semibold", 14), anchor="w")
        self.banner_title.pack(fill="x")
        self.banner_sub = tk.Label(text, font=("Segoe UI", 10), anchor="w", justify="left")
        self.banner_sub.pack(fill="x")
        self.banner_btn = ttk.Button(self.banner, command=lambda: self.banner_action and self.banner_action())
        self.banner_text_frame = text

    def build_quick(self, parent):
        q = ttk.LabelFrame(parent, text=" Keep awake right now ", padding=(self.px(10), self.px(4), self.px(10), self.px(10)))
        q.pack(fill="x")
        for label, minutes in (("30 min", 30), ("1 hour", 60), ("2 hours", 120), ("4 hours", 240)):
            ttk.Button(q, text=label, width=9, command=lambda m=minutes, l=label: self.quick_for(m, l)).pack(side="left", padx=(0, self.px(6)))
        ttk.Button(q, text="Until a time...", command=self.quick_until).pack(side="left", padx=(0, self.px(6)))
        ttk.Button(q, text="Indefinitely", command=self.quick_forever).pack(side="left")
        self.quick_screen = tk.BooleanVar(value=True)
        ttk.Checkbutton(q, text="Keep screen on", variable=self.quick_screen).pack(side="right")

    def build_list(self, parent):
        lf = ttk.LabelFrame(parent, text=" Arms (schedules) ", padding=(self.px(10), self.px(4), self.px(10), self.px(10)))
        lf.pack(fill="both", expand=True, pady=(self.px(10), 0))

        table = ttk.Frame(lf)
        table.pack(fill="both", expand=True)
        cols = (("state", "State", 110, "w", False), ("name", "Name", 190, "w", True),
                ("schedule", "Schedule", 270, "w", True), ("timing", "Ends / next", 150, "w", False),
                ("screen", "Screen", 70, "center", False))
        self.tree = ttk.Treeview(table, columns=[c[0] for c in cols], show="headings", selectmode="extended", height=8)
        for key, title, width, anchor, stretch in cols:
            self.tree.heading(key, text=title, anchor=anchor)
            self.tree.column(key, width=self.px(width), minwidth=self.px(60), anchor=anchor, stretch=stretch)
        for tag, opts in ROW_TAGS.items():
            self.tree.tag_configure(tag, **opts)
        scroll = ttk.Scrollbar(table, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        self.empty = ttk.Label(self.tree, style="Empty.TLabel", justify="center",
                               text="Nothing armed yet.\nUse a quick button above, or + New schedule below.")

        self.tree.bind("<<TreeviewSelect>>", lambda e: self.update_buttons())
        self.tree.bind("<Double-1>", self.on_double_click)
        self.tree.bind("<Return>", lambda e: self.edit_selected())
        self.tree.bind("<Delete>", lambda e: self.delete_selected())
        self.tree.bind("<space>", lambda e: self.toggle_selected())
        self.tree.bind("<Button-3>", self.on_right_click)

        self.menu = tk.Menu(self.root, tearoff=False)
        self.menu.add_command(label="Edit...", command=self.edit_selected)
        self.menu.add_command(label="Arm", command=lambda: self.set_enabled(True))
        self.menu.add_command(label="Disarm", command=lambda: self.set_enabled(False))
        self.menu.add_separator()
        self.menu.add_command(label="Delete", command=self.delete_selected)

        bar = ttk.Frame(lf)
        bar.pack(fill="x", pady=(self.px(8), 0))
        gap = (0, self.px(6))
        ttk.Button(bar, text="+ New schedule...", command=self.new_schedule).pack(side="left", padx=gap)
        self.btn_edit = ttk.Button(bar, text="Edit...", command=self.edit_selected)
        self.btn_arm = ttk.Button(bar, text="Arm", command=lambda: self.set_enabled(True))
        self.btn_disarm = ttk.Button(bar, text="Disarm", command=lambda: self.set_enabled(False))
        self.btn_delete = ttk.Button(bar, text="Delete", command=self.delete_selected)
        for b in (self.btn_edit, self.btn_arm, self.btn_disarm, self.btn_delete):
            b.pack(side="left", padx=gap)
        self.btn_disarm_all = ttk.Button(bar, text="Disarm all", command=self.disarm_all)
        self.btn_disarm_all.pack(side="right")
        self.btn_clear = ttk.Button(bar, text="Clear expired", command=self.clear_expired)
        self.btn_clear.pack(side="right", padx=gap)

    def build_footer(self, parent):
        foot = ttk.Frame(parent)
        foot.pack(fill="x", pady=(self.px(10), 0))
        d = self.px(10)
        self.agent_dot = tk.Canvas(foot, width=d, height=d, highlightthickness=0, background=self.bg)
        self.agent_dot.pack(side="left")
        self.agent_oval = self.agent_dot.create_oval(1, 1, d - 1, d - 1, width=0)
        self.agent_label = ttk.Label(foot)
        self.agent_label.pack(side="left", padx=(self.px(6), self.px(10)))
        self.agent_btn = ttk.Button(foot, command=self.toggle_agent)
        self.agent_btn.pack(side="left")
        ttk.Button(foot, text="Open log", command=self.open_log).pack(side="left", padx=(self.px(6), 0))
        self.autostart_var = tk.BooleanVar(value=autostart_value() is not None)
        ttk.Checkbutton(foot, text="Start agent when I sign in to Windows", variable=self.autostart_var,
                        command=lambda: self.try_set_autostart(self.autostart_var.get())).pack(side="right")
        ttk.Label(parent, style="Hint.TLabel",
                  text="You can close this window - the agent keeps working in the background. "
                       "Closing the lid or pressing Sleep still follows your Windows settings.").pack(fill="x", pady=(self.px(6), 0))

    # ---- refresh loop
    def tick(self):
        try:
            stamp = file_stamp()
            if stamp != self.stamp:
                loaded = load_arms()
                if loaded is not None:
                    self.arms, self.stamp = loaded, stamp
            now = datetime.now()
            stale = [a for a in self.arms if a.get("quick") and safe(is_expired, a, now)]
            if stale:
                self.arms = [a for a in self.arms if a not in stale]
                self.save(refresh=False)
            self.refresh()
        finally:
            self.root.after(1500, self.tick)

    def refresh(self):
        now = datetime.now()
        self.refresh_rows(now)
        running = agent_running()
        self.refresh_banner(now, running)
        self.refresh_footer(running)
        self.update_buttons()

    def row_for(self, arm, now):
        name = arm.get("name") or arm.get("id")
        schedule = safe(describe, arm, now)
        if schedule is None:
            return ("Invalid", name, "Can't read this entry - edit or delete it", "", ""), "broken"
        screen = "On" if arm.get("screen") else "-"
        end = safe(active_end, arm, now)
        if safe(is_expired, arm, now):
            return ("Expired", name, schedule, "-", screen), "expired"
        if not arm.get("enabled"):
            return ("Disarmed", name, schedule, "-", screen), "disarmed"
        if end:
            timing = "No end" if end == NO_END else f"Ends {fmt_when(end, now)}"
            return ("● Active", name, schedule, timing, screen), "active"
        nxt = safe(next_start, arm, now)
        return ("Armed", name, schedule, f"Next {fmt_when(nxt, now)}" if nxt else "-", screen), "armed"

    def refresh_rows(self, now):
        existing = set(self.tree.get_children())
        for i, arm in enumerate(self.arms):
            values, tag = self.row_for(arm, now)
            if arm["id"] in existing:
                self.tree.item(arm["id"], values=values, tags=(tag,))
                self.tree.move(arm["id"], "", i)
            else:
                self.tree.insert("", i, iid=arm["id"], values=values, tags=(tag,))
        for iid in existing - {a["id"] for a in self.arms}:
            self.tree.delete(iid)
        if self.arms:
            self.empty.place_forget()
        else:
            self.empty.place(relx=0.5, rely=0.55, anchor="center")

    def refresh_banner(self, now, running):
        active = [(a, safe(active_end, a, now)) for a in self.arms]
        active = [(a, end) for a, end in active if end]
        if not running and self.agent_starting():
            state = "idle"
            title = "Starting the background agent..."
            sub = "This only takes a moment."
            action = None
        elif not running:
            state = "warn"
            title = "Background agent isn't running"
            sub = "Your arms can't keep the laptop awake until it's started."
            action = ("Start agent", self.toggle_agent)
        elif active:
            state = "awake"
            names = [str(a.get("name") or a["id"]) for a, _ in active]
            who = names[0] if len(names) == 1 else f"{names[0]} + {len(names) - 1} more"
            latest = max(end for _, end in active)
            until = "until you disarm it" if latest == NO_END else f"until {fmt_when(latest, now)}"
            screen = "screen stays on" if any(a.get("screen") for a, _ in active) else "screen may turn off"
            title = "Keeping your laptop awake"
            sub = f"{who}  ·  {until}  ·  {screen}"
            action = ("Stop now", self.stop_now)
        else:
            state = "idle"
            title = "Normal sleep settings"
            upcoming = [(safe(next_start, a, now), a) for a in self.arms]
            upcoming = [(t, a) for t, a in upcoming if t]
            if upcoming:
                t, a = min(upcoming, key=lambda x: x[0])
                sub = f"Next: {a.get('name') or a['id']}  ·  starts {fmt_when(t, now)}"
            else:
                sub = "Nothing is armed right now."
            action = None

        bg, fg, dot = THEME[state]
        for w in (self.banner, self.banner_text_frame, self.banner_dot):
            w.configure(background=bg)
        self.banner_title.configure(text=title, background=bg, foreground=fg)
        self.banner_sub.configure(text=sub, background=bg, foreground=fg)
        self.banner_dot.itemconfigure(self.banner_oval, fill=dot)
        if action:
            self.banner_action = action[1]
            self.banner_btn.configure(text=action[0])
            self.banner_btn.pack(side="right", anchor="center")
        else:
            self.banner_action = None
            self.banner_btn.pack_forget()
        if state != self.icon_state:
            self.root.iconphoto(True, self.icons[state])
            self.icon_state = state

    def refresh_footer(self, running):
        starting = not running and self.agent_starting()
        self.agent_dot.itemconfigure(self.agent_oval, fill=THEME["awake" if running else "idle" if starting else "warn"][2])
        self.agent_label.configure(text="Background agent: " + ("running" if running else "starting..." if starting else "stopped"))
        self.agent_btn.configure(text="Stop agent" if running else "Start agent")
        self.agent_btn.state(["disabled"] if starting else ["!disabled"])

    def update_buttons(self):
        now = datetime.now()
        sel = self.selected()
        usable = [a for a in sel if not safe(is_expired, a, now, default=False)]

        def enable(button, on):
            button.state(["!disabled"] if on else ["disabled"])

        enable(self.btn_edit, len(sel) == 1)
        enable(self.btn_arm, any(not a.get("enabled") for a in usable))
        enable(self.btn_disarm, any(a.get("enabled") for a in sel))
        enable(self.btn_delete, bool(sel))
        enable(self.btn_disarm_all, any(a.get("enabled") for a in self.arms))
        enable(self.btn_clear, any(safe(is_expired, a, now, default=False) for a in self.arms))

    # ---- actions
    def save(self, refresh=True):
        try:
            save_arms(self.arms)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't save your arms:\n{exc}", parent=self.root)
        self.stamp = file_stamp()
        if refresh:
            self.refresh()

    def selected(self):
        ids = set(self.tree.selection())
        return [a for a in self.arms if a["id"] in ids]

    def replace_quick(self, arm):
        """Quick arms share one slot: a new one replaces the previous."""
        self.arms = [arm] + [a for a in self.arms if not a.get("quick")]
        self.save()

    def quick_for(self, minutes, label):
        until = round_up(datetime.now() + timedelta(minutes=minutes), 1)
        self.replace_quick(new_arm(kind="until", name=f"Quick · {label}", quick=True,
                                   until=until.isoformat(timespec="minutes"), screen=self.quick_screen.get()))

    def quick_forever(self):
        self.replace_quick(new_arm(kind="always", name="Quick · indefinitely", quick=True,
                                   screen=self.quick_screen.get()))

    def quick_until(self):
        ArmEditor(self, preset={"kind": "until", "quick": True, "screen": self.quick_screen.get()})

    def new_schedule(self):
        ArmEditor(self)

    def edit_selected(self):
        sel = self.selected()
        if len(sel) == 1:
            ArmEditor(self, arm=sel[0])

    def set_enabled(self, value):
        for arm in self.selected():
            arm["enabled"] = value
        self.save()

    def toggle_selected(self):
        sel = self.selected()
        if sel:
            self.set_enabled(not all(a.get("enabled") for a in sel))

    def delete_selected(self):
        sel = self.selected()
        if not sel:
            return
        what = f"\"{sel[0].get('name')}\"" if len(sel) == 1 else f"these {len(sel)} arms"
        if messagebox.askyesno(APP_NAME, f"Delete {what}?", parent=self.root):
            ids = {a["id"] for a in sel}
            self.arms = [a for a in self.arms if a["id"] not in ids]
            self.save()

    def disarm_all(self):
        self.arms = [a for a in self.arms if not a.get("quick")]
        for arm in self.arms:
            arm["enabled"] = False
        self.save()

    def stop_now(self):
        """Disarm whatever is keeping the laptop awake at this moment."""
        now = datetime.now()
        active = [a for a in self.arms if safe(active_end, a, now)]
        self.arms = [a for a in self.arms if not (a.get("quick") and a in active)]
        for arm in active:
            arm["enabled"] = False
        self.save()

    def clear_expired(self):
        now = datetime.now()
        self.arms = [a for a in self.arms if not safe(is_expired, a, now, default=False)]
        self.save()

    def launch_agent(self):
        start_agent()
        self.agent_launched = time.monotonic()

    def agent_starting(self):
        return self.agent_launched is not None and time.monotonic() - self.agent_launched < 20

    def toggle_agent(self):
        if agent_running():
            stop_agent()
            self.agent_launched = None
        else:
            self.launch_agent()
        self.root.after(400, self.refresh)
        self.root.after(2500, self.refresh)

    def try_set_autostart(self, enabled):
        try:
            set_autostart(enabled)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"Couldn't change the sign-in setting:\n{exc}", parent=self.root)
        self.autostart_var.set(autostart_value() is not None)

    def open_log(self):
        if LOG_FILE.exists():
            os.startfile(LOG_FILE)
        else:
            messagebox.showinfo(APP_NAME, "Nothing logged yet.", parent=self.root)

    def on_double_click(self, event):
        if self.tree.identify_row(event.y):
            self.edit_selected()

    def on_right_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if row not in self.tree.selection():
            self.tree.selection_set(row)
        sel = self.selected()
        self.menu.entryconfigure("Edit...", state="normal" if len(sel) == 1 else "disabled")
        self.menu.entryconfigure("Arm", state="normal" if any(not a.get("enabled") for a in sel) else "disabled")
        self.menu.entryconfigure("Disarm", state="normal" if any(a.get("enabled") for a in sel) else "disabled")
        self.menu.tk_popup(event.x_root, event.y_root)


class ArmEditor(tk.Toplevel):
    """Dialog for creating or editing one arm."""

    def __init__(self, app, arm=None, preset=None):
        super().__init__(app.root)
        self.app, self.arm = app, arm
        src = dict(arm or preset or {})
        self.quick = bool(src.get("quick"))
        self.withdraw()
        self.title("Edit arm" if arm else ("Keep awake until..." if self.quick else "New schedule"))
        self.transient(app.root)
        self.resizable(False, False)
        self.configure(background=app.bg)

        now = datetime.now()
        start, end = src.get("start", "09:00"), src.get("end", "18:00")
        days = sorted(src.get("days", REPEAT_PRESETS["Weekdays (Mon-Fri)"]))
        until = safe(datetime.fromisoformat, src.get("until")) or round_up(now + timedelta(hours=1), 15)
        if src.get("repeat") == "once":
            repeat = ONCE
        else:
            repeat = next((label for label, d in REPEAT_PRESETS.items() if d == days), CUSTOM)

        self.name = tk.StringVar(value="" if self.quick and not arm else src.get("name", ""))
        self.kind = tk.StringVar(value=src.get("kind", "window"))
        self.sh, self.sm = tk.StringVar(value=start[:2]), tk.StringVar(value=start[3:])
        self.eh, self.em = tk.StringVar(value=end[:2]), tk.StringVar(value=end[3:])
        self.repeat = tk.StringVar(value=repeat)
        self.date = tk.StringVar(value=src.get("date", now.date().isoformat()))
        self.day_vars = [tk.BooleanVar(value=i in days) for i in range(7)]
        self.udate = tk.StringVar(value=until.date().isoformat())
        self.uh, self.um = tk.StringVar(value=f"{until:%H}"), tk.StringVar(value=f"{until:%M}")
        self.screen = tk.BooleanVar(value=src.get("screen", True))
        self.enabled = tk.BooleanVar(value=src.get("enabled", True))
        self.preview = tk.StringVar()

        px = app.px
        self.body = ttk.Frame(self, padding=px(18))
        self.body.pack(fill="both", expand=True)
        self.body.columnconfigure(1, weight=1)
        self._row = 0

        name_entry = ttk.Entry(self.body, textvariable=self.name, width=42)
        self.add_row("Name", name_entry)
        ttk.Label(self.body, text="(optional)", style="Hint.TLabel").grid(row=0, column=2, sticky="w", padx=(px(6), 0))

        kinds = ttk.Frame(self.body)
        for value, text in (("window", "During a time window - once on a date, or repeating on chosen days"),
                            ("until", "From now until a specific date and time"),
                            ("always", "Indefinitely, until I disarm it")):
            ttk.Radiobutton(kinds, text=text, value=value, variable=self.kind,
                            command=self.update_visibility).pack(anchor="w", pady=px(2))
        self.add_row("Keep awake", kinds, sticky="nw", pady=(px(10), px(6)))

        times = ttk.Frame(self.body)
        self.time_box(times, self.sh, self.sm).pack(side="left")
        ttk.Label(times, text="to").pack(side="left", padx=px(10))
        self.time_box(times, self.eh, self.em).pack(side="left")
        self.overnight = ttk.Label(times, style="Hint.TLabel")
        self.overnight.pack(side="left", padx=(px(10), 0))
        self.window_rows = [self.add_row("From", times)]

        combo = ttk.Combobox(self.body, textvariable=self.repeat, state="readonly", width=24,
                             values=[ONCE, *REPEAT_PRESETS, CUSTOM])
        combo.bind("<<ComboboxSelected>>", self.on_repeat)
        self.window_rows.append(self.add_row("Repeat", combo))
        self.date_rows = [self.add_row("On", self.date_box(self.date))]
        day_frame = ttk.Frame(self.body)
        for i, name in enumerate(DAY_NAMES):
            ttk.Checkbutton(day_frame, text=name, variable=self.day_vars[i],
                            command=self.on_day).pack(side="left", padx=(0, px(6)))
        self.days_rows = [self.add_row("Days", day_frame)]

        self.until_rows = [self.add_row("Date", self.date_box(self.udate)),
                           self.add_row("Time", self.time_box(self.body, self.uh, self.um))]

        ttk.Separator(self.body).grid(row=self.next_row(), column=0, columnspan=3, sticky="we", pady=px(12))
        ttk.Checkbutton(self.body, text="Keep the screen on too (most reliable on this kind of laptop)",
                        variable=self.screen).grid(row=self.next_row(), column=0, columnspan=3, sticky="w")
        ttk.Checkbutton(self.body, text="Armed", variable=self.enabled).grid(
            row=self.next_row(), column=0, columnspan=3, sticky="w", pady=(px(4), 0))
        self.preview_label = ttk.Label(self.body, textvariable=self.preview, wraplength=px(500), style="Preview.TLabel")
        self.preview_label.grid(row=self.next_row(), column=0, columnspan=3, sticky="w", pady=(px(12), 0))

        buttons = ttk.Frame(self.body)
        buttons.grid(row=self.next_row(), column=0, columnspan=3, sticky="e", pady=(px(16), 0))
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right")
        ttk.Button(buttons, text="Save", default="active", command=self.save).pack(side="right", padx=(0, px(8)))

        watched = [self.name, self.kind, self.sh, self.sm, self.eh, self.em, self.repeat, self.date,
                   self.udate, self.uh, self.um, self.screen, self.enabled, *self.day_vars]
        for var in watched:
            var.trace_add("write", lambda *_: self.update_preview())
        self.bind("<Return>", self.save)
        self.bind("<Escape>", lambda e: self.destroy())

        self.update_visibility()
        self.place_over_parent()
        self.deiconify()
        self.grab_set()
        name_entry.focus_set()

    # ---- layout helpers
    def next_row(self):
        self._row += 1
        return self._row - 1

    def add_row(self, label, widget, sticky="w", pady=None):
        pady = pady if pady is not None else (self.app.px(4), self.app.px(4))
        r = self.next_row()
        lbl = ttk.Label(self.body, text=label)
        lbl.grid(row=r, column=0, sticky=sticky, padx=(0, self.app.px(14)), pady=pady)
        widget.grid(row=r, column=1, columnspan=2 if label != "Name" else 1, sticky="w", pady=pady)
        return lbl, widget

    def time_box(self, parent, hours, minutes):
        f = ttk.Frame(parent)
        ttk.Spinbox(f, from_=0, to=23, wrap=True, width=3, format="%02.0f", justify="center",
                    textvariable=hours).pack(side="left")
        ttk.Label(f, text=":").pack(side="left", padx=self.app.px(3))
        ttk.Spinbox(f, from_=0, to=59, increment=5, wrap=True, width=3, format="%02.0f", justify="center",
                    textvariable=minutes).pack(side="left")
        return f

    def date_box(self, var):
        f = ttk.Frame(self.body)
        ttk.Entry(f, textvariable=var, width=12).pack(side="left")
        today = date.today()
        ttk.Button(f, text="Today", width=8, command=lambda: var.set(today.isoformat())).pack(side="left", padx=(self.app.px(6), 0))
        ttk.Button(f, text="Tomorrow", width=9, command=lambda: var.set((today + timedelta(days=1)).isoformat())).pack(side="left", padx=(self.app.px(4), 0))
        ttk.Label(f, text="YYYY-MM-DD", style="Hint.TLabel").pack(side="left", padx=(self.app.px(8), 0))
        return f

    def place_over_parent(self):
        self.update_idletasks()
        parent = self.app.root
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_reqwidth()) // 2
        y = parent.winfo_rooty() + max(0, (parent.winfo_height() - self.winfo_reqheight()) // 3)
        self.geometry(f"+{max(0, x)}+{max(0, y)}")

    def show(self, rows, visible):
        for row in rows:
            for widget in row:
                widget.grid() if visible else widget.grid_remove()

    # ---- events
    def update_visibility(self):
        kind = self.kind.get()
        once = self.repeat.get() == ONCE
        self.show(self.window_rows, kind == "window")
        self.show(self.date_rows, kind == "window" and once)
        self.show(self.days_rows, kind == "window" and not once)
        self.show(self.until_rows, kind == "until")
        self.update_preview()

    def on_repeat(self, _event=None):
        days = REPEAT_PRESETS.get(self.repeat.get())
        if days is not None:
            for i, var in enumerate(self.day_vars):
                var.set(i in days)
        self.update_visibility()

    def on_day(self):
        days = [i for i, v in enumerate(self.day_vars) if v.get()]
        self.repeat.set(next((label for label, d in REPEAT_PRESETS.items() if d == days), CUSTOM))

    def collect(self):
        """The arm described by the form as (fields, None), or (None, error message)."""
        now = datetime.now()
        kind = self.kind.get()
        arm = {"kind": kind, "screen": self.screen.get(), "enabled": self.enabled.get()}
        try:
            if kind == "window":
                arm["start"] = hm_text(self.sh.get(), self.sm.get())
                arm["end"] = hm_text(self.eh.get(), self.em.get())
                if self.repeat.get() == ONCE:
                    arm.update(repeat="once", date=parse_date(self.date.get()).isoformat())
                    if is_expired(arm, now):
                        return None, "That window has already ended - pick a later date or time."
                else:
                    days = [i for i, v in enumerate(self.day_vars) if v.get()]
                    if not days:
                        return None, "Pick at least one day."
                    arm.update(repeat="weekly", days=days)
            elif kind == "until":
                until = datetime.combine(parse_date(self.udate.get()),
                                         parse_hm(hm_text(self.uh.get(), self.um.get())))
                if until <= now:
                    return None, "That time has already passed."
                arm["until"] = until.isoformat(timespec="minutes")
        except ValueError as exc:
            return None, str(exc)
        arm["name"] = self.name.get().strip() or (f"Quick · {default_name(arm).lower()}" if self.quick else default_name(arm))
        return arm, None

    def update_preview(self):
        overnight = False
        try:
            overnight = parse_hm(hm_text(self.eh.get(), self.em.get())) <= parse_hm(hm_text(self.sh.get(), self.sm.get()))
        except ValueError:
            pass
        self.overnight.configure(text="ends the next day" if overnight else "")

        arm, error = self.collect()
        if error:
            self.preview_label.configure(style="Error.TLabel")
            self.preview.set(error)
            return
        now = datetime.now()
        if not arm["enabled"]:
            tail = "Saved disarmed - it won't do anything until you arm it."
        else:
            end = active_end(arm, now)
            nxt = next_start(arm, now)
            if end:
                tail = "Starts right away" + ("" if end == NO_END else f" and runs until {fmt_when(end, now)}") + "."
            elif nxt:
                tail = f"Next starts {fmt_when(nxt, now)}."
            else:
                tail = ""
        self.preview_label.configure(style="Preview.TLabel")
        self.preview.set(f"{describe(arm, now)}.  {tail}")

    def save(self, _event=None):
        arm, error = self.collect()
        if error:
            self.update_preview()
            self.bell()
            return
        app = self.app
        if self.arm:
            keep = {k: self.arm[k] for k in ("id", "created", "quick") if k in self.arm}
            updated = {**keep, **arm}
            for i, existing in enumerate(app.arms):
                if existing["id"] == updated["id"]:
                    app.arms[i] = updated
                    break
            else:
                app.arms.append(updated)
            app.save()
        elif self.quick:
            app.replace_quick(new_arm(quick=True, **arm))
        else:
            app.arms.append(new_arm(**arm))
            app.save()
        self.destroy()


def run_ui():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("StayAwake.App")
    except (AttributeError, OSError):
        pass

    ctypes.set_last_error(0)
    ui_mutex = kernel32.CreateMutexW(None, False, UI_MUTEX)  # held until the window closes
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        hwnd = user32.FindWindowW("TkTopLevel", APP_NAME)
        if hwnd:
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
        return

    root = tk.Tk()
    App(root)
    root.mainloop()
    kernel32.CloseHandle(ui_mutex)


if __name__ == "__main__":
    if "--agent" in sys.argv[1:]:
        run_agent()
    else:
        run_ui()
