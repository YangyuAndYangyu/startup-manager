# -*- coding: utf-8 -*-
"""开机启动管理器 —— 查看 / 禁用 / 启用 / 删除 开机自启的软件

管理三类开机自启来源：
  1. 注册表       HKCU / HKLM 的 ...\\CurrentVersion\\Run 与 RunOnce（含 32 位视图）
  2. 启动文件夹   当前用户 与 所有用户 的「启动」目录
  3. 计划任务     登录时 / 开机时 触发的任务（可切换是否显示微软系统任务）

（Windows 服务不在管理范围内，避免误伤系统功能。）

安全设计：
  * 「禁用」= 把启动项挪到本软件的备份区 %LOCALAPPDATA%\\StartupManager，
    注册表项的原值 / 类型、启动文件夹里的文件、计划任务名都会完整保留，
    随时可以「启用」原样还原
  * 每次操作都写进 log.txt，方便回溯
  * 写 HKLM、改系统计划任务需要管理员权限，软件会提示并支持一键提权重启

用法：
  python startup_manager.py                 启动图形界面
  python startup_manager.py --list          命令行列出所有启动项（排查用）
  python startup_manager.py --enable-all    还原所有被本软件禁用过的启动项
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
from dataclasses import dataclass, field
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox, ttk

try:
    import winreg
except ImportError:  # 非 Windows 上只做语法检查时用到
    winreg = None

APP_NAME = "开机启动管理器"
DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or Path.home()) / "StartupManager"
DISABLED_FILE = DATA_DIR / "disabled.json"
DISABLED_FILES = DATA_DIR / "disabled_files"
BACKUP_DIR = DATA_DIR / "backup"
LOG_FILE = DATA_DIR / "log.txt"

HIVES = {
    "HKCU": (winreg.HKEY_CURRENT_USER if winreg else None),
    "HKLM": (winreg.HKEY_LOCAL_MACHINE if winreg else None),
}
HIVE_LABEL = {"HKCU": "HKEY_CURRENT_USER", "HKLM": "HKEY_LOCAL_MACHINE"}

# 注册表里的自启项来源（顺序就是界面里的排列顺序）
RUN_SOURCES = [
    {"key": "HKCU", "sub": r"Software\Microsoft\Windows\CurrentVersion\Run", "label": "当前用户 Run", "admin": False},
    {"key": "HKCU", "sub": r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "label": "当前用户 RunOnce", "admin": False},
    {"key": "HKCU", "sub": r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "label": "当前用户 Run (32位)", "admin": False},
    {"key": "HKLM", "sub": r"Software\Microsoft\Windows\CurrentVersion\Run", "label": "所有用户 Run", "admin": True},
    {"key": "HKLM", "sub": r"Software\Microsoft\Windows\CurrentVersion\RunOnce", "label": "所有用户 RunOnce", "admin": True},
    {"key": "HKLM", "sub": r"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Run", "label": "所有用户 Run (32位)", "admin": True},
]

_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
SUSPECT_HINTS = ("\\temp\\", "\\tmp\\", "\\appdata\\local\\temp", "\\downloads\\")


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #
def is_admin() -> bool:
    """当前进程是不是管理员。"""
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def relaunch_as_admin() -> bool:
    """用管理员权限重新启动自己。"""
    try:
        script = os.path.abspath(sys.argv[0])
        params = " ".join(f'"{a}"' for a in sys.argv[1:])
        rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, f'"{script}" {params}'.strip(), None, 1)
        return int(rc) > 32
    except Exception:
        return False


def _decode(raw: bytes) -> str:
    """Windows 命令输出解码：优先 UTF-8，再退到 GBK / mbcs。"""
    if not raw:
        return ""
    for enc in ("utf-8-sig", "utf-8", "gbk", "mbcs"):
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def run_cmd(args, timeout=60) -> tuple:
    """执行命令，返回 (是否成功, 输出文本)。"""
    try:
        p = subprocess.run(list(args), capture_output=True, timeout=timeout, creationflags=_NO_WINDOW)
    except Exception as exc:  # 找不到程序 / 超时
        return False, str(exc)
    return p.returncode == 0, (_decode(p.stdout) + _decode(p.stderr)).strip()


def run_ps(script: str, timeout=90) -> str:
    """执行一段 PowerShell，返回标准输出。"""
    exe = shutil.which("powershell") or shutil.which("pwsh")
    if not exe:
        return ""
    ok, out = run_cmd(
        [exe, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        timeout=timeout,
    )
    return out if ok else ""


def expand(text: str) -> str:
    return os.path.expandvars((text or "").strip())


def exe_of(command: str) -> str:
    """从启动命令行里猜出可执行文件的路径。"""
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


def looks_suspicious(command: str) -> bool:
    low = expand(command).lower()
    return any(h in low for h in SUSPECT_HINTS)


def open_file_location(path: str) -> None:
    """在资源管理器里选中文件。"""
    p = expand(path).strip().strip('"')
    if path_exists(p):
        subprocess.Popen(["explorer", "/select,", os.path.normpath(p)])
    elif p:
        parent = os.path.dirname(os.path.normpath(p))
        if os.path.isdir(parent):
            os.startfile(parent)  # noqa: S606 - 打开本机文件夹
        else:
            messagebox.showwarning(APP_NAME, f"找不到这个文件：\n{p}")
    else:
        messagebox.showwarning(APP_NAME, "这一项没有可打开的文件路径。")


def regedit_goto(hive: str, sub: str) -> None:
    """打开注册表编辑器并定位到指定项。"""
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
    """把名字变成安全的文件名。"""
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", name or "item").strip(" .")
    return (s[:max_len] or "item")


# --------------------------------------------------------------------------- #
# 备份区：被禁用的启动项都存这里
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


def log_action(text: str) -> None:
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        with LOG_FILE.open("a", encoding="utf-8") as fh:
            fh.write(f"[{stamp}] {text}\n")
    except Exception:
        pass


def _dump_value(value):
    """把注册表值转成能存 JSON 的形式。"""
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
# 启动项
# --------------------------------------------------------------------------- #
@dataclass
class Item:
    uid: str
    kind: str                 # registry / folder / task
    name: str
    command: str              # 命令行 或 文件路径
    source: str               # 来源（界面上显示）
    location: str             # 注册表路径 / 文件夹 / 任务全名
    enabled: bool
    need_admin: bool = False
    managed: bool = False     # 是被本软件禁用的吗
    extra: dict = field(default_factory=dict)   # 还原所需原始信息

    @property
    def target(self) -> str:
        """要检查是否存在的文件。"""
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
        return looks_suspicious(self.command)

    @property
    def status_text(self) -> str:
        parts = ["已启用" if self.enabled else ("已禁用" if self.managed else "已禁用（系统设置）")]
        if self.missing:
            parts.append("文件缺失")
        if self.suspicious:
            parts.append("可疑路径")
        return " · ".join(parts)


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
                    cmd = value if isinstance(value, str) else (value[0] if isinstance(value, (list, tuple)) and value else str(value))
                    items.append(Item(
                        uid=f"registry|{src['key']}|{src['sub']}|{name}",
                        kind="registry",
                        name=name,
                        command=str(cmd),
                        source=src["label"],
                        location=f"{HIVE_LABEL[src['key']]}\\{src['sub']}",
                        enabled=True,
                        need_admin=src["admin"],
                        extra={"hive": src["key"], "sub": src["sub"], "value_type": vtype},
                    ))
        except (FileNotFoundError, PermissionError, OSError):
            continue
    return items


def startup_folders() -> list:
    folders = []
    appdata = os.environ.get("APPDATA")
    if appdata:
        folders.append({
            "path": Path(appdata) / r"Microsoft\Windows\Start Menu\Programs\Startup",
            "label": "当前用户 启动文件夹", "admin": False,
        })
    programdata = os.environ.get("ProgramData")
    if programdata:
        folders.append({
            "path": Path(programdata) / r"Microsoft\Windows\Start Menu\Programs\Startup",
            "label": "所有用户 启动文件夹", "admin": True,
        })
    return folders


def _resolve_shortcuts(paths: list) -> dict:
    """用 PowerShell 批量读取快捷方式指向的目标。"""
    paths = [str(p) for p in paths if str(p).lower().endswith(".lnk")]
    if not paths:
        return {}
    quoted = ",".join("'" + p.replace("'", "''") + "'" for p in paths)
    script = (
        "$sh = New-Object -ComObject WScript.Shell; "
        f"@({quoted}) | ForEach-Object {{ "
        "$t = ''; try { $t = $sh.CreateShortcut($_).TargetPath } catch {}; "
        "\"$_`t$t\" }"
    )
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
            target = resolved.get(str(entry), "")
            items.append(Item(
                uid=f"folder|{path}|{entry.name}",
                kind="folder",
                name=entry.name,
                command=str(entry),
                source=folder["label"],
                location=str(path),
                enabled=True,
                need_admin=folder["admin"],
                extra={"path": str(entry), "resolved": target},
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
    out = run_ps(PS_TASKS)
    items = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        state = parts[0].strip()
        task_path = parts[1].strip()
        task_name = parts[2].strip()
        execute = parts[3].strip() if len(parts) > 3 else ""
        args = parts[4].strip() if len(parts) > 4 else ""
        full = f"{task_path}{task_name}"
        if not include_system and task_path.lower().startswith("\\microsoft\\"):
            continue
        if execute.strip('" ').lower() in ("", "by user disabled", "no action"):
            # 有些任务用 COM 处理器，或者被别的软件写成了占位文本，这里当作「没有命令行」
            command = ""
        elif execute.startswith('"'):
            command = f"{execute} {args}".strip()
        else:
            command = f'"{execute}" {args}'.strip()
        items.append(Item(
            uid=f"task|{full}",
            kind="task",
            name=task_name,
            command=command,
            source="计划任务（登录/开机触发）",
            location=full,
            enabled=(state.lower() != "disabled"),
            need_admin=task_path.lower().startswith("\\microsoft\\"),
            extra={"full_name": full, "state": state},
        ))
    return items


def items_from_store(store: dict = None) -> list:
    """把备份区里「被本软件禁用过」的启动项还原成 Item，用于显示和还原。"""
    store = store if store is not None else load_store()
    items = []
    for uid, entry in store["items"].items():
        kind = entry.get("kind")
        if kind == "registry":
            items.append(Item(
                uid=uid, kind="registry", name=entry.get("name", ""), command=entry.get("command", ""),
                source=entry.get("source", "注册表"), location=entry.get("location", ""),
                enabled=False, managed=True, need_admin=entry.get("need_admin", False),
                extra={"hive": entry.get("hive"), "sub": entry.get("sub")},
            ))
        elif kind == "folder":
            items.append(Item(
                uid=uid, kind="folder", name=entry.get("name", ""), command=entry.get("path", ""),
                source=entry.get("source", "启动文件夹"), location=entry.get("location", ""),
                enabled=False, managed=True, need_admin=entry.get("need_admin", False),
                extra={"saved": entry.get("saved", ""), "path": entry.get("path", ""),
                       "resolved": entry.get("resolved", "")},
            ))
        elif kind == "task":
            items.append(Item(
                uid=uid, kind="task", name=entry.get("name", ""), command=entry.get("command", ""),
                source="计划任务（登录/开机触发）", location=entry.get("location", ""),
                enabled=False, managed=True, need_admin=entry.get("need_admin", False),
                extra={"full_name": entry.get("full_name", ""), "state": "Disabled"},
            ))
    return items


def scan_all(include_system_tasks: bool = False) -> list:
    """扫描全部自启项，并把「被本软件禁用过」的项补回列表。"""
    items = []
    if winreg is not None:
        items.extend(scan_registry())
    items.extend(scan_folders())
    items.extend(scan_tasks(include_system_tasks))

    seen = {it.uid for it in items}
    for it in items_from_store():
        if it.uid not in seen:      # 用户又手动加回来了，以实际的为准
            items.append(it)
    return items


# --------------------------------------------------------------------------- #
# 动作：禁用 / 启用 / 删除 / 新增
# --------------------------------------------------------------------------- #
def disable_item(item: Item, store: dict) -> tuple:
    """把一个启用中的启动项挪进备份区。返回 (成功, 消息)。"""
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
            return False, "需要管理员权限（请点「以管理员身份重启」）"
        except OSError as exc:
            return False, f"删除注册表值失败：{exc}"
        store["items"][item.uid] = {
            "kind": "registry",
            "name": item.name,
            "command": item.command,
            "source": item.source,
            "location": item.location,
            "hive": item.extra["hive"],
            "sub": item.extra["sub"],
            "value": _dump_value(value),
            "value_type": vtype,
            "need_admin": item.need_admin,
            "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
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
            return False, "需要管理员权限（请点「以管理员身份重启」）"
        except Exception as exc:
            return False, f"移动文件失败：{exc}"
        store["items"][item.uid] = {
            "kind": "folder",
            "name": item.name,
            "command": item.command,
            "source": item.source,
            "location": item.location,
            "path": str(src),
            "saved": str(dest),
            "resolved": item.extra.get("resolved", ""),
            "need_admin": item.need_admin,
            "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return True, f"已禁用：{item.name}"

    if item.kind == "task":
        ok, out = run_cmd(["schtasks", "/change", "/tn", item.extra["full_name"], "/disable"])
        if not ok:
            return False, f"禁用计划任务失败：{out[:200]}"
        store["items"][item.uid] = {
            "kind": "task",
            "name": item.name,
            "command": item.command,
            "location": item.location,
            "full_name": item.extra["full_name"],
            "need_admin": item.need_admin,
            "disabled_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        return True, f"已禁用计划任务：{item.name}"

    return False, "未知的启动项类型"


def enable_item(item: Item, store: dict) -> tuple:
    """还原一个被禁用的启动项。"""
    entry = store["items"].get(item.uid)

    if item.kind == "registry":
        if winreg is None:
            return False, "当前系统不支持注册表操作"
        if not entry:
            return False, "备份里没有这一项的原始数据，无法自动还原"
        hive = HIVES[entry["hive"]]
        try:
            with winreg.CreateKeyEx(hive, entry["sub"], 0, winreg.KEY_SET_VALUE) as key:
                winreg.SetValueEx(key, entry["name"], 0, entry["value_type"], _load_value(entry["value"]))
        except PermissionError:
            return False, "需要管理员权限（请点「以管理员身份重启」）"
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
        target = dest
        if target.exists():
            target = dest.with_name(f"{dest.stem} (还原){dest.suffix}")
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(target))
        except PermissionError:
            return False, "需要管理员权限（请点「以管理员身份重启」）"
        except Exception as exc:
            return False, f"还原文件失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已启用：{item.name}"

    if item.kind == "task":
        ok, out = run_cmd(["schtasks", "/change", "/tn", item.extra["full_name"], "/enable"])
        if not ok:
            return False, f"启用计划任务失败：{out[:200]}"
        store["items"].pop(item.uid, None)
        return True, f"已启用计划任务：{item.name}"

    return False, "未知的启动项类型"


def delete_item(item: Item, store: dict) -> tuple:
    """彻底删除启动项（不可还原）。"""
    if item.kind == "registry":
        if item.enabled:
            hive = HIVES[item.extra["hive"]]
            try:
                with winreg.OpenKey(hive, item.extra["sub"], 0, winreg.KEY_SET_VALUE) as key:
                    winreg.DeleteValue(key, item.name)
            except PermissionError:
                return False, "需要管理员权限（请点「以管理员身份重启」）"
            except OSError as exc:
                return False, f"删除注册表值失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已删除：{item.name}"

    if item.kind == "folder":
        target = Path(item.extra.get("path") if item.enabled else store["items"].get(item.uid, {}).get("saved", ""))
        try:
            if target and target.exists():
                target.unlink()
        except PermissionError:
            return False, "需要管理员权限（请点「以管理员身份重启」）"
        except Exception as exc:
            return False, f"删除文件失败：{exc}"
        store["items"].pop(item.uid, None)
        return True, f"已删除：{item.name}"

    if item.kind == "task":
        ok, out = run_cmd(["schtasks", "/delete", "/tn", item.extra["full_name"], "/f"])
        if not ok:
            return False, f"删除计划任务失败：{out[:200]}"
        store["items"].pop(item.uid, None)
        return True, f"已删除计划任务：{item.name}"

    return False, "未知的启动项类型"


def add_registry_run(name: str, command: str, all_users: bool) -> tuple:
    """新增一个注册表自启项。"""
    if winreg is None:
        return False, "当前系统不支持注册表操作"
    hive = winreg.HKEY_LOCAL_MACHINE if all_users else winreg.HKEY_CURRENT_USER
    sub = r"Software\Microsoft\Windows\CurrentVersion\Run"
    try:
        with winreg.CreateKeyEx(hive, sub, 0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, command)
    except PermissionError:
        return False, "写所有用户的启动项需要管理员权限（请点「以管理员身份重启」）"
    except OSError as exc:
        return False, f"写入注册表失败：{exc}"
    return True, f"已新增：{name}"


def create_shortcut(lnk_path: Path, target: str, args: str = "", workdir: str = "") -> tuple:
    """用 PowerShell 的 WScript.Shell 创建快捷方式。"""
    lnk = str(lnk_path).replace("'", "''")
    tgt = expand(target).replace("'", "''")
    arg = (args or "").replace("'", "''")
    wd = (workdir or os.path.dirname(expand(target))).replace("'", "''")
    script = (
        "$sh = New-Object -ComObject WScript.Shell; "
        f"$s = $sh.CreateShortcut('{lnk}'); "
        f"$s.TargetPath = '{tgt}'; "
        f"$s.Arguments = '{arg}'; "
        f"$s.WorkingDirectory = '{wd}'; "
        "$s.Save()"
    )
    ok, out = run_cmd([shutil.which("powershell") or "powershell", "-NoProfile", "-NonInteractive",
                       "-ExecutionPolicy", "Bypass", "-Command", script], timeout=60)
    if not ok:
        return False, f"创建快捷方式失败：{out[:200]}"
    return True, "已创建快捷方式"


def add_folder_run(name: str, target: str, args: str, all_users: bool) -> tuple:
    """在启动文件夹里创建一个快捷方式。"""
    candidates = [f for f in startup_folders() if f["admin"] == all_users]
    if not candidates:
        return False, "找不到对应的启动文件夹"
    folder = Path(candidates[0]["path"])
    if all_users and not is_admin():
        return False, "写所有用户的启动文件夹需要管理员权限（请点「以管理员身份重启」）"
    try:
        folder.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        return False, "需要管理员权限（请点「以管理员身份重启」）"
    lnk = folder / f"{sanitize(name)}.lnk"
    if lnk.exists():
        return False, f"启动文件夹里已经有同名项：{lnk.name}"
    return create_shortcut(lnk, target, args)


# --------------------------------------------------------------------------- #
# 图形界面
# --------------------------------------------------------------------------- #
class StartupManagerGUI:
    def __init__(self, root):
        self.root = root
        self.items = []
        self.by_uid = {}
        self.scanning = False
        self.sort_col = None
        self.sort_desc = False
        self.include_system_tasks = tk.BooleanVar(value=False)
        self.filter_text = tk.StringVar(value="")
        self.filter_status = tk.StringVar(value="全部")

        root.title(f"{APP_NAME} —— 开机自启一目了然")
        root.geometry("1120x700")
        root.minsize(900, 560)
        try:
            ttk.Style().theme_use("vista")
        except tk.TclError:
            pass

        self._build_ui()
        self.refresh()
        root.after(400, self._admin_banner)

    # ------------------------------------------------------------------ UI --
    def _build_ui(self):
        style = ttk.Style()
        default_font = ("Microsoft YaHei UI", 10)
        style.configure(".", font=default_font)
        style.configure("Treeview", rowheight=26, font=default_font)
        style.configure("Treeview.Heading", font=("Microsoft YaHei UI", 10, "bold"))
        style.configure("Title.TLabel", font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Hint.TLabel", foreground="#7a6a52")

        top = ttk.Frame(self.root, padding=(12, 10, 12, 6))
        top.pack(fill="x")
        ttk.Label(top, text="🥚→🚀 开机启动管理器", style="Title.TLabel").pack(side="left")
        self.admin_label = ttk.Label(top, text="", style="Hint.TLabel")
        self.admin_label.pack(side="left", padx=12)
        ttk.Button(top, text="以管理员身份重启", command=self.on_elevate).pack(side="right")
        ttk.Button(top, text="🔄 刷新列表", command=self.refresh).pack(side="right", padx=6)

        bar = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        bar.pack(fill="x")
        ttk.Label(bar, text="搜索：").pack(side="left")
        entry = ttk.Entry(bar, textvariable=self.filter_text, width=28)
        entry.pack(side="left")
        entry.bind("<KeyRelease>", lambda _e: self._fill_tree())
        ttk.Label(bar, text="   状态：").pack(side="left")
        combo = ttk.Combobox(bar, textvariable=self.filter_status, state="readonly", width=14,
                             values=["全部", "已启用", "已禁用", "文件缺失", "可疑路径"])
        combo.pack(side="left")
        combo.bind("<<ComboboxSelected>>", lambda _e: self._fill_tree())
        ttk.Checkbutton(bar, text="显示微软系统计划任务", variable=self.include_system_tasks,
                        command=self.refresh).pack(side="left", padx=16)

        act = ttk.Frame(self.root, padding=(12, 0, 12, 6))
        act.pack(fill="x")
        self.buttons = {}
        specs = [
            ("disable", "⛔ 禁用所选", self.on_disable),
            ("enable", "✅ 启用所选", self.on_enable),
            ("delete", "🗑 删除所选", self.on_delete),
            ("add", "➕ 新增启动项", self.on_add),
            ("open", "📂 打开文件位置", self.on_open),
            ("copy", "📋 复制命令", self.on_copy),
            ("regedit", "🔍 注册表定位", self.on_regedit),
            ("clean", "🧹 禁用所有无效项", self.on_clean_invalid),
            ("restore", "↩ 还原全部禁用项", self.on_restore_all),
            ("export", "💾 导出清单", self.on_export),
        ]
        for key, text, cmd in specs:
            btn = ttk.Button(act, text=text, command=cmd)
            btn.pack(side="left", padx=3)
            self.buttons[key] = btn

        mid = ttk.Frame(self.root, padding=(12, 0, 12, 0))
        mid.pack(fill="both", expand=True)

        columns = ("name", "status", "source", "command")
        tree = ttk.Treeview(mid, columns=columns, show="headings", selectmode="extended")
        tree.heading("name", text="名称", command=lambda: self._sort_by("name"))
        tree.heading("status", text="状态", command=lambda: self._sort_by("status"))
        tree.heading("source", text="来源", command=lambda: self._sort_by("source"))
        tree.heading("command", text="命令 / 目标文件", command=lambda: self._sort_by("command"))
        tree.column("name", width=270, anchor="w")
        tree.column("status", width=170, anchor="center")
        tree.column("source", width=210, anchor="w")
        tree.column("command", width=470, anchor="w")
        tree.tag_configure("on", foreground="#146c2e")
        tree.tag_configure("on_missing", foreground="#b35c00")
        tree.tag_configure("off", foreground="#8a8a8a")
        tree.tag_configure("off_missing", foreground="#a08a68")
        tree.bind("<<TreeviewSelect>>", lambda _e: self._show_details())
        tree.bind("<Double-1>", lambda _e: self.on_open())
        tree.bind("<Button-3>", self._popup_menu)
        vsb = ttk.Scrollbar(mid, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=vsb.set)
        tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="left", fill="y")
        self.tree = tree

        menu = tk.Menu(self.root, tearoff=0)
        menu.add_command(label="⛔ 禁用", command=self.on_disable)
        menu.add_command(label="✅ 启用", command=self.on_enable)
        menu.add_command(label="🗑 删除", command=self.on_delete)
        menu.add_separator()
        menu.add_command(label="📂 打开文件位置", command=self.on_open)
        menu.add_command(label="📋 复制命令", command=self.on_copy)
        menu.add_command(label="🔍 在注册表编辑器中定位", command=self.on_regedit)
        self.menu = menu

        detail = ttk.LabelFrame(self.root, text=" 详情 ", padding=(10, 6))
        detail.pack(fill="x", padx=12, pady=(8, 4))
        self.detail_vars = {}
        for row, key in enumerate(("名称", "来源", "位置", "命令")):
            ttk.Label(detail, text=f"{key}：", width=6, anchor="e").grid(row=row, column=0, sticky="ne", pady=1)
            var = tk.StringVar(value="-")
            ent = ttk.Entry(detail, textvariable=var, state="readonly")
            ent.grid(row=row, column=1, sticky="we", pady=1)
            self.detail_vars[key] = var
        detail.columnconfigure(1, weight=1)

        bottom = ttk.Frame(self.root, padding=(12, 4, 12, 10))
        bottom.pack(fill="x")
        self.status = tk.StringVar(value="准备就绪")
        ttk.Label(bottom, textvariable=self.status, style="Hint.TLabel").pack(side="left")
        ttk.Label(bottom, text=f"备份区：{DATA_DIR}", style="Hint.TLabel").pack(side="right")

    def _admin_banner(self):
        if is_admin():
            self.admin_label.config(text="已获得管理员权限（可管理所有用户的启动项）")
        else:
            self.admin_label.config(text="当前是普通权限：只能改当前用户的启动项")

    # --------------------------------------------------------------- 扫描 --
    def refresh(self):
        if self.scanning:
            return
        self.scanning = True
        self.status.set("正在扫描启动项…")
        for btn in self.buttons.values():
            btn.state(["disabled"])
        threading.Thread(target=self._scan_worker, daemon=True).start()

    def _scan_worker(self):
        try:
            items = scan_all(bool(self.include_system_tasks.get()))
            err = None
        except Exception as exc:  # 扫描本身不该崩掉界面
            items, err = [], exc
        self.root.after(0, self._after_scan, items, err)

    def _after_scan(self, items, err):
        self.scanning = False
        for btn in self.buttons.values():
            btn.state(["!disabled"])
        if err is not None:
            self.status.set(f"扫描出错：{err}")
            messagebox.showerror(APP_NAME, f"扫描启动项时出错：\n{err}")
            return
        self.items = items
        self.by_uid = {it.uid: it for it in items}
        self._fill_tree()
        enabled = sum(1 for it in items if it.enabled)
        disabled = len(items) - enabled
        missing = sum(1 for it in items if it.missing)
        suspect = sum(1 for it in items if it.suspicious)
        self.status.set(
            f"共 {len(items)} 项：已启用 {enabled} · 已禁用 {disabled} · 文件缺失 {missing} · 可疑路径 {suspect}"
            f"　（扫描时间 {time.strftime('%H:%M:%S')}）"
        )

    def _visible_items(self):
        text = self.filter_text.get().strip().lower()
        mode = self.filter_status.get()
        result = []
        for it in self.items:
            if text and text not in f"{it.name} {it.command} {it.source} {it.location}".lower():
                continue
            if mode == "已启用" and not it.enabled:
                continue
            if mode == "已禁用" and it.enabled:
                continue
            if mode == "文件缺失" and not it.missing:
                continue
            if mode == "可疑路径" and not it.suspicious:
                continue
            result.append(it)
        return result

    def _fill_tree(self):
        self.tree.delete(*self.tree.get_children())
        items = self._visible_items()
        if self.sort_col:
            items.sort(key=lambda it: {
                "name": it.name.lower(),
                "status": (not it.enabled, it.name.lower()),
                "source": it.source.lower(),
                "command": it.command.lower(),
            }[self.sort_col], reverse=self.sort_desc)
        for it in items:
            if it.enabled:
                tag = "on_missing" if (it.missing or it.suspicious) else "on"
            else:
                tag = "off_missing" if it.missing else "off"
            self.tree.insert("", "end", iid=it.uid, values=(it.name, it.status_text, it.source, it.command), tags=(tag,))
        self._show_details()

    def _sort_by(self, col):
        if self.sort_col == col:
            self.sort_desc = not self.sort_desc
        else:
            self.sort_col, self.sort_desc = col, False
        self._fill_tree()

    def selected_items(self):
        return [self.by_uid[u] for u in self.tree.selection() if u in self.by_uid]

    def _show_details(self):
        items = self.selected_items()
        if not items:
            for var in self.detail_vars.values():
                var.set("-")
            return
        it = items[0]
        self.detail_vars["名称"].set(f"{it.name}　（{len(items)} 项被选中）" if len(items) > 1 else it.name)
        self.detail_vars["来源"].set(it.source)
        self.detail_vars["位置"].set(it.location)
        target = it.target
        extra = f"　→ 指向：{it.extra.get('resolved')}" if it.kind == "folder" and it.extra.get("resolved") else ""
        note = "　（文件不存在）" if it.missing else ""
        shown = it.command or "（这个计划任务没有给出命令行，可点「刷新列表」后重试，或在系统自带的任务计划程序里查看）"
        self.detail_vars["命令"].set(f"{shown}{extra}{note}")

    # --------------------------------------------------------------- 动作 --
    def _apply(self, action_name, func, items):
        """对多个启动项执行同一动作，最后统一汇报。"""
        if not items:
            messagebox.showinfo(APP_NAME, "请先在列表里选中一项。")
            return
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
            log_action(f"{action_name} {ok_count} 项：" + "; ".join(it.name for it in items[:20]))
        if messages:
            messagebox.showwarning(APP_NAME, f"完成 {ok_count} 项，失败 {len(messages)} 项：\n\n" + "\n".join(messages[:12]))
        self.refresh()

    def on_disable(self):
        items = [it for it in self.selected_items() if it.enabled]
        skipped = len(self.selected_items()) - len(items)
        if skipped and not items:
            messagebox.showinfo(APP_NAME, "选中的项都已经是禁用状态了。")
            return
        if not items:
            messagebox.showinfo(APP_NAME, "请选中要禁用的启动项（可以按住 Ctrl / Shift 多选）。")
            return
        if not messagebox.askyesno(APP_NAME, f"确定禁用选中的 {len(items)} 个启动项吗？\n\n"
                                             "放心：它们会被挪到本软件的备份区，随时可以「启用」还原。"):
            return
        self._apply("禁用", disable_item, items)

    def on_enable(self):
        items = [it for it in self.selected_items() if not it.enabled]
        if not items:
            messagebox.showinfo(APP_NAME, "请选中要启用的项（已被本软件禁用的项会显示为「已禁用」）。")
            return
        self._apply("启用", enable_item, items)

    def on_delete(self):
        items = self.selected_items()
        if not items:
            messagebox.showinfo(APP_NAME, "请选中要删除的启动项。")
            return
        names = "\n".join(f"· {it.name}（{it.source}）" for it in items[:10])
        if not messagebox.askyesno(APP_NAME, f"彻底删除下面 {len(items)} 项？这个操作不可撤销：\n\n{names}\n\n"
                                             "如果只是想让它不开机自启，用「禁用」更安全。"):
            return
        self._apply("删除", delete_item, items)

    def on_clean_invalid(self):
        items = [it for it in self.items if it.enabled and it.missing]
        if not items:
            messagebox.showinfo(APP_NAME, "没有发现「指向的文件已不存在」的启动项。")
            return
        names = "\n".join(f"· {it.name} → {it.target}" for it in items[:10])
        if not messagebox.askyesno(APP_NAME, f"发现 {len(items)} 个无效启动项（文件已不存在），禁用它们？\n\n{names}"):
            return
        self._apply("禁用无效项", disable_item, items)

    def on_restore_all(self):
        store = load_store()
        if not store["items"]:
            messagebox.showinfo(APP_NAME, "备份区是空的，没有被本软件禁用过的启动项。")
            return
        if not messagebox.askyesno(APP_NAME, f"还原全部 {len(store['items'])} 个被禁用的启动项？"):
            return
        self._apply("还原", enable_item, items_from_store(store))

    def on_open(self):
        items = self.selected_items()
        if not items:
            return
        open_file_location(replace_target(items[0]))

    def on_copy(self):
        items = self.selected_items()
        if not items:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append("\n".join(it.command for it in items))
        self.status.set(f"已复制 {len(items)} 条命令到剪贴板")

    def on_regedit(self):
        items = self.selected_items()
        if not items:
            return
        it = items[0]
        if it.kind != "registry":
            messagebox.showinfo(APP_NAME, "只有注册表启动项才能定位到注册表。")
            return
        hive = it.extra.get("hive") or (load_store()["items"].get(it.uid, {}).get("hive"))
        sub = it.extra.get("sub") or (load_store()["items"].get(it.uid, {}).get("sub"))
        if hive and sub:
            regedit_goto(hive, sub)

    def on_export(self):
        default = f"启动项清单_{time.strftime('%Y%m%d_%H%M%S')}.csv"
        path = filedialog.asksaveasfilename(title="导出启动项清单", initialfile=default,
                                            defaultextension=".csv",
                                            filetypes=[("CSV 表格", "*.csv"), ("JSON", "*.json")])
        if not path:
            return
        try:
            if path.lower().endswith(".json"):
                data = [{
                    "名称": it.name, "状态": it.status_text, "来源": it.source,
                    "位置": it.location, "命令": it.command,
                    "目标文件": it.target, "文件存在": (not it.missing) if it.target else None,
                } for it in self.items]
                Path(path).write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
            else:
                with open(path, "w", newline="", encoding="utf-8-sig") as fh:
                    writer = csv.writer(fh)
                    writer.writerow(["名称", "状态", "来源", "位置", "命令", "目标文件", "文件存在"])
                    for it in self.items:
                        writer.writerow([it.name, it.status_text, it.source, it.location, it.command,
                                         it.target, "" if not it.target else ("是" if not it.missing else "否")])
            self.status.set(f"已导出到 {path}")
            messagebox.showinfo(APP_NAME, f"已导出：\n{path}")
        except Exception as exc:
            messagebox.showerror(APP_NAME, f"导出失败：{exc}")

    def on_add(self):
        AddDialog(self.root, self.refresh)

    def on_elevate(self):
        if is_admin():
            messagebox.showinfo(APP_NAME, "当前已经是管理员权限了。")
            return
        if not messagebox.askyesno(APP_NAME, "将以管理员身份重新打开本软件（会弹出 UAC 提示），继续？"):
            return
        if relaunch_as_admin():
            self.root.destroy()
        else:
            messagebox.showerror(APP_NAME, "提权失败，可能是你取消了 UAC 提示。")

    def _popup_menu(self, event):
        row = self.tree.identify_row(event.y)
        if row:
            if row not in self.tree.selection():
                self.tree.selection_set(row)
            self.menu.tk_popup(event.x_root, event.y_root)


def replace_target(item: Item) -> str:
    """取用于「打开文件位置」的路径。"""
    if item.kind == "folder":
        return item.extra.get("resolved") or item.extra.get("saved") or item.command
    return item.command


class AddDialog(tk.Toplevel):
    """新增启动项的小窗口。"""

    def __init__(self, master, on_done):
        super().__init__(master)
        self.on_done = on_done
        self.title("新增开机启动项")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        body = ttk.Frame(self, padding=14)
        body.pack(fill="both", expand=True)

        self.name_var = tk.StringVar()
        self.cmd_var = tk.StringVar()
        self.args_var = tk.StringVar()
        self.scope_var = tk.StringVar(value="当前用户 · 注册表 Run")

        ttk.Label(body, text="名称：").grid(row=0, column=0, sticky="e", pady=4)
        ttk.Entry(body, textvariable=self.name_var, width=52).grid(row=0, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(body, text="程序：").grid(row=1, column=0, sticky="e", pady=4)
        ttk.Entry(body, textvariable=self.cmd_var, width=42).grid(row=1, column=1, sticky="we", pady=4)
        ttk.Button(body, text="浏览…", command=self._browse).grid(row=1, column=2, padx=(6, 0))

        ttk.Label(body, text="参数：").grid(row=2, column=0, sticky="e", pady=4)
        ttk.Entry(body, textvariable=self.args_var, width=52).grid(row=2, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(body, text="方式：").grid(row=3, column=0, sticky="e", pady=4)
        ttk.Combobox(body, textvariable=self.scope_var, state="readonly", width=49, values=[
            "当前用户 · 注册表 Run",
            "所有用户 · 注册表 Run（需管理员）",
            "当前用户 · 启动文件夹",
            "所有用户 · 启动文件夹（需管理员）",
        ]).grid(row=3, column=1, columnspan=2, sticky="we", pady=4)

        ttk.Label(body, text="提示：只想让它「现在能手动打开、开机别自启」，可以先随便选一种方式建好后，再点「禁用」。",
                  style="Hint.TLabel", wraplength=430, justify="left").grid(row=4, column=0, columnspan=3, sticky="w", pady=(8, 4))

        btns = ttk.Frame(body)
        btns.grid(row=5, column=0, columnspan=3, sticky="e", pady=(10, 0))
        ttk.Button(btns, text="取消", command=self.destroy).pack(side="right", padx=4)
        ttk.Button(btns, text="确定新增", command=self._ok).pack(side="right")
        body.columnconfigure(1, weight=1)

    def _browse(self):
        path = filedialog.askopenfilename(title="选择要开机启动的程序",
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
# 命令行模式
# --------------------------------------------------------------------------- #
def cli_list() -> None:
    items = scan_all(include_system_tasks=True)
    print(f"{'状态':<8} {'来源':<24} {'名称':<34} 命令")
    print("-" * 130)
    for it in sorted(items, key=lambda i: (i.source, i.name.lower())):
        state = "已启用" if it.enabled else "已禁用"
        line = f"{state:<8} {it.source[:22]:<24} {it.name[:32]:<34} {it.command or '（无命令行）'}"
        if it.missing:
            line += "  ［文件缺失］"
        print(line)
    print("-" * 130)
    print(f"共 {len(items)} 项，已启用 {sum(1 for i in items if i.enabled)} 项")


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


def main() -> None:
    if winreg is None:
        print("这个软件只能在 Windows 上运行。")
        return
    argv = sys.argv[1:]
    if "--list" in argv:
        cli_list()
        return
    if "--enable-all" in argv:
        cli_enable_all()
        return
    root = tk.Tk()
    StartupManagerGUI(root)
    root.mainloop()


if __name__ == "__main__":
    main()
