# -*- coding: utf-8 -*-
"""开机启动管理器 v2 —— 让开机自启一眼看清、一键收拾

产品目标（v2 重新排过优先级）：
  1. 一眼看清  开机到底拉起了多少东西，哪几项是白占资源的
  2. 一键收拾  自动挑出「无效项 / 更新器 / 可疑项」，勾选确认后批量禁用
  3. 绝不闯祸  「禁用」只是搬进备份区，永远能一键撤销；删除要二次确认
  4. 看得舒服  现代扁平界面：卡片布局、圆角按钮、彩色状态标签

管理范围：
  1. 注册表       HKCU / HKLM 的 ...\\CurrentVersion\\Run(Once)，含 32 位视图
  2. 启动文件夹   当前用户 与 所有用户 的「启动」目录
  3. 计划任务     登录时 / 开机时 触发的任务
（Windows 服务不碰，避免误伤系统。）

用法：
  python startup_manager.py                图形界面
  python startup_manager.py --list         命令行列出所有启动项
  python startup_manager.py --enable-all   还原所有被本软件禁用过的项
"""
import base64
import csv
import ctypes
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, font as tkfont, messagebox, ttk
import webbrowser

try:
    import winreg
except ImportError:
    winreg = None

APP_NAME = "开机启动管理器"
APP_VERSION = "v2.1"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "StartupManager"
DISABLED_FILE = DATA_DIR / "disabled.json"
DISABLED_FILES = DATA_DIR / "disabled_files"
LOG_FILE = DATA_DIR / "log.txt"
HISTORY_FILE = DATA_DIR / "history.json"    # 对比报告用的状态快照

HIVES = {
    "HKCU": (winreg.HKEY_CURRENT_USER if winreg else None),
    "HKLM": (winreg.HKEY_LOCAL_MACHINE if winreg else None),
}
HIVE_LABEL = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}

RUN_SOURCES = [
    {"key": "HKCU", "sub": r"Software\Microsoft\Windows\CurrentVersion\Run", "label": "当前用户 Run", "admin": False},
    {"key": "HKCU", "sub": r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "label": "当前用户 RunOnce", "admin": False},
    {"key": "HKCU", "sub": r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "label": "当前用户 Run (32位)", "admin": False},
    {"key": "HKLM", "sub": r"Software\Microsoft\Windows\CurrentVersion\Run", "label": "所有用户 Run", "admin": True},
    {"key": "HKLM", "sub": r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "label": "所有用户 RunOnce", "admin": True},
    {"key": "HKLM", "sub": r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "label": "所有用户 Run (32位)", "admin": True},
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)

# 智能分类用的关键词
UPDATER_WORDS = ("update", "updater", "upgrade", "升级", "更新", "setup", "install", "patch",
                 "jucheck", "jusched", "360zip", "360safe", "qihoo", "googleupdate", "edgeupdate",
                 "wpsupdate", "sogou", "kuwo", "updatecheck", "autoupdate")
KEEP_WORDS = ("ctfmon", "realtek", "rtkaud", "hdaudio", "audio", "defender", "security",
              "antivirus", "输入法", "ime", "synaptics", "elantech", "touchpad", "igfx",
              "intel graphics", "sandboxie", "安全中心")
SUSPECT_HINTS = ("\\temp\\", "\\tmp\\", "\\appdata\\local\\temp", "\\downloads\\")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    try:
        script = os.path.abspath(sys.argv[0])
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}'.strip(), None, 1)
        return int(rc) > 32
    except Exception:
        return False


def _decode(raw: bytes) -> str:
    if not raw:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run_cmd(args, timeout=60):
    try:
        p = subprocess.run(list(args), capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except Exception as exc:
        return False, str(exc)
    return p.returncode == 0, (_decode(p.stdout) + _decode(p.stderr)).strip()


def run_ps(script: str, timeout=90) -> str:
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return ""
    ok, out = run_cmd([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-Command", script], timeout=timeout)
    return out if ok else ""


def expand(text: str) -> str:
    return os.path.expandvars((text or "").strip())


def exe_of(command: str) -> str:
    s = expand(command)
    if not s:
        return ""
    if s.startswith('"'):
        end = s.find('"', 1)
        return s[1:end] if end > 0 else s[1:]
    m = re.match(r"^(\S+?\.(?:exe|com|bat|cmd|vbs|ps1|lnk|dll|msc|jar|py))(?:\s|$)", s, re.I)
    if m:
        return m.group(1)
    m = re.match(r'^rundll32(?:\.exe)?\s+(?:"([^"]+)"|(\S+))', s, re.I)
    if m:
        return m.group(1) or m.group(2)
    return s.split()[0]


def path_exists(path: str) -> bool:
    p = expand(path).strip().strip('"')
    if not p:
        return False
    low = p.lower()
    if low.startswith("\\systemroot\\"):
        p = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), p[len("\\systemroot\\"):])
    elif low.startswith("\\\\?\\"):
        p = p[4:]
    try:
        return os.path.exists(p)
    except Exception:
        return False


def human_size(path: str) -> str:
    try:
        if os.path.isfile(path):
            size = os.path.getsize(path)
    except Exception:
        return "—"
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    return f"{size / 1048576:.1f} MB"


def in_windows_dir(path: str) -> bool:
    windir = os.environ.get("SystemRoot", r"C:\Windows").lower()
    return expand(path).lower().startswith(windir)


def open_file_location(path: str) -> None:
    p = expand(path).strip().strip('"')
    if path_exists(p):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
    elif p:
        parent = os.path.dirname(os.path.normpath(p))
        if os.path.isdir(parent):
            os.startfile(parent)  # noqa: S606
        else:
            messagebox.showwarning(APP_NAME, f"找不到这个文件：\n{p}")
    else:
        messagebox.showwarning(APP_NAME, "这一项没有可打开的文件路径。")


def regedit_goto(hive: str, sub: str) -> None:
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER,
                                r"Software\Microsoft\Windows\CurrentVersion\Applets\Regedit",
                                0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, "LastKey", 0, winreg.REG_SZ, f"{HIVE_LABEL.get(hive, hive)}\\{sub}")
    except Exception:
        pass
    try:
        subprocess.Popen(["regedit"])
    except Exception as exc:
        messagebox.showerror(APP_NAME, f"打不开注册表编辑器：{exc}")


def sanitize(name: str, max_len: int = 60) -> str:
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "item").strip(" .")
    return (s[:max_len] or "item")


# --------------------------------------------------------------------------- #
# 备份区
# --------------------------------------------------------------------------- #
def load_store() -> dict:
    try:
        data = json.loads(DISABLED_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("items"), dict):
            return data
    except Exception:
        pass
    return {"items": {}}


def save_store(store: dict) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = DISABLED_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(store, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, DISABLED_FILE)


def legacy_stores() -> list:
    """旧版本可能把数据写进了商店版(MSIX) Python 的重定向目录，这里把它们找出来。

    微软商店版 Python 会把对 %LOCALAPPDATA% 的写入重定向到包私有目录，
    所以同一个软件用 .py 跑和用 exe 跑，看到的备份区不是同一个地方。
    """
    base = Path(os.environ.get("LOCALAPPDATA") or Path.home())
    packages = base / "Packages"
    found = []
    if packages.is_dir():
        try:
            for pkg in packages.glob("PythonSoftwareFoundation.Python.*"):
                found.append(pkg / "LocalCache" / "Local" / "StartupManager")
        except OSError:
            pass
    return [p for p in found if p.is_dir() and (p / "disabled.json").is_file()
            and p.resolve() != DATA_DIR.resolve()]


def migrate_legacy_stores() -> int:
    """把旧位置数据并进正式备份区（每个旧目录只并一次），返回并入条数。"""
    folders = legacy_stores()
    if not folders:
        return 0
    store = load_store()
    merged = 0
    for folder in folders:
        try:
            old = json.loads((folder / "disabled.json").read_text(encoding="utf-8"))
        except Exception:
            continue
        items = old.get("items") if isinstance(old, dict) else None
        if not isinstance(items, dict):
            continue
        for uid, entry in items.items():
            if uid not in store["items"]:
                store["items"][uid] = entry
                merged += 1
        try:  # 并入后把旧文件改名，避免下次重复扫
            shutil.copy2(folder / "disabled.json", folder / "disabled.json.migrated")
            (folder / "disabled.json").unlink()
            old_log = folder / "log.txt"
            if old_log.is_file():
                with LOG_FILE.open("a", encoding="utf-8") as dst, \
                        old_log.open("r", encoding="utf-8", errors="replace") as src:
                    dst.write(src.read())
        except Exception:
            pass
    if merged:
        save_store(store)
        log_action(f"从旧位置并入 {merged} 条禁用记录，备份区改用 {DATA_DIR}")
    return merged


def log_action(text: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")
    except Exception:
        pass


def _dump_value(value):
    if isinstance(value, bytes):
        return {"b64": base64.b64encode(value).decode("ascii")}
    if isinstance(value, (list, tuple)):
        return {"list": [str(v) for v in value]}
    if isinstance(value, bool):
        return {"int": int(value)}
    if isinstance(value, int):
        return {"int": value}
    return {"str": str(value)}


def _load_value(data):
    if "b64" in data:
        return base64.b64decode(data["b64"])
    if "list" in data:
        return list(data["list"])
    if "int" in data:
        return int(data["int"])
    return data.get("str", "")


# --------------------------------------------------------------------------- #
# 启动项数据
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    uid: str
    kind: str                 # registry / folder / task
    name: str
    command: str
    source: str
    location: str
    enabled: bool
    need_admin: bool = False
    managed: bool = False
    extra: dict = field(default_factory=dict)

    @property
    def target(self) -> str:
        if self.kind == "folder":
            return self.extra.get("resolved") or self.command
        return exe_of(self.command)

    @property
    def missing(self) -> bool:
        t = self.target
        if not t:
            return False
        if self.kind == "task" and t.lower() in ("cmd", "cmd.exe"):
            return False
        return not path_exists(t)

    @property
    def suspicious(self) -> bool:
        low = expand(self.command).lower()
        return any(h in low for h in SUSPECT_HINTS)

    @property
    def status_text(self) -> str:
        if self.enabled:
            return "已启用"
        return "已禁用" if self.managed else "已禁用(系统)"


def classify(item: Item) -> tuple:
    """(分类, 建议, 原因) —— 产品经理视角的「这一项要不要留」。"""
    if item.suspicious:
        return "可疑", "建议禁用", "程序放在临时目录/下载目录里，不像正经软件的安装位置"
    if item.target and item.missing:
        return "无效项", "建议禁用", "它要启动的程序已经不存在了，纯粹是开机时白跑一趟"
    low = f"{item.name} {item.command}".lower()
    if item.kind == "task" and item.location.lower().startswith("\\microsoft\\"):
        return "系统组件", "慎动", "Windows 自带的任务，禁掉可能影响系统功能"
    if any(w in low for w in KEEP_WORDS) or in_windows_dir(item.target):
        return "系统组件", "建议保留", "系统或硬件相关组件（输入法、声卡、安全中心等），禁了容易出怪问题"
    if any(w in low for w in UPDATER_WORDS):
        return "更新器", "可禁用", "软件更新/升级器，禁用后软件照常用，只是不再自动更新"
    if item.kind == "task":
        return "软件自启", "看需要", "登录时被计划任务拉起的程序"
    return "软件自启", "看需要", "跟着系统一起启动的软件，用不到就可以禁用"


CATEGORY_STYLE = {
    "无效项": ("#dc2626", "#fdeceb"),
    "可疑": ("#dc2626", "#fdeceb"),
    "更新器": ("#b45309", "#fdf1e3"),
    "系统组件": ("#475569", "#eef1f5"),
    "软件自启": ("#2563eb", "#e8f0fe"),
}
ADVICE_STYLE = {
    "建议禁用": ("#dc2626", "#fdeceb"),
    "可禁用": ("#b45309", "#fdf1e3"),
    "建议保留": ("#16803c", "#e7f7ee"),
    "慎动": ("#475569", "#eef1f5"),
    "看需要": ("#2563eb", "#e8f0fe"),
}


# --------------------------------------------------------------------------- #
# 扫描
# --------------------------------------------------------------------------- #
def scan_registry() -> list:
    items = []
    for src in RUN_SOURCES:
        hive = HIVES.get(src["key"])
        if hive is None:
            continue
        try:
            with winreg.OpenKey(hive, src["sub"], 0, winreg.KEY_READ) as key:
                idx = 0
                while True:
                    try:
                        name, value, vtype = winreg.EnumValue(key, idx)
                    except OSError:
                        break
                    idx += 1
                    if isinstance(value, str):
                        cmd = value
                    elif isinstance(value, (list, tuple)) and value:
                        cmd = str(value[0])
                    else:
                        cmd = str(value)
                    items.append(Item(
                        uid=f"registry|{src['key']}|{src['sub']}|{name}",
                        kind="registry", name=name, command=cmd,
                        source=src["label"],
                        location=f"{HIVE_LABEL[src['key']]}\\{src['sub']}",
                        enabled=True, need_admin=src["admin"],
                        extra={"hive": src["key"], "sub": src["sub"], "value_type": vtype},
                    ))
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return items


def startup_folders() -> list:
    folders = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append({"path": Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup",
                        "label": "当前用户 启动文件夹", "admin": False})
    programdata = os.environ.get("ProgramData")
    if programdata:
        folders.append({"path": Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\Startup",
                        "label": "所有用户 启动文件夹", "admin": True})
    return folders


def _resolve_shortcuts(paths: list) -> dict:
    paths = [str(p) for p in paths if str(p).lower().endswith(".lnk")]
    if not paths:
        return {}
    quoted = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    script = ("$sh = New-Object -ComObject WScript.Shell; "
              f"@({quoted}) | ForEach-Object {{ $t = ''; "
              "try { $t = $sh.CreateShortcut($_).TargetPath } catch {}; \"$_`t$t\" }")
    out = run_ps(script, timeout=45)
    result = {}
    for line in out.splitlines():
        lnk, _, target = line.partition("\t")
        if lnk:
            result[lnk.strip()] = target.strip()
    return result


def scan_folders() -> list:
    items = []
    for folder in startup_folders():
        path = Path(folder["path"])
        if not path.is_dir():
            continue
        try:
            entries = sorted(p for p in path.iterdir() if p.name.lower() != "desktop.ini")
        except (PermissionError, OSError):
            continue
        resolved = _resolve_shortcuts(entries) if entries else {}
        for entry in entries:
            items.append(Item(
                uid=f"folder|{path}|{entry.name}",
                kind="folder", name=entry.name, command=str(entry),
                source=folder["label"], location=str(path),
                enabled=True, need_admin=folder["admin"],
                extra={"path": str(entry), "resolved": resolved.get(str(entry), "")},
            ))
    return items


PS_TASKS = r"""
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$ErrorActionPreference = 'SilentlyContinue'
Get-ScheduledTask | Where-Object {
  $t = $_
  @($t.Triggers | Where-Object {
    $_.CimClass.CimClassName -eq 'MSFT_TaskLogonTrigger' -or $_.CimClass.CimClassName -eq 'MSFT_TaskBootTrigger'
  }).Count -gt 0
} | ForEach-Object {
  $act = @($_.Actions)[0]
  $exe = if ($act) { $act.Execute } else { '' }
  $arg = if ($act) { $act.Arguments } else { '' }
  @($_.State, $_.TaskPath, $_.TaskName, $exe, $arg) -join "`t"
}
"""


def scan_tasks(include_system: bool = False) -> list:
    items = []
    for line in run_ps(PS_TASKS).splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        state, task_path, task_name = parts[0].strip(), parts[1].strip(), parts[2].strip()
        execute = parts[3].strip() if len(parts) > 3 else ""
        args = parts[4].strip() if len(parts) > 4 else ""
        full = f"{task_path}{task_name}"
        if not include_system and task_path.lower().startswith("\\microsoft\\"):
            continue
        if execute.strip('" ').lower() in ("", "by user disabled", "no action"):
            command = ""
        elif execute.startswith('"'):
            command = f"{execute} {args}".strip()
        else:
            command = f'"{execute}" {args}'.strip()
        items.append(Item(
            uid=f"task|{full}",
            kind="task", name=task_name, command=command,
            source="计划任务（登录/开机触发）", location=full,
            enabled=(state.lower() != "disabled"),
            need_admin=task_path.lower().startswith("\\microsoft\\"),
            extra={"full_name": full, "state": state},
        ))
    return items


def items_from_store(store: dict = None) -> list:
    """被本软件禁用过的项，还原成 Item（用于显示和还原）。"""
    store = store if store is not None else load_store()
    items = []
    for uid, entry in store["items"].items():
        kind = entry.get("kind")
        if kind == "registry":
            items.append(Item(uid=uid, kind="registry", name=entry.get("name", ""),
                              command=entry.get("command", ""), source=entry.get("source", "注册表"),
                              location=entry.get("location", ""), enabled=False, managed=True,
                              need_admin=entry.get("need_admin", False),
                              extra={"hive": entry.get("hive"), "sub": entry.get("sub")}))
        elif kind == "folder":
            items.append(Item(uid=uid, kind="folder", name=entry.get("name", ""),
                              command=entry.get("path", ""), source=entry.get("source", "启动文件夹"),
                              location=entry.get("location", ""), enabled=False, managed=True,
                              need_admin=entry.get("need_admin", False),
                              extra={"saved": entry.get("saved", ""), "path": entry.get("path", ""),
                                     "resolved": entry.get("resolved", "")}))
        elif kind == "task":
            items.append(Item(uid=uid, kind="task", name=entry.get("name", ""),
                              command=entry.get("command", ""), source="计划任务（登录/开机触发）",
                              location=entry.get("location", ""), enabled=False, managed=True,
                              need_admin=entry.get("need_admin", False),
                              extra={"full_name": entry.get("full_name", ""), "state": "Disabled"}))
    return items


def scan_all(include_system_tasks: bool = False) -> list:
    items = []
    if winreg is not None:
        items.extend(scan_registry())
    items.extend(scan_folders())
    items.extend(scan_tasks(include_system_tasks))
    seen = {it.uid for it in items}
    for it in items_from_store():
        if it.uid not in seen:
            items.append(it)
    return items


# --------------------------------------------------------------------------- #
# 报告：状态快照 & 按软件分组
# --------------------------------------------------------------------------- #
def load_history() -> dict:
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        if isinstance(data, dict) and isinstance(data.get("snapshots"), list):
            return data
    except Exception:
        pass
    return {"snapshots": []}


def snapshot_counts(items: list) -> dict:
    """把当前启动项汇总成一份「计数器」快照。"""
    cats = {}
    enabled = bad = 0
    for it in items:
        category, advice, _r = classify(it)
        cats[category] = cats.get(category, 0) + 1
        if it.enabled:
            enabled += 1
            if advice in ("建议禁用", "可禁用"):
                bad += 1
    return {"enabled": enabled, "disabled": len(items) - enabled,
            "bad": bad, "total": len(items), "cats": cats}


def record_snapshot(items: list, label: str) -> None:
    """把当前状态记进 history.json，作为对比报告的一个时间点。"""
    h = load_history()
    snap = snapshot_counts(items)
    snap["t"] = time.strftime("%Y-%m-%d %H:%M:%S")
    snap["label"] = label
    h["snapshots"].append(snap)
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = HISTORY_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(h, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, HISTORY_FILE)
    except Exception:
        pass


def app_family(item: Item) -> str:
    """把启动项归到「哪一个软件/产品」名下，用于按软件分组。"""
    target = item.target
    if item.kind in ("registry", "folder") and target:
        parts = [p for p in Path(target).parts if p]
        skip = {"bin", "x64", "x86", "ext", "app", "application", "update", "updater",
                "version", "config", "runtime", "windows", "system32", "syswow64",
                "program files", "program files (x86)", "common files"}
        for part in reversed(parts[:-1]):        # 安装目录：从文件上一级往上找产品名
            if part.lower() not in skip and "." not in part:
                return part
        return parts[-2] if len(parts) > 1 else (Path(target).stem or item.name)
    if item.kind == "task":
        base = Path(item.location).stem or item.name
        short = re.sub(r"(Update|Updater|Task|Logon|Login|Cleanup|Check|Scheduler|AutoLaunch|"
                       r"Boot|Start|Detect|Sync|Save|Queue|Service|System|User|Machine|Core|"
                       r"Retry|Report).*$", "", base, flags=re.I)
        return short.strip(" _-") or base
    return item.name


# --------------------------------------------------------------------------- #
# 动作
# --------------------------------------------------------------------------- #
def disable_item(item: Item, store: dict) -> tuple:
    if item.kind == "registry":
        if winreg is None:
            return False, "当前系统不支持注册表操作"
        hive = HIVES[item.extra["hive"]]
        try:
            with winreg.OpenKey(hive, item.extra["sub"], 0, winreg.KEY_READ) as key:
                value, vtype = winreg.QueryValueEx(key, item.name)
        except OSError as exc:
            return False, f"读取注册表失败：{exc}"
        try:
            with winreg.OpenKey(hive, item.extra["sub"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.DeleteValue(key, item.name)
        except PermissionError:
            return False, "需要管理员权限（请点右上角「以管理员身份重启」）"
        except OSError as exc:
            return False, f"删除注册表值失败：{exc}"
        store["items"][item.uid] = {
            "kind": "registry", "name": item.name, "command": item.command,
            "source": item.source, "location": item.location,
            "hive": item.extra["hive"], "sub": item.extra["sub"],
            "value": _dump_value(value), "value_type": vtype,
            "need_admin": item.need_admin, "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return True, f"已禁用：{item.name}"

    if item.kind == "folder":
        src = Path(item.extra["path"])
        if not src.exists():
            return False, "启动文件不存在，可能已被删除"
        DISABLED_FILES.mkdir(parents=True, exist_ok=True)
        dest = DISABLED_FILES / f"{sanitize(item.name, 40)}__{abs(hash(item.uid)) % 100000}.bak"
        try:
            shutil.move(str(src), str(dest))
        except PermissionError:
            return False, "需要管理员权限（请点右上角「以管理员身份重启」）"
        except Exception as exc:
            return False, f"移动文件失败：{exc}"
        store["items"][item.uid] = {
            "kind": "folder", "name": item.name, "command": item.command,
            "source": item.source, "location": item.location,
            "path": str(src), "saved": str(dest), "resolved": item.extra.get("resolved", ""),
            "need_admin": item.need_admin, "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return True, f"已禁用：{item.name}"

    if item.kind == "task":
        ok, out = run_cmd(["schtasks", "/change", "/tn", item.extra["full_name"], "/disable"])
        if not ok:
            return False, f"禁用计划任务失败：{out[:160]}"
        store["items"][item.uid] = {
            "kind": "task", "name": item.name, "command": item.command,
            "location": item.location, "full_name": item.extra["full_name"],
            "need_admin": item.need_admin, "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return True, f"已禁用计划任务：{item.name}"

    return False, "未知的启动项类型"


def enable_item(item: Item, store: dict) -> tuple:
    entry = store["items"].get(item.uid)

    if item.kind == "registry":
        if not entry:
            return False, "备份里没有这一项的原始数据，无法自动还原"
        hive = HIVES[entry["hive"]]
        try:
            with winreg.CreateKeyEx(hive, entry["sub"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, entry["name"], 0, entry["value_type"], _load_value(entry["value"]))
        except PermissionError:
            return False, "需要管理员权限（请点右上角「以管理员身份重启」）"
        except OSError as exc:
            return False, f"写入注册表失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已启用：{item.name}"

    if item.kind == "folder":
        if not entry:
            return False, "备份里没有这一项的原始数据，无法自动还原"
        src, dest = Path(entry["saved"]), Path(entry["path"])
        if not src.exists():
            return False, "备份文件已丢失，无法还原"
        target = dest if not dest.exists() else dest.with_name(f"{dest.stem} (还原){dest.suffix}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
        except PermissionError:
            return False, "需要管理员权限（请点右上角「以管理员身份重启」）"
        except Exception as exc:
            return False, f"还原文件失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已启用：{item.name}"

    if item.kind == "task":
        ok, out = run_cmd(["schtasks", "/change", "/tn", item.extra["full_name"], "/enable"])
        if not ok:
            return False, f"启用计划任务失败：{out[:160]}"
        store["items"].pop(item.uid, None)
        return True, f"已启用计划任务：{item.name}"

    return False, "未知的启动项类型"


def delete_item(item: Item, store: dict) -> tuple:
    if item.kind == "registry":
        if item.enabled:
            hive = HIVES[item.extra["hive"]]
            try:
                with winreg.OpenKey(hive, item.extra["sub"], 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, item.name)
            except PermissionError:
                return False, "需要管理员权限（请点右上角「以管理员身份重启」）"
            except OSError as exc:
                return False, f"删除注册表值失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已删除：{item.name}"

    if item.kind == "folder":
        raw = item.extra.get("path") if item.enabled else store["items"].get(item.uid, {}).get("saved", "")
        target = Path(raw) if raw else None
        try:
            if target and target.exists():
                target.unlink()
        except PermissionError:
            return False, "需要管理员权限（请点右上角「以管理员身份重启」）"
        except Exception as exc:
            return False, f"删除文件失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已删除：{item.name}"

    if item.kind == "task":
        ok, out = run_cmd(["schtasks", "/delete", "/tn", item.extra["full_name"], "/f"])
        if not ok:
            return False, f"删除计划任务失败：{out[:160]}"
        store["items"].pop(item.uid, None)
        return True, f"已删除计划任务：{item.name}"

    return False, "未知的启动项类型"


def add_registry_run(name: str, command: str, all_users: bool) -> tuple:
    if winreg is None:
        return False, "当前系统不支持注册表操作"
    hive = winreg.HKEY_LOCAL_MACHINE if all_users else winreg.HKEY_CURRENT_USER
    sub = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.CreateKeyEx(hive, sub, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    except PermissionError:
        return False, "写所有用户的启动项需要管理员权限"
    except OSError as exc:
        return False, f"写入注册表失败：{exc}"
    return True, f"已新增：{name}"


def create_shortcut(lnk_path: Path, target: str, args: str = "", workdir: str = "") -> tuple:
    lnk = str(lnk_path).replace("'", "''")
    tgt = expand(target).replace("'", "''")
    arg = (args or "").replace("'", "''")
    wd = (workdir or os.path.dirname(expand(target))).replace("'", "''")
    script = ("$sh = New-Object -ComObject WScript.Shell; "
              f"$s = $sh.CreateShortcut('{lnk}'); $s.TargetPath = '{tgt}'; "
              f"$s.Arguments = '{arg}'; $s.WorkingDirectory = '{wd}'; $s.Save()")
    exe = shutil.which("powershell") or "powershell"
    ok, out = run_cmd([exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                       "-Command", script], timeout=60)
    if not ok:
        return False, f"创建快捷方式失败：{out[:160]}"
    return True, "已创建快捷方式"


def add_folder_run(name: str, target: str, args: str, all_users: bool) -> tuple:
    candidates = [f for f in startup_folders() if f["admin"] == all_users]
    if not candidates:
        return False, "找不到对应的启动文件夹"
    if all_users and not is_admin():
        return False, "写所有用户的启动文件夹需要管理员权限"
    folder = Path(candidates[0]["path"])
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return False, "需要管理员权限"
    lnk = folder / f"{sanitize(name)}.lnk"
    if lnk.exists():
        return False, f"启动文件夹里已经有同名项：{lnk.name}"
    return create_shortcut(lnk, target, args)


# --------------------------------------------------------------------------- #
# 界面配色与小部件
# --------------------------------------------------------------------------- #
C = {
    "bg": "#f4f6fa",
    "card": "#ffffff",
    "border": "#e3e7ef",
    "text": "#1f2430",
    "muted": "#6b7280",
    "accent": "#2563eb",
    "accent_dark": "#1d4ed8",
    "accent_soft": "#e8f0fe",
    "ok": "#16803c",
    "warn": "#b45309",
    "danger": "#dc2626",
    "hover": "#f1f4fa",
}
F = ("Microsoft YaHei UI", 10)
F_BOLD = ("Microsoft YaHei UI", 10, "bold")
F_SMALL = ("Microsoft YaHei UI", 9)
F_TITLE = ("Microsoft YaHei UI", 16, "bold")
F_NUM = ("Microsoft YaHei UI", 24, "bold")
F_MONO = ("Consolas", 9)


def round_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw):
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class RoundButton(tk.Canvas):
    """圆角按钮（tkinter 没有原生圆角，用 Canvas 画一个）。"""

    MODES = {
        "ghost":   {"bg": C["card"],       "fg": C["text"],    "border": C["border"],  "hover": C["hover"]},
        "primary": {"bg": C["accent"],     "fg": "#ffffff",    "border": C["accent"],  "hover": C["accent_dark"]},
        "soft":    {"bg": C["accent_soft"], "fg": C["accent"], "border": C["accent_soft"], "hover": "#dbe7fd"},
        "danger":  {"bg": C["card"],       "fg": C["danger"], "border": "#f0c9c5",    "hover": "#fdeceb"},
        "danger_solid": {"bg": C["danger"], "fg": "#ffffff",   "border": C["danger"],  "hover": "#b91c1c"},
        "success": {"bg": C["ok"],         "fg": "#ffffff",    "border": C["ok"],      "hover": "#116232"},
    }

    def __init__(self, master, text="", command=None, *, mode="ghost", padx=14, height=34,
                 radius=9, font=None, width=None, bg=None, tooltip=None):
        self.text = text
        self.command = command
        self.mode = mode
        self.radius = radius
        self.font = font or F
        self.colors = self.MODES.get(mode, self.MODES["ghost"])
        self._enabled = True
        self._hover = False
        self._pressed = False
        self._bg = bg or C["card"]
        w = width or (tkfont.Font(font=self.font).measure(text) + padx * 2)
        super().__init__(master, width=w, height=height, bd=0, highlightthickness=0, bg=self._bg)
        self._pw, self._ph = w, height
        self._draw()
        for seq, fn in (("<Enter>", self._on_enter), ("<Leave>", self._on_leave),
                        ("<ButtonPress-1>", self._on_press), ("<ButtonRelease-1>", self._on_release)):
            self.bind(seq, fn)
        if tooltip:
            self._tooltip = tooltip

    # -- 绘制 --
    def _draw(self):
        self.delete("all")
        col = self.colors
        fill = col["hover"] if (self._hover and self._enabled) else col["bg"]
        outline = col["border"]
        if not self._enabled:
            fill = "#f3f4f6"
            outline = "#e5e7eb"
        offset = 1 if (self._pressed and self._enabled) else 0
        round_rect(self, 0.5, 0.5 + offset, self._pw - 0.5, self._ph - 0.5 + offset, self.radius,
                   fill=fill, outline=outline)
        if self.text:
            fg = col["fg"] if self._enabled else "#9ca3af"
            self.create_text(self._pw / 2, self._ph / 2 + offset, text=self.text, font=self.font, fill=fg)

    def set_text(self, text, width=None):
        self.text = text
        new_w = width or (tkfont.Font(font=self.font).measure(text) + 28)
        self._pw = new_w
        self.configure(width=new_w)
        self._draw()

    def set_enabled(self, enabled: bool):
        self._enabled = bool(enabled)
        self.configure(cursor="hand2" if enabled else "arrow")
        self._draw()

    # -- 事件 --
    def _on_enter(self, _e):
        self._hover = True
        self.configure(cursor="hand2" if self._enabled else "arrow")
        self._draw()

    def _on_leave(self, _e):
        self._hover = False
        self._pressed = False
        self._draw()

    def _on_press(self, _e):
        if self._enabled:
            self._pressed = True
            self._draw()

    def _on_release(self, _e):
        was = self._pressed
        self._pressed = False
        self._draw()
        if was and self._enabled and self.command:
            self.command()


class Pill(tk.Canvas):
    """小圆角标签。"""

    def __init__(self, master, text="", fg="#2563eb", bg="#e8f0fe", bg_parent=None, font=None):
        self.font = font or F_SMALL
        w = tkfont.Font(font=self.font).measure(text) + 18
        super().__init__(master, width=w, height=22, bd=0, highlightthickness=0,
                         bg=bg_parent or C["card"])
        self.text, self.fg, self.bg = text, fg, bg
        self._pw = w
        self._draw()

    def _draw(self):
        self.delete("all")
        round_rect(self, 0.5, 0.5, self._pw - 0.5, 21.5, 10, fill=self.bg, outline=self.bg)
        self.create_text(self._pw / 2, 11, text=self.text, font=self.font, fill=self.fg)

    def set(self, text, fg=None, bg=None):
        self.text = text
        if fg:
            self.fg = fg
        if bg:
            self.bg = bg
        w = tkfont.Font(font=self.font).measure(text) + 18
        self._pw = w
        self.configure(width=w)
        self._draw()


class StatCard(tk.Frame):
    """顶部统计卡片，点击等于筛选。"""

    def __init__(self, master, label, accent, command=None):
        super().__init__(master, bg=C["card"], highlightbackground=C["border"],
                         highlightthickness=1, cursor="hand2" if command else "")
        self.command = command
        self.accent = accent
        self.bar = tk.Frame(self, bg=accent, width=4)
        self.bar.pack(side="left", fill="y")
        inner = tk.Frame(self, bg=C["card"])
        inner.pack(side="left", fill="both", expand=True, padx=14, pady=9)
        self.value_lbl = tk.Label(inner, text="0", bg=C["card"], fg=C["text"], font=F_NUM)
        self.value_lbl.pack(anchor="w")
        self.label_lbl = tk.Label(inner, text=label, bg=C["card"], fg=C["muted"], font=F_SMALL)
        self.label_lbl.pack(anchor="w")
        self._parts = [self, inner, self.value_lbl, self.label_lbl, self.bar]
        for widget in self._parts:
            widget.bind("<Button-1>", self._click)
            widget.bind("<Enter>", self._enter)
            widget.bind("<Leave>", self._leave)

    def _click(self, _e):
        if self.command:
            self.command()

    def _paint(self, color):
        try:
            self.configure(bg=color)
            for widget in self._parts[1:4]:
                widget.configure(bg=color)
        except tk.TclError:
            pass

    def _enter(self, _e):
        self._paint("#f8fafd")

    def _leave(self, _e):
        self._paint(C["card"])

    def set_value(self, value):
        self.value_lbl.configure(text=str(value))


class ChipGroup(tk.Frame):
    """一排可切换的筛选胶囊。"""

    def __init__(self, master, values, on_change=None, bg=None):
        super().__init__(master, bg=bg or C["bg"])
        self.values = list(values)
        self.on_change = on_change
        self.current = self.values[0]
        self.buttons = {}
        for value in self.values:
            btn = RoundButton(self, text=value, mode="ghost", height=30, radius=15,
                              padx=12, command=lambda v=value: self.select(v), bg=bg or C["bg"])
            btn.pack(side="left", padx=(0, 6))
            self.buttons[value] = btn
        self._paint()

    def _paint(self):
        for value, btn in self.buttons.items():
            if value == self.current:
                btn.mode = "primary"
                btn.colors = RoundButton.MODES["primary"]
            else:
                btn.mode = "ghost"
                btn.colors = RoundButton.MODES["ghost"]
            btn._draw()

    def select(self, value):
        if value not in self.buttons:
            return
        self.current = value
        self._paint()
        if self.on_change:
            self.on_change(value)

    def get(self):
        return self.current


class ScrollFrame(tk.Frame):
    """可滚动容器。"""

    def __init__(self, master, bg=None):
        super().__init__(master, bg=bg or C["card"])
        self.canvas = tk.Canvas(self, bg=bg or C["card"], highlightthickness=0, bd=0)
        self.vsb = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.vsb.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.vsb.pack(side="right", fill="y")
        self.body = tk.Frame(self.canvas, bg=bg or C["card"])
        self._win = self.canvas.create_window((0, 0), window=self.body, anchor="nw")
        self.body.bind("<Configure>",
                       lambda _e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(self._win, width=e.width))
        self.canvas.bind_all("<MouseWheel>", self._wheel)

    def _wheel(self, event):
        try:
            if self.winfo_containing(event.x_root, event.y_root) is None:
                return
            self.canvas.yview_scroll(int(-event.delta / 120), "units")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# 主界面
# --------------------------------------------------------------------------- #
class StartupManagerGUI:
    def __init__(self, root, migrated: int = 0):
        self.root = root
        self.migrated = migrated
        self.items = []
        self.by_uid = {}
        self.rows = {}
        self.scanning = False
        self.sort_col = "name"
        self.sort_desc = False
        self.last_batch = None
        self.group_mode = tk.BooleanVar(value=False)
        self.groups = {}
        self._seeded = False
        self._pending_label = None
        self._dots = 0
        self.include_system = tk.BooleanVar(value=False)
        self.search_text = tk.StringVar(value="")

        root.title(f"{APP_NAME} {APP_VERSION}")
        root.geometry("1240x780")
        root.minsize(1060, 660)
        root.configure(bg=C["bg"])

        self._setup_style()
        self._build_ui()
        self.refresh()
        self.root.after(300, self._refresh_admin_badge)

    # ------------------------------------------------------------ 样式 --
    def _setup_style(self):
        style = ttk.Style()
        try:
            style.theme_use("clam")     # clam 才能完全自定义颜色
        except tk.TclError:
            pass
        style.configure(".", font=F, background=C["bg"], foreground=C["text"])
        style.configure("Modern.Treeview", background=C["card"], fieldbackground=C["card"],
                        foreground=C["text"], borderwidth=0, rowheight=34, font=F)
        style.layout("Modern.Treeview", [("Modern.Treeview.treearea", {"sticky": "nswe"})])
        style.configure("Modern.Treeview.Heading", background="#f7f8fb", foreground=C["muted"],
                        relief="flat", borderwidth=0, font=F_BOLD, padding=8)
        style.map("Modern.Treeview.Heading", background=[("active", "#eef1f7")])
        style.map("Modern.Treeview", background=[("selected", C["accent_soft"])],
                  foreground=[("selected", "#123a86")])
        style.configure("Vertical.TScrollbar", background="#dde2ec", troughcolor=C["card"],
                        bordercolor=C["card"], arrowcolor=C["muted"], relief="flat")

    # -------------------------------------------------------------- UI --
    def _build_ui(self):
        root = self.root
        root.columnconfigure(0, weight=1)
        root.rowconfigure(3, weight=1)

        # ① 顶部标题栏
        header = tk.Frame(root, bg=C["bg"])
        header.grid(row=0, column=0, sticky="ew", padx=18, pady=(16, 10))
        left = tk.Frame(header, bg=C["bg"])
        left.pack(side="left")
        tk.Label(left, text="开机启动管理器", bg=C["bg"], fg=C["text"], font=F_TITLE).pack(anchor="w")
        self.subtitle = tk.Label(left, text="正在准备…", bg=C["bg"], fg=C["muted"], font=F_SMALL)
        self.subtitle.pack(anchor="w")
        right = tk.Frame(header, bg=C["bg"])
        right.pack(side="right")
        self.btn_optimize = RoundButton(right, "✨ 一键优化", command=self.on_optimize,
                                        mode="primary", height=36, padx=18)
        self.btn_optimize.pack(side="right", padx=(8, 0))
        self.btn_refresh = RoundButton(right, "↻ 重新扫描", command=self.refresh, height=36)
        self.btn_refresh.pack(side="right", padx=(8, 0))
        self.btn_admin = RoundButton(right, "🛡 普通权限", command=self.on_elevate, height=36)
        self.btn_admin.pack(side="right")

        # ② 统计卡片
        stats = tk.Frame(root, bg=C["bg"])
        stats.grid(row=1, column=0, sticky="ew", padx=18)
        for col in range(4):
            stats.columnconfigure(col, weight=1, uniform="stat")
        self.card_on = StatCard(stats, "已启用的启动项", C["accent"], lambda: self.chips.select("已启用"))
        self.card_bad = StatCard(stats, "可清理（无效/更新器/可疑）", C["danger"], lambda: self.chips.select("可清理"))
        self.card_off = StatCard(stats, "已被本软件禁用", C["ok"], lambda: self.chips.select("已禁用"))
        self.card_admin = StatCard(stats, "需要管理员才能改", C["warn"], lambda: self.chips.select("需管理员"))
        for col, card in enumerate((self.card_on, self.card_bad, self.card_off, self.card_admin)):
            card.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 10, 0))

        # ③ 筛选栏
        filt = tk.Frame(root, bg=C["bg"])
        filt.grid(row=2, column=0, sticky="ew", padx=18, pady=(12, 8))
        search_box = tk.Frame(filt, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        search_box.pack(side="left")
        tk.Label(search_box, text="🔍", bg=C["card"], fg=C["muted"]).pack(side="left", padx=(9, 0))
        self.search_entry = tk.Entry(search_box, textvariable=self.search_text, bd=0, bg=C["card"],
                                     fg=C["text"], insertbackground=C["accent"], font=F, width=24)
        self.search_entry.pack(side="left", padx=(6, 10), pady=7)
        self.search_entry.bind("<KeyRelease>", lambda _e: self._fill_tree())
        self.chips = ChipGroup(filt, ["全部", "已启用", "已禁用", "可清理", "需管理员", "无效项", "更新器"],
                               on_change=lambda _v: self._fill_tree())
        self.chips.pack(side="left", padx=10)
        self.chk_system = tk.Checkbutton(
            filt, text="显示微软系统计划任务", variable=self.include_system, command=self.refresh,
            bg=C["bg"], fg=C["muted"], activebackground=C["bg"], activeforeground=C["text"],
            selectcolor=C["card"], font=F_SMALL, bd=0, highlightthickness=0)
        self.chk_system.pack(side="right", padx=(0, 14))
        self.chk_group = tk.Checkbutton(
            filt, text="按软件分组", variable=self.group_mode,
            command=lambda: self._fill_tree(),
            bg=C["bg"], fg=C["muted"], activebackground=C["bg"], activeforeground=C["text"],
            selectcolor=C["card"], font=F_SMALL, bd=0, highlightthickness=0)
        self.chk_group.pack(side="right")

        # ④ 主体：左边列表 + 右边详情
        body = tk.Frame(root, bg=C["bg"])
        body.grid(row=3, column=0, sticky="nsew", padx=18)
        body.columnconfigure(0, weight=1)
        body.rowconfigure(0, weight=1)

        list_card = tk.Frame(body, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        list_card.grid(row=0, column=0, sticky="nsew")
        list_card.rowconfigure(1, weight=1)
        list_card.columnconfigure(0, weight=1)
        list_head = tk.Frame(list_card, bg=C["card"])
        list_head.grid(row=0, column=0, columnspan=2, sticky="ew", padx=12, pady=(10, 4))
        self.list_title = tk.Label(list_head, text="启动项", bg=C["card"], fg=C["text"], font=F_BOLD)
        self.list_title.pack(side="left")
        tk.Label(list_head, text="双击可以打开文件位置，右键有更多操作",
                 bg=C["card"], fg=C["muted"], font=F_SMALL).pack(side="right")

        columns = ("name", "status", "category", "source", "command")
        self.tree = ttk.Treeview(list_card, columns=columns, show="headings",
                                 selectmode="extended", style="Modern.Treeview")
        for col, text, width in (("name", "名称", 250), ("status", "状态", 90),
                                 ("category", "分类", 90), ("source", "来源", 200),
                                 ("command", "命令 / 目标文件", 430)):
            self.tree.heading(col, text=text, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, anchor="w", stretch=(col == "command"))
        self.tree.tag_configure("on", foreground=C["text"])
        self.tree.tag_configure("off", foreground="#9aa3b2")
        self.tree.tag_configure("bad", foreground=C["danger"])
        self.tree.tag_configure("odd", background="#fafbfe")
        self.tree.tag_configure("odd_off", background="#fafbfe", foreground="#9aa3b2")
        self.tree.tag_configure("odd_bad", background="#fafbfe", foreground=C["danger"])
        self.tree.grid(row=1, column=0, sticky="nsew", padx=(12, 0), pady=(0, 10))
        vsb = ttk.Scrollbar(list_card, orient="vertical", command=self.tree.yview)
        vsb.grid(row=1, column=1, sticky="ns", pady=(0, 10))
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.bind("<<TreeviewSelect>>", lambda _e: self._show_details())
        self.tree.bind("<Double-1>", lambda _e: self.on_open())
        self.tree.bind("<Button-3>", self._popup)

        self.detail = DetailPanel(body, self)
        self.detail.grid(row=0, column=1, sticky="nsew", padx=(12, 0))

        # ⑤ 底部操作栏
        footer = tk.Frame(root, bg=C["bg"])
        footer.grid(row=4, column=0, sticky="ew", padx=18, pady=(12, 14))
        self.btns = {}
        for key, text, mode, cmd in (
                ("disable", "⛔ 禁用所选", "danger", self.on_disable),
                ("enable", "✅ 启用所选", "success", self.on_enable),
                ("delete", "🗑 删除所选", "ghost", self.on_delete),
                ("add", "➕ 新增启动项", "ghost", self.on_add),
                ("undo", "↩ 撤销上次操作", "soft", self.on_undo),
                ("report", "📈 对比报告", "ghost", self.on_report),
                ("export", "💾 导出清单", "ghost", self.on_export),
                ("backup", "📁 打开备份区", "ghost", self.on_open_backup)):
            btn = RoundButton(footer, text, mode=mode, command=cmd, height=34)
            btn.pack(side="left", padx=(0, 8))
            self.btns[key] = btn
        self.btns["undo"].set_enabled(False)
        self.status = tk.Label(footer, text="准备就绪", bg=C["bg"], fg=C["muted"], font=F_SMALL)
        self.status.pack(side="right")

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="⛔ 禁用", command=self.on_disable)
        menu.add_command(label="✅ 启用", command=self.on_enable)
        menu.add_command(label="🗑 删除", command=self.on_delete)
        menu.add_separator()
        menu.add_command(label="📂 打开文件位置", command=self.on_open)
        menu.add_command(label="📋 复制命令", command=self.on_copy)
        menu.add_command(label="🔍 在注册表编辑器中定位", command=self.on_regedit)
        self.menu = menu

        root.bind("<F5>", lambda _e: self.refresh())
        root.bind("<Control-f>", lambda _e: self.search_entry.focus_set())
        root.bind("<Escape>", lambda _e: self._clear_search())
        root.bind("<Delete>", lambda _e: None)

    # ---------------------------------------------------------- 扫描流程 --
    def refresh(self):
        if self.scanning:
            return
        self.scanning = True
        self._dots = 0
        self._tick()
        for btn in self.btns.values():
            btn.set_enabled(False)
        self.btn_refresh.set_enabled(False)
        self.subtitle.configure(text="正在扫描启动项…")
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _tick(self):
        if not self.scanning:
            return
        self._dots = (self._dots + 1) % 4
        self.status.configure(text="正在扫描注册表、启动文件夹、计划任务" + "…" + "." * self._dots)
        self.root.after(320, self._tick)

    def _scan_worker(self):
        try:
            items = scan_all(bool(self.include_system.get()))
            err = None
        except Exception as exc:
            items, err = [], exc
        self.root.after(0, self._after_scan, items, err)

    def _after_scan(self, items, err):
        self.scanning = False
        for btn in self.btns.values():
            btn.set_enabled(True)
        self.btns["undo"].set_enabled(bool(self.last_batch))
        self.btn_refresh.set_enabled(True)
        if err is not None:
            self.subtitle.configure(text="扫描出错")
            self.status.configure(text=f"扫描出错：{err}")
            messagebox.showerror(APP_NAME, f"扫描启动项时出错：\n{err}")
            return
        self.items = items
        self.by_uid = {it.uid: it for it in items}
        self._fill_tree()
        self._update_stats()
        h = load_history()
        if not h["snapshots"] and not self._seeded:
            self._seeded = True
            record_snapshot(items, "首次扫描（初始状态）")
        elif self._pending_label:
            record_snapshot(items, self._pending_label)
            self._pending_label = None
        extra = f"　（已从旧版本并入 {self.migrated} 条禁用记录）" if self.migrated else ""
        self.subtitle.configure(text=f"已扫描 {len(items)} 项自启　·　备份区 {DATA_DIR}{extra}")

    def _update_stats(self):
        enabled = [it for it in self.items if it.enabled]
        disabled = [it for it in self.items if not it.enabled]
        bad = [it for it in self.items if it.enabled and self._advice(it)[1] in ("建议禁用", "可禁用")]
        need_admin = [it for it in self.items if it.need_admin]
        self.card_on.set_value(len(enabled))
        self.card_bad.set_value(len(bad))
        self.card_off.set_value(len(disabled))
        self.card_admin.set_value(len(need_admin))
        self.status.configure(
            text=f"共 {len(self.items)} 项：已启用 {len(enabled)} · 已禁用 {len(disabled)} · "
                 f"可清理 {len(bad)}　（{time.strftime('%H:%M:%S')}）")

    def _advice(self, item):
        return classify(item)

    # ---------------------------------------------------------- 列表渲染 --
    def _visible_items(self):
        text = self.search_text.get().strip().lower()
        mode = self.chips.get()
        result = []
        for it in self.items:
            category, advice, _reason = classify(it)
            if mode == "已启用" and not it.enabled:
                continue
            if mode == "已禁用" and it.enabled:
                continue
            if mode == "可清理" and not (it.enabled and advice in ("建议禁用", "可禁用")):
                continue
            if mode == "需管理员" and not it.need_admin:
                continue
            if mode == "无效项" and category != "无效项":
                continue
            if mode == "更新器" and category != "更新器":
                continue
            if text and text not in f"{it.name} {it.command} {it.source} {it.location}".lower():
                continue
            result.append(it)
        return result

    def _row_tag_for(self, it):
        """根据条目状态决定行的颜色标签。"""
        category, _a, _r = classify(it)
        if not it.enabled:
            return "off"
        if category in ("无效项", "可疑"):
            return "bad"
        return "on"

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        self.rows = {}
        self.groups = {}
        items = self._visible_items()
        grouped = bool(self.group_mode.get())

        def sort_key(it):
            category, _advice, _r = classify(it)
            return {
                "name": it.name.lower(),
                "status": (not it.enabled, it.name.lower()),
                "category": (category, it.name.lower()),
                "source": (it.source.lower(), it.name.lower()),
                "command": it.command.lower(),
            }[self.sort_col]

        if grouped:
            # —— 按软件分组视图：一组一行，可整组操作 ——
            families = {}
            for it in items:
                families.setdefault(app_family(it), []).append(it)
            for family, group in sorted(families.items(), key=lambda kv: kv[0].lower()):
                enabled = sum(1 for it in group if it.enabled)
                tags = ["group"]
                if enabled == 0:
                    tags.append("off")
                elif any(self._row_tag_for(it) == "bad" for it in group if it.enabled):
                    tags.append("bad")
                else:
                    tags.append("on")
                gid = f"group:{family}"
                self.groups[gid] = group
                self.tree.insert("", "end", iid=gid, open=True,
                                 values=(f"{family}", f"{enabled}/{len(group)} 启用", "整组", "按软件分组合并", ""),
                                 tags=tuple(tags))
                for it in sorted(group, key=lambda i: (not i.enabled, i.name.lower())):
                    base = self._row_tag_for(it)
                    tag = ("odd_" + base) if base in ("off", "bad") else base
                    self.tree.insert("", "end", iid=it.uid,
                                     values=(f"  {it.name}", it.status_text, str(classify(it)[0]),
                                             it.source, it.command),
                                     tags=(tag,))
                    self.rows[it.uid] = it
        else:
            # —— 平铺视图 ——
            items.sort(key=sort_key, reverse=self.sort_desc)
            for index, it in enumerate(items):
                base = self._row_tag_for(it)
                tag = ("odd_" + base) if (index % 2 and base in ("off", "bad")) else \
                      ("odd" if index % 2 else base)
                self.tree.insert("", "end", iid=it.uid,
                                 values=(it.name, it.status_text, str(classify(it)[0]),
                                         it.source, it.command),
                                 tags=(tag,))
                self.rows[it.uid] = it

        if not items:
            self.tree.insert("", "end", iid="__empty__",
                             values=("没有匹配的启动项", "", "", "", ""), tags=("off",))
        self.list_title.configure(text=f"启动项（显示 {len(items)} / 共 {len(self.items)}）")
        self._show_details()

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self._fill_tree()

    def _clear_search(self):
        self.search_text.set("")
        self._fill_tree()

    def selected_items(self):
        """当前选中的启动项（选中分组行时展开成组内所有项）。"""
        out, seen = [], set()
        for iid in self.tree.selection():
            if iid in self.rows and self.rows[iid].uid not in seen:
                out.append(self.rows[iid])
                seen.add(self.rows[iid].uid)
            elif iid in self.groups:
                for it in self.groups[iid]:
                    if it.uid not in seen:
                        out.append(it)
                        seen.add(it.uid)
        return out

    def _show_details(self):
        """右侧详情：优先展示分组行的概览。"""
        sel = set(self.tree.selection())
        group_only = []
        for gid, group in self.groups.items():
            uids = {it.uid for it in group}
            if gid in sel and not (uids & sel):
                group_only.append((gid, group))
        if group_only and len(group_only) == len(sel):
            _gid, group = group_only[0]
            self.detail.show_group(group)
            return
        self.detail.show(self.selected_items())

    # ---------------------------------------------------------- 统计徽章 --
    def _refresh_admin_badge(self):
        if is_admin():
            self.btn_admin.set_text("🛡 管理员权限")
            self.btn_admin.mode = "soft"
            self.btn_admin.colors = RoundButton.MODES["soft"]
            self.btn_admin._draw()
        else:
            self.btn_admin.set_text("🛡 普通权限")
        self.detail.refresh_admin()

    # ------------------------------------------------------------ 动作 --
    def _require_admin(self, items):
        need = [it for it in items if it.need_admin]
        if need and not is_admin():
            return messagebox.askyesno(
                APP_NAME,
                f"选中的 {len(need)} 项需要管理员权限才能修改。\n"
                "现在以管理员身份重新打开软件吗？（会弹 UAC 提示，当前窗口会关闭）")
        return False

    def _apply(self, label, func, items, *, remember=True, quiet=False):
        if not items:
            messagebox.showinfo(APP_NAME, "请先在列表里选中一项。")
            return 0
        if self._require_admin(items):
            self.on_elevate()
            return 0
        store = load_store()
        ok_count, messages = 0, []
        for it in items:
            try:
                ok, msg = func(it, store)
            except Exception as exc:
                ok, msg = False, f"{it.name}：{exc}"
            if ok:
                ok_count += 1
            else:
                messages.append(msg)
        if ok_count:
            save_store(store)
            log_action(f"{label} {ok_count} 项：" + "; ".join(it.name for it in items[:20]))
            if remember:
                self.last_batch = {"action": func, "label": label, "items": list(items)}
            self._pending_label = f"{label} {ok_count} 项"
        if messages and not quiet:
            messagebox.showwarning(APP_NAME, f"完成 {ok_count} 项，失败 {len(messages)} 项：\n\n"
                                             + "\n".join(messages[:10]))
        self.refresh()
        if ok_count and quiet:
            self.status.configure(text=f"{label}完成：{ok_count} 项（可以点「↩ 撤销上次操作」找回）")
        return ok_count

    def on_disable(self):
        picked = self.selected_items()
        items = [it for it in picked if it.enabled]
        if not items:
            messagebox.showinfo(APP_NAME, "请选中要禁用的启动项（按住 Ctrl / Shift 可多选）。")
            return
        if not messagebox.askyesno(APP_NAME, f"禁用选中的 {len(items)} 个启动项？\n\n"
                                            "它们会被搬进备份区，软件本身不受影响，随时可以启用回来。"):
            return
        self._apply("禁用", disable_item, items)

    def on_enable(self):
        items = [it for it in self.selected_items() if not it.enabled]
        if not items:
            messagebox.showinfo(APP_NAME, "请选中要启用的项（已禁用的项显示为灰色）。")
            return
        self._apply("启用", enable_item, items)

    def on_delete(self):
        items = self.selected_items()
        if not items:
            messagebox.showinfo(APP_NAME, "请选中要删除的启动项。")
            return
        names = "\n".join(f"· {it.name}（{it.source}）" for it in items[:8])
        if not messagebox.askyesno(APP_NAME, f"彻底删除这 {len(items)} 项？删除后无法还原：\n\n{names}\n\n"
                                            "只想让它别开机自启的话，用「禁用」更稳妥。"):
            return
        self._apply("删除", delete_item, items, remember=False)

    def on_undo(self):
        batch = self.last_batch
        if not batch:
            return
        if batch["action"] is disable_item:
            store = load_store()
            uids = {it.uid for it in batch["items"]}
            targets = [x for x in items_from_store(store) if x.uid in uids]
            func, label = enable_item, "撤销（恢复启用）"
        else:
            uids = {it.uid for it in batch["items"]}
            targets = [x for x in self.items if x.uid in uids and x.enabled]
            func, label = disable_item, "撤销（重新禁用）"
        if not targets:
            messagebox.showinfo(APP_NAME, "这些项已经不在列表里了，可能被别的软件改动过。")
            self.last_batch = None
            self.btns["undo"].set_enabled(False)
            return
        count = self._apply(label, func, targets, remember=False, quiet=True)
        if count:
            self.last_batch = None
            self.btns["undo"].set_enabled(False)

    def on_optimize(self):
        candidates = []
        for it in self.items:
            if not it.enabled:
                continue
            category, advice, reason = classify(it)
            if advice in ("建议禁用", "可禁用"):
                candidates.append((it, category, advice, reason))
        if not candidates:
            messagebox.showinfo(APP_NAME, "很干净，没有发现建议清理的启动项。")
            return
        OptimizeDialog(self.root, candidates, on_confirm=self._optimize_confirm)

    def _optimize_confirm(self, picked):
        if not picked:
            return
        count = self._apply("一键优化", disable_item, picked, quiet=True)
        if count:
            messagebox.showinfo(APP_NAME, f"优化完成，已禁用 {count} 项。\n\n"
                                          "不满意可以点「↩ 撤销上次操作」全部找回。")
            self.refresh()

    def on_add(self):
        AddDialog(self.root, self.refresh)

    def on_open(self):
        items = self.selected_items()
        if items:
            open_file_location(target_for_open(items[0]))

    def on_copy(self):
        items = self.selected_items()
        if not items:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(it.command for it in items))
        self.status.configure(text=f"已复制 {len(items)} 条命令到剪贴板")

    def on_regedit(self):
        items = self.selected_items()
        if not items:
            return
        it = items[0]
        if it.kind != "registry":
            messagebox.showinfo(APP_NAME, "只有注册表启动项才能定位到注册表。")
            return
        entry = load_store()["items"].get(it.uid, {})
        hive = it.extra.get("hive") or entry.get("hive")
        sub = it.extra.get("sub") or entry.get("sub")
        if hive and sub:
            regedit_goto(hive, sub)

    def on_open_backup(self):
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        os.startfile(str(DATA_DIR))  # noqa: S606

    def on_export(self):
        default = f"启动项清单_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(title="导出启动项清单", initialfile=default,
                                            defaultextension=".csv",
                                            filetypes=[("CSV 表格", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                data = [{"名称": it.name, "状态": it.status_text, "分类": classify(it)[0],
                         "建议": classify(it)[1], "来源": it.source, "位置": it.location,
                         "命令": it.command, "目标文件": it.target,
                         "文件存在": (not it.missing) if it.target else None} for it in self.items]
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["名称", "状态", "分类", "建议", "来源", "位置", "命令", "目标文件", "文件存在"])
                    for it in self.items:
                        category, advice, _r = classify(it)
                        writer.writerow([it.name, it.status_text, category, advice, it.source,
                                         it.location, it.command, it.target,
                                         "" if not it.target else ("是" if not it.missing else "否")])
            self.status.configure(text=f"已导出到 {path}")
            messagebox.showinfo(APP_NAME, f"已导出：\n{path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}")

    def on_report(self):
        snaps = load_history()["snapshots"]
        if not snaps:
            messagebox.showinfo(APP_NAME, "还没有可对比的历史记录。\n\n"
                                          "做几次「禁用 / 启用」操作后，这里会生成完整的前后对比。")
            return
        ReportDialog(self.root, load_history(), snapshot_counts(self.items))

    def on_elevate(self):
        if is_admin():
            messagebox.showinfo(APP_NAME, "当前已经是管理员权限了。")
            return
        if not messagebox.askyesno(APP_NAME, "将以管理员身份重新打开本软件（会弹 UAC 提示），继续？"):
            return
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showerror(APP_NAME, "提权失败，可能是你取消了 UAC 提示。")

    def _popup(self, event):
        row = self.tree.identify_row(event.y)
        if row and row != "__empty__":
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)


def target_for_open(item: Item) -> str:
    if item.kind == "folder":
        return item.extra.get("resolved") or item.extra.get("saved") or item.command
    return item.command


class DetailPanel(tk.Frame):
    """右侧详情 + 针对单项的操作。"""

    def __init__(self, master, app):
        super().__init__(master, bg=C["card"], highlightbackground=C["border"],
                         highlightthickness=1, width=350)
        self.app = app
        self.grid_propagate(False)
        self.item = None

        head = tk.Frame(self, bg=C["card"])
        head.pack(fill="x", padx=14, pady=(12, 6))
        tk.Label(head, text="详情", bg=C["card"], fg=C["text"], font=F_BOLD).pack(side="left")
        self.admin_hint = tk.Label(head, text="", bg=C["card"], fg=C["muted"], font=F_SMALL)
        self.admin_hint.pack(side="right")

        self.name_lbl = tk.Label(self, text="在左边选一项看看", bg=C["card"], fg=C["text"],
                                 font=("Microsoft YaHei UI", 12, "bold"), wraplength=300, justify="left")
        self.name_lbl.pack(anchor="w", padx=14)

        pills = tk.Frame(self, bg=C["card"])
        pills.pack(anchor="w", padx=14, pady=(6, 8))
        self.pill_cat = Pill(pills, "分类", *CATEGORY_STYLE["软件自启"])
        self.pill_cat.pack(side="left", padx=(0, 6))
        self.pill_advice = Pill(pills, "建议", *ADVICE_STYLE["看需要"])
        self.pill_advice.pack(side="left")

        self.reason = tk.Label(self, text="", bg=C["card"], fg=C["muted"], font=F_SMALL,
                               wraplength=310, justify="left")
        self.reason.pack(anchor="w", padx=14)

        grid = tk.Frame(self, bg=C["card"])
        grid.pack(fill="x", padx=14, pady=(10, 0))
        self.fields = {}
        for row, key in enumerate(("来源", "位置", "命令", "目标文件", "文件大小", "状态")):
            tk.Label(grid, text=key, bg=C["card"], fg=C["muted"], font=F_SMALL,
                     anchor="nw", width=6).grid(row=row, column=0, sticky="nw", pady=4)
            lbl = tk.Label(grid, text="-", bg=C["card"], fg=C["text"], font=F_SMALL,
                           wraplength=250, justify="left", anchor="w")
            lbl.grid(row=row, column=1, sticky="w", pady=4)
            self.fields[key] = lbl
        grid.columnconfigure(1, weight=1)

        actions = tk.Frame(self, bg=C["card"])
        actions.pack(side="bottom", fill="x", padx=14, pady=14)
        self.btn_toggle = RoundButton(actions, "⛔ 禁用这一项", command=self._toggle, mode="danger")
        self.btn_toggle.pack(fill="x", pady=(0, 6))
        row2 = tk.Frame(actions, bg=C["card"])
        row2.pack(fill="x")
        self.btn_open = RoundButton(row2, "📂 打开位置", command=self._open, bg=C["card"])
        self.btn_open.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.btn_reg = RoundButton(row2, "🔍 注册表", command=lambda: self.app.on_regedit(), bg=C["card"])
        self.btn_reg.pack(side="left", fill="x", expand=True)
        for btn in (self.btn_toggle, self.btn_open, self.btn_reg):
            btn.set_enabled(False)

    def refresh_admin(self):
        self.admin_hint.configure(text="管理员权限" if is_admin() else "普通权限")

    def show(self, items):
        if not items:
            self.item = None
            self.name_lbl.configure(text="在左边选一项看看")
            self.reason.configure(text="选中后这里会显示：来源、命令行、目标文件是否还在，"
                                       "以及这一项要不要留。")
            for lbl in self.fields.values():
                lbl.configure(text="-")
            for btn in (self.btn_toggle, self.btn_open, self.btn_reg):
                btn.set_enabled(False)
            return

        it = items[0]
        self.item = it
        category, advice, reason = classify(it)
        cat_fg, cat_bg = CATEGORY_STYLE.get(category, ("#2563eb", "#e8f0fe"))
        adv_fg, adv_bg = ADVICE_STYLE.get(advice, ("#2563eb", "#e8f0fe"))
        extra = f"（共选中 {len(items)} 项）" if len(items) > 1 else ""
        self.name_lbl.configure(text=f"{it.name}{extra}")
        self.pill_cat.set(category, cat_fg, cat_bg)
        self.pill_advice.set(advice, adv_fg, adv_bg)
        self.reason.configure(text=reason)
        self.fields["来源"].configure(text=it.source)
        self.fields["位置"].configure(text=it.location)
        self.fields["命令"].configure(text=it.command or "（这项没有命令行）")
        target = it.target
        self.fields["目标文件"].configure(text=target or "—")
        self.fields["文件大小"].configure(text=human_size(target) if target else "—")
        state = "已启用" if it.enabled else ("已禁用（本软件）" if it.managed else "已禁用（系统设置）")
        if it.missing:
            state += " · 文件已不存在"
        self.fields["状态"].configure(text=state)
        self.btn_toggle.set_text("✅ 启用这一项" if not it.enabled else "⛔ 禁用这一项")
        self.btn_toggle.mode = "success" if not it.enabled else "danger"
        self.btn_toggle.colors = RoundButton.MODES[self.btn_toggle.mode]
        self.btn_toggle._draw()
        for btn in (self.btn_toggle, self.btn_open, self.btn_reg):
            btn.set_enabled(True)
        self.btn_reg.set_enabled(it.kind == "registry")

    def show_group(self, group):
        """分组行（按软件分组视图）的概览。"""
        self.item = None
        enabled = sum(1 for it in group if it.enabled)
        family = app_family(group[0])
        names = "、".join(it.name for it in group[:4]) + ("…" if len(group) > 4 else "")
        self.name_lbl.configure(text=f"整组：{family}　（{len(group)} 项）")
        self.pill_cat.set("按软件分组", *CATEGORY_STYLE["软件自启"])
        if enabled == 0:
            self.pill_advice.set("整组已禁用", *ADVICE_STYLE["建议保留"])
        else:
            self.pill_advice.set("可整组操作", *ADVICE_STYLE["看需要"])
        self.reason.configure(text="这是「按软件分组」视图：一个软件在开机时可能留了多条自启，"
                                   "在这里一起看、一起管。禁用它等于禁用组里所有启动项。")
        self.fields["来源"].configure(text="按软件合并")
        self.fields["位置"].configure(text=f"{len(group)} 个启动项来自同一软件")
        self.fields["命令"].configure(text=names)
        self.fields["目标文件"].configure(text=family)
        self.fields["文件大小"].configure(text="—")
        self.fields["状态"].configure(text=f"{enabled} 项启用 / {len(group) - enabled} 项禁用")
        self.btn_toggle.set_text("⛔ 禁用整组" if enabled else "✅ 启用整组")
        self.btn_toggle.mode = "danger" if enabled else "success"
        self.btn_toggle.colors = RoundButton.MODES[self.btn_toggle.mode]
        self.btn_toggle._draw()
        for btn in (self.btn_toggle, self.btn_open, self.btn_reg):
            btn.set_enabled(True)
        self.btn_reg.set_enabled(False)

    def _toggle(self):
        items = self.app.selected_items()
        if not items:
            return
        if self.item and not self.item.enabled:
            self.app.on_enable()
        else:
            self.app.on_disable()

    def _open(self):
        if self.item:
            open_file_location(target_for_open(self.item))


class ReportDialog(tk.Toplevel):
    """📈 对比报告：初始 vs 当前 + 趋势时间线 + 导出。"""

    def __init__(self, master, history, now_counts):
        super().__init__(master)
        self.history = history
        self.snaps = history["snapshots"]
        self.now_counts = now_counts
        self.title("开机自启 · 前后对比报告")
        self.configure(bg=C["bg"])
        self.geometry("780x660")
        self.transient(master)
        self.grab_set()

        first = self.snaps[0]
        last = self.snaps[-1]
        delta = last["enabled"] - first["enabled"]

        head = tk.Frame(self, bg=C["bg"])
        head.pack(fill="x", padx=18, pady=(16, 8))
        tk.Label(head, text="📈 开机自启 前后对比", bg=C["bg"], fg=C["text"], font=F_TITLE).pack(anchor="w")
        tk.Label(head, text=f"首个快照 {first.get('t', '')}　→　最近 {last.get('t', '')}　·　共 {len(self.snaps)} 个时间点",
                 bg=C["bg"], fg=C["muted"], font=F_SMALL).pack(anchor="w")

        # 顶部大数字
        stat = tk.Frame(self, bg=C["bg"])
        stat.pack(fill="x", padx=18, pady=(4, 10))
        for col in range(4):
            stat.columnconfigure(col, weight=1, uniform="s")
        cards = [
            ("首次自启", first["enabled"], C["muted"]),
            ("现在自启", last["enabled"], C["accent"]),
            ("减少了" if delta < 0 else ("增加了" if delta > 0 else "没变化"),
             abs(delta), C["ok"] if delta <= 0 else C["danger"]),
            ("现在可清理", self.now_counts["bad"], C["warn"]),
        ]
        for col, (label, value, color) in enumerate(cards):
            box = tk.Frame(stat, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
            box.grid(row=0, column=col, sticky="ew", padx=(0 if col == 0 else 10, 0))
            tk.Label(box, text=str(value), bg=C["card"], fg=color, font=F_NUM).pack(anchor="w", padx=14, pady=(8, 0))
            tk.Label(box, text=label, bg=C["card"], fg=C["muted"], font=F_SMALL).pack(anchor="w", padx=14, pady=(0, 8))

        # 分类对比条形图
        chart_card = tk.Frame(self, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        chart_card.pack(fill="x", padx=18, pady=(0, 8))
        chart_head = tk.Frame(chart_card, bg=C["card"])
        chart_head.pack(fill="x", padx=12, pady=(8, 0))
        tk.Label(chart_head, text="各分类启动项数量", bg=C["card"], fg=C["text"], font=F_BOLD).pack(side="left")
        tk.Label(chart_head, text="浅灰＝首次　 蓝色＝当前", bg=C["card"], fg=C["muted"], font=F_SMALL).pack(side="right")
        self.chart = tk.Canvas(chart_card, bg=C["card"], width=720, height=230,
                               highlightthickness=0, bd=0)
        self.chart.pack(padx=8, pady=(0, 8))
        self._draw_chart()

        # 时间线
        tl_card = tk.Frame(self, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        tl_card.pack(fill="both", expand=True, padx=18, pady=(0, 8))
        tk.Label(tl_card, text="优化时间线", bg=C["card"], fg=C["text"], font=F_BOLD).pack(anchor="w", padx=12, pady=(8, 2))
        scroll = ScrollFrame(tl_card, bg=C["card"])
        scroll.pack(fill="both", expand=True, padx=6, pady=(0, 8))
        prev_enabled = None
        for s in self.snaps:
            d = (s["enabled"] - prev_enabled) if prev_enabled is not None else None
            prev_enabled = s["enabled"]
            row = tk.Frame(scroll.body, bg=C["card"])
            row.pack(fill="x", padx=8, pady=3)
            tk.Label(row, text=s.get("t", "")[5:16], bg=C["card"], fg=C["muted"], width=14,
                     font=F_SMALL, anchor="w").pack(side="left")
            tk.Label(row, text=s.get("label", ""), bg=C["card"], fg=C["text"], font=F_SMALL,
                     width=30, anchor="w").pack(side="left")
            tk.Label(row, text=f"启用 {s['enabled']} 项", bg=C["card"], fg=C["text"],
                     font=F_SMALL, anchor="w").pack(side="left")
            if d is not None:
                fg = C["ok"] if d < 0 else C["danger"] if d > 0 else C["muted"]
                tk.Label(row, text=("▼" if d > 0 else "▲") + str(abs(d)) if d else "—",
                         bg=C["card"], fg=fg, font=F_SMALL,
                         anchor="w").pack(side="left", padx=(6, 0))
            cats = s.get("cats", {})
            if cats:
                tk.Label(row, text="　更新器" + str(cats.get("更新器", 0)) +
                                 " · 无效" + str(cats.get("无效项", 0)) +
                                 " · 可疑" + str(cats.get("可疑", 0)),
                         bg=C["card"], fg=C["muted"], font=F_SMALL).pack(side="right")

        # 底部按钮
        bottom = tk.Frame(self, bg=C["bg"])
        bottom.pack(fill="x", padx=18, pady=(0, 16))
        RoundButton(bottom, "关闭", command=self.destroy, height=34, padx=20,
                    bg=C["bg"]).pack(side="right", padx=(8, 0))
        RoundButton(bottom, "导出 HTML 报告", command=self._export_html, height=34, padx=18,
                    bg=C["bg"]).pack(side="right", padx=(8, 0))
        RoundButton(bottom, "导出 CSV", command=self._export_csv, height=34, padx=18,
                    bg=C["bg"]).pack(side="right")

    def _draw_chart(self):
        canvas = self.chart
        canvas.delete("all")
        w, h = 720, 230
        first, last = self.snaps[0], self.snaps[-1]
        names = list(dict.fromkeys(list(first.get("cats", {})) + list(last.get("cats", {}))))
        data = [(n, first.get("cats", {}).get(n, 0), last.get("cats", {}).get(n, 0)) for n in names]
        data.sort(key=lambda v: -max(v[1], v[2]))
        maxv = max([max(v[1], v[2]) for v in data] or [1])
        left, right, topy, boty = 70, w - 26, 26, h - 46
        step = (right - left) / max(len(data), 1)
        bw = min(step * 0.21, 26)

        def bar(cx, val, color):
            bh = (val / maxv) * (boty - topy)
            if bh < 2 and val > 0:
                bh = 4
            canvas.create_rectangle(cx - bw, boty - bh, cx + bw, boty, fill=color, outline="")
            canvas.create_text(cx, boty - bh - 11, text=str(val), font=F_SMALL, fill=color)

        for i, (name, a, b) in enumerate(data):
            cx = left + step * (i + 0.5)
            bar(cx - bw * 0.55, a, "#cbd5e1")
            bar(cx + bw * 0.55, b, C["accent"])
            canvas.create_text(cx, boty + 14, text=name, font=F_SMALL, fill=C["muted"], width=int(step))
        for y in (topy, (topy + boty) // 2, boty):
            canvas.create_line(left, y, right, y, fill="#eef0f4")
            canvas.create_text(left - 8, y, text=str(round(maxv * (boty - y) / (boty - topy))),
                               font=F_SMALL, fill="#9aa3b2", anchor="e")

    def _comparison_rows(self):
        first, last = self.snaps[0], self.snaps[-1]
        names = list(dict.fromkeys(list(first.get("cats", {})) + list(last.get("cats", {}))))
        rows = []
        for n in names:
            a = first.get("cats", {}).get(n, 0)
            b = last.get("cats", {}).get(n, 0)
            rows.append((n, a, b, b - a))
        return rows

    def _export_csv(self):
        path = filedialog.asksaveasfilename(title="导出对比报告 CSV", initialfile="开机自启对比报告.csv",
                                            defaultextension=".csv",
                                            filetypes=[("CSV 表格", "*.csv")], parent=self)
        if not path:
            return
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                writer = csv.writer(fh)
                writer.writerow(["开机自启对比报告", ""])
                writer.writerow(["首次快照", self.snaps[0].get("t", "")])
                writer.writerow(["最近快照", self.snaps[-1].get("t", "")])
                writer.writerow([])
                writer.writerow(["分类", "首次", "当前", "变化"])
                for name, a, b, d in self._comparison_rows():
                    writer.writerow([name, a, b, d])
                writer.writerow([])
                writer.writerow(["时间", "事件", "启用项", "变化", "更新器", "无效", "可疑"])
                prev = None
                for s in self.snaps:
                    d = (s["enabled"] - prev) if prev is not None else ""
                    prev = s["enabled"]
                    cats = s.get("cats", {})
                    writer.writerow([s.get("t", ""), s.get("label", ""), s["enabled"], d,
                                     cats.get("更新器", 0), cats.get("无效项", 0), cats.get("可疑", 0)])
            messagebox.showinfo(APP_NAME, f"已导出：\n{path}", parent=self)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}", parent=self)

    def _export_html(self):
        path = filedialog.asksaveasfilename(title="导出 HTML 报告", initialfile="开机自启对比报告.html",
                                            defaultextension=".html",
                                            filetypes=[("HTML 报告", "*.html")], parent=self)
        if not path:
            return
        try:
            first, last = self.snaps[0], self.snaps[-1]
            delta = last["enabled"] - first["enabled"]
            bar_rows = []
            for name, a, b, d in self._comparison_rows():
                bar_rows.append(
                    f'<tr><td>{name}</td>'
                    f'<td class="big">{a}</td>'
                    f'<td class="big on">{b}</td>'
                    f'<td>{d if d >= 0 else ("+" + str(abs(d)))}</td></tr>')
            tl_rows = []
            prev = None
            for s in self.snaps:
                d = (s["enabled"] - prev) if prev is not None else ""
                prev = s["enabled"]
                cls = "down" if (isinstance(d, int) and d < 0) else ("up" if (isinstance(d, int) and d > 0) else "")
                tl_rows.append(
                    f'<tr><td class="ts">{s.get("t", "")}</td>'
                    f'<td>{s.get("label", "")}</td>'
                    f'<td class="big on">{s["enabled"]}</td>'
                    f'<td class="{cls}">{d if isinstance(d, int) else "—"}</td></tr>')
            html = f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>开机自启对比报告</title><style>
body{{font-family:'Microsoft YaHei','PingFang SC',system-ui,sans-serif;background:#f4f6fa;color:#1f2430;max-width:880px;margin:28px auto;padding:0 18px}}
h1{{font-size:22px}} .sub{{color:#6b7280;font-size:13px}}
.cards{{display:flex;gap:12px;margin:18px 0}}
.card{{flex:1;background:#fff;border:1px solid #e3e7ef;border-radius:12px;padding:14px 16px}}
.card .num{{font-size:30px;font-weight:700}} .card .lab{{color:#6b7280;font-size:12px;margin-top:2px}}
.blue{{color:#2563eb}} .red{{color:#dc2626}} .green{{color:#16803c}}
table{{width:100%;border-collapse:collapse;background:#fff;border-radius:12px;overflow:hidden;font-size:13px}}
th,td{{padding:8px 12px;text-align:left;border-bottom:1px solid #eef0f4}}
th{{background:#f7f8fb;color:#6b7280}}
.big{{font-weight:700}} .on{{color:#2563eb}} .up{{color:#2563eb}} .down{{color:#16803c}} .ts{{color:#6b7280;font-size:12px}}
.section{{font-size:15px;font-weight:700;margin:22px 0 8px}}</style></head><body>
<h1>🥚 开机自启 · 前后对比报告</h1>
<div class="sub">首个快照 {first.get('t','')} → 最近 {last.get('t','')}　·　共 {len(self.snaps)} 个时间点</div>
<div class="cards">
<div class="card"><div class="num">{first['enabled']}</div><div class="lab">首次自启项目数</div></div>
<div class="card"><div class="num blue">{last['enabled']}</div><div class="lab">现在自启项目数</div></div>
<div class="card"><div class="num {'green' if delta<=0 else 'red'}">{("减少了 "+str(abs(delta))) if delta<0 else ("增加了 "+str(delta)) if delta>0 else "0"}</div><div class="lab">变化</div></div>
<div class="card"><div class="num red">{self.now_counts['bad']}</div><div class="lab">当前可清理项</div></div>
</div>
<div class="section">分类对比</div>
<table><tr><th>分类</th><th>首次</th><th>当前</th><th>变化</th></tr>{''.join(bar_rows)}</table>
<div class="section">优化时间线</div>
<table><tr><th>时间</th><th>事件</th><th>启用项</th><th>变化</th></tr>{''.join(tl_rows)}</table>
<p class="sub" style="margin-top:22px">由「开机启动管理器」自动生成 · 备份区 {DATA_DIR}</p>
</body></html>"""
            Path(path).write_text(html, encoding="utf-8")
            webbrowser.open("file:///" + path.replace("\\", "/"))
            messagebox.showinfo(APP_NAME, f"已导出并在浏览器打开：\n{path}", parent=self)
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}", parent=self)


class OptimizeDialog(tk.Toplevel):
    """一键优化：把「建议禁用 / 可禁用」的项列出来，勾选后批量禁用。"""

    def __init__(self, master, candidates, on_confirm):
        super().__init__(master)
        self.on_confirm = on_confirm
        self.candidates = candidates
        self.vars = []
        self.title("一键优化")
        self.configure(bg=C["bg"])
        self.geometry("720x600")
        self.transient(master)
        self.grab_set()

        head = tk.Frame(self, bg=C["bg"])
        head.pack(fill="x", padx=18, pady=(16, 6))
        tk.Label(head, text="✨ 一键优化开机自启", bg=C["bg"], fg=C["text"], font=F_TITLE).pack(anchor="w")
        tk.Label(head, text="下面这些项在开机时白跑一趟，禁用它们可以让开机更快、后台更清爽。\n"
                            "禁用只是搬进备份区，随时能撤销或用「启用」找回来。",
                 bg=C["bg"], fg=C["muted"], font=F_SMALL, justify="left").pack(anchor="w", pady=(4, 0))

        tools = tk.Frame(self, bg=C["bg"])
        tools.pack(fill="x", padx=18, pady=(6, 4))
        RoundButton(tools, "全选", command=lambda: self._set_all(True), height=30, padx=12,
                    bg=C["bg"]).pack(side="left", padx=(0, 8))
        RoundButton(tools, "全不选", command=lambda: self._set_all(False), height=30, padx=12,
                    bg=C["bg"]).pack(side="left")
        self.count_lbl = tk.Label(tools, text="", bg=C["bg"], fg=C["muted"], font=F_SMALL)
        self.count_lbl.pack(side="right")

        card = tk.Frame(self, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        card.pack(fill="both", expand=True, padx=18, pady=(8, 8))
        scroll = ScrollFrame(card)
        scroll.pack(fill="both", expand=True, padx=6, pady=6)

        for it, category, advice, reason in candidates:
            var = tk.BooleanVar(value=True)
            self.vars.append((var, it))
            row = tk.Frame(scroll.body, bg=C["card"])
            row.pack(fill="x", pady=2)
            tk.Checkbutton(row, variable=var, bg=C["card"], activebackground=C["card"],
                           selectcolor=C["card"], bd=0, highlightthickness=0,
                           command=self._update_count).pack(side="left")
            text = tk.Frame(row, bg=C["card"])
            text.pack(side="left", fill="x", expand=True)
            top = tk.Frame(text, bg=C["card"])
            top.pack(anchor="w", fill="x")
            tk.Label(top, text=it.name, bg=C["card"], fg=C["text"], font=F_BOLD).pack(side="left")
            fg, bg = ADVICE_STYLE.get(advice, ("#2563eb", "#e8f0fe"))
            Pill(top, advice, fg, bg, bg_parent=C["card"]).pack(side="left", padx=8)
            tk.Label(top, text=category, bg=C["card"], fg=C["muted"], font=F_SMALL).pack(side="left")
            tk.Label(text, text=f"{reason}　·　{it.source}", bg=C["card"], fg=C["muted"],
                     font=F_SMALL, wraplength=560, justify="left").pack(anchor="w")

        bottom = tk.Frame(self, bg=C["bg"])
        bottom.pack(fill="x", padx=18, pady=(0, 16))
        RoundButton(bottom, "取消", command=self.destroy, height=36, padx=20,
                    bg=C["bg"]).pack(side="right", padx=(8, 0))
        RoundButton(bottom, "立即优化", command=self._confirm, mode="primary", height=36,
                    padx=22, bg=C["bg"]).pack(side="right")
        self._update_count()

    def _set_all(self, value):
        for var, _it in self.vars:
            var.set(value)
        self._update_count()

    def _picked(self):
        return [it for var, it in self.vars if var.get()]

    def _update_count(self):
        picked = self._picked()
        self.count_lbl.configure(text=f"已勾选 {len(picked)} / {len(self.vars)} 项")

    def _confirm(self):
        picked = self._picked()
        if not picked:
            messagebox.showinfo(APP_NAME, "至少勾选一项。", parent=self)
            return
        self.destroy()
        self.on_confirm(picked)


class AddDialog(tk.Toplevel):
    """新增启动项。"""

    def __init__(self, master, on_done):
        super().__init__(master)
        self.on_done = on_done
        self.title("新增开机启动项")
        self.configure(bg=C["bg"])
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        self.name_var = tk.StringVar()
        self.cmd_var = tk.StringVar()
        self.args_var = tk.StringVar()
        self.scope_var = tk.StringVar(value="当前用户 · 注册表 Run")

        body = tk.Frame(self, bg=C["card"], highlightbackground=C["border"], highlightthickness=1)
        body.pack(fill="both", expand=True, padx=18, pady=18)

        def label(text, row):
            tk.Label(body, text=text, bg=C["card"], fg=C["muted"], font=F_SMALL).grid(
                row=row, column=0, sticky="e", padx=(14, 8), pady=6)

        def entry(var, row, width=46):
            ent = tk.Entry(body, textvariable=var, bd=1, relief="flat", bg="#f7f8fb",
                           fg=C["text"], font=F, width=width, insertbackground=C["accent"])
            ent.grid(row=row, column=1, columnspan=2, sticky="we", pady=6, ipady=5)
            return ent

        label("名称", 0)
        entry(self.name_var, 0)
        label("程序", 1)
        entry(self.cmd_var, 1, 36)
        RoundButton(body, "浏览…", command=self._browse, height=32, bg=C["card"]).grid(
            row=1, column=3, padx=(8, 14))
        label("参数", 2)
        entry(self.args_var, 2)
        label("方式", 3)
        combo = ttk.Combobox(body, textvariable=self.scope_var, state="readonly", width=43, values=[
            "当前用户 · 注册表 Run",
            "所有用户 · 注册表 Run（需管理员）",
            "当前用户 · 启动文件夹",
            "所有用户 · 启动文件夹（需管理员）",
        ])
        combo.grid(row=3, column=1, columnspan=3, sticky="we", pady=6)

        tk.Label(body, text="例：想让某程序开机自动跑，选「当前用户 · 注册表 Run」即可；"
                            "不确定就先建好、再决定要不要禁用。",
                 bg=C["card"], fg=C["muted"], font=F_SMALL, wraplength=420,
                 justify="left").grid(row=4, column=0, columnspan=4, sticky="w", padx=14, pady=(10, 4))

        btns = tk.Frame(body, bg=C["card"])
        btns.grid(row=5, column=0, columnspan=4, sticky="e", padx=14, pady=(8, 14))
        RoundButton(btns, "取消", command=self.destroy, height=34, padx=18,
                    bg=C["card"]).pack(side="right", padx=(8, 0))
        RoundButton(btns, "确定新增", command=self._ok, mode="primary", height=34,
                    padx=18, bg=C["card"]).pack(side="right")
        body.columnconfigure(1, weight=1)

    def _browse(self):
        path = filedialog.askopenfilename(
            title="选择要开机启动的程序",
            filetypes=[("程序", "*.exe *.bat *.cmd *.lnk *.vbs *.ps1"), ("所有文件", "*.*")])
        if path:
            path = os.path.normpath(path)
            self.cmd_var.set(path)
            if not self.name_var.get():
                self.name_var.set(Path(path).stem)

    def _ok(self):
        name = self.name_var.get().strip()
        raw = self.cmd_var.get().strip()
        args = self.args_var.get().strip()
        if not name or not raw:
            messagebox.showwarning(APP_NAME, "名称和程序都要填。", parent=self)
            return
        target = expand(raw)
        if not path_exists(target):
            if not messagebox.askyesno(APP_NAME, f"这个文件好像不存在：\n{target}\n\n仍然添加？", parent=self):
                return
        command = f'"{target}"' + (f" {args}" if args else "")
        scope = self.scope_var.get()
        if scope.startswith("当前用户 · 注册表"):
            ok, msg = add_registry_run(name, command, all_users=False)
        elif scope.startswith("所有用户 · 注册表"):
            ok, msg = add_registry_run(name, command, all_users=True)
        elif scope.startswith("当前用户 · 启动文件夹"):
            ok, msg = add_folder_run(name, target, args, all_users=False)
        else:
            ok, msg = add_folder_run(name, target, args, all_users=True)
        if ok:
            log_action(f"新增启动项：{name} → {command}（{scope}）")
            self.destroy()
            self.on_done()
        else:
            messagebox.showerror(APP_NAME, msg, parent=self)


# --------------------------------------------------------------------------- #
# 命令行
# --------------------------------------------------------------------------- #
def cli_list() -> None:
    items = scan_all(include_system_tasks=True)
    print(f"{'状态':<12} {'分类':<8} {'建议':<8} {'来源':<22} 名称 / 命令")
    print("-" * 130)
    for it in sorted(items, key=lambda i: (classify(i)[0], i.name.lower())):
        category, advice, _r = classify(it)
        print(f"{it.status_text:<12} {category:<8} {advice:<8} {it.source[:20]:<22} {it.name}")
        print(f"{'':<54}{it.command or '（无命令行）'}"
              + ("　［文件缺失］" if it.missing else ""))
    print("-" * 130)
    enabled = sum(1 for i in items if i.enabled)
    bad = sum(1 for i in items if i.enabled and classify(i)[1] in ("建议禁用", "可禁用"))
    print(f"共 {len(items)} 项：已启用 {enabled} · 已禁用 {len(items) - enabled} · 建议清理 {bad}")


def cli_enable_all() -> None:
    store = load_store()
    if not store["items"]:
        print("备份区是空的，没有被禁用过的启动项。")
        return
    items = {it.uid: it for it in scan_all(include_system_tasks=True)}
    for it in items_from_store(store):
        items.setdefault(it.uid, it)
    done = 0
    for uid in list(store["items"]):
        it = items.get(uid)
        if it is None:
            print(f"跳过（当前扫描不到）：{store['items'][uid].get('name')}")
            continue
        ok, msg = enable_item(it, store)
        print(("  " if ok else "× ") + msg)
        done += int(ok)
    if done:
        save_store(store)
        log_action(f"命令行还原 {done} 项")
    print(f"共还原 {done} 项")


def install_excepthook() -> None:
    def hook(etype, value, tb):
        text = "".join(traceback.format_exception(etype, value, tb))
        log_action("未捕获异常：" + text.replace("\n", " | ")[:3000])
        try:
            messagebox.showerror(APP_NAME, f"出错了：{value}\n\n详细信息已写入：\n{LOG_FILE}")
        except Exception:
            pass
    sys.excepthook = hook
    tk.Tk.report_callback_exception = lambda self, e, v, t: hook(e, v, t)


def main() -> None:
    if winreg is None:
        print("这个软件只能在 Windows 上运行。")
        return
    argv = sys.argv[1:]
    migrated = migrate_legacy_stores()      # 把旧位置的禁用记录并进来
    if migrated:
        print(f"已从旧备份位置并入 {migrated} 条禁用记录 → {DATA_DIR}")
    if "--list" in argv:
        cli_list()
        return
    if "--enable-all" in argv:
        cli_enable_all()
        return

    install_excepthook()
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)   # 高分屏不糊
    except Exception:
        pass
    root = tk.Tk()
    StartupManagerGUI(root, migrated=migrated)
    root.mainloop()


if __name__ == "__main__":
    main()
