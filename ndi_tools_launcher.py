from __future__ import annotations
import base64
import json
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Dict, Iterable, List, Optional, Tuple
from platform_paths import default_tools_dir_candidates, is_macos
STUDIO_MONITOR_WEB_PORTS = range(80, 90)
STUDIO_MONITOR_WEB_AUTH = ('admin', 'admin')
_STUDIO_MONITOR_PORT_LOCK = threading.Lock()
_STUDIO_MONITOR_CLAIMED_PORTS: set[int] = set()
TOOL_KEYWORDS: Dict[str, Tuple[str, ...]] = {'studio_monitor': ('studio', 'monitor'), 'access_manager': ('access', 'manager'), 'discovery': ('discovery',), 'launcher': ('launcher',), 'test_patterns': ('test', 'pattern')}
TOOL_RELATIVE_PATHS: Dict[str, Tuple[str, ...]] = {'studio_monitor': ('Studio Monitor\\Application.Network.StudioMonitor.x64.exe',), 'access_manager': ('Access Manager\\Application.NdiGroupEditor.exe',), 'discovery': ('Discovery\\Application.NDI.DiscoveryService.UI.exe',), 'launcher': ('NDI Launcher.exe',), 'test_patterns': ('Test Patterns\\Application.Network.TestPatterns.exe',), 'router': ('Router\\Application.NDI.Router.exe',), 'screen_capture': ('Screen Capture\\Application.Network.ScanConverter2.x64.exe',), 'webcam': ('Webcam\\Webcam.exe',)}
TOOL_RELATIVE_PATHS_MAC: Dict[str, Tuple[str, ...]] = {'studio_monitor': ('NDI Studio Monitor.app/Contents/MacOS/NDI Studio Monitor',), 'access_manager': ('NDI Access Manager.app/Contents/MacOS/NDI Access Manager',), 'discovery': ('NDI Discovery.app/Contents/MacOS/NDI Discovery',), 'launcher': ('NDI Launcher.app/Contents/MacOS/NDI Launcher',), 'test_patterns': ('NDI Test Patterns.app/Contents/MacOS/NDI Test Patterns',)}
TOOL_EXACT_NAMES: Dict[str, Tuple[str, ...]] = {'studio_monitor': ('NDI Studio Monitor.exe', 'Studio Monitor.exe', 'Application.StudioMonitor.exe', 'NDI.StudioMonitor.exe'), 'access_manager': ('NDI Access Manager.exe', 'Access Manager.exe'), 'discovery': ('NDI Discovery.exe', 'Discovery.exe'), 'launcher': ('NDI Launcher.exe', 'Launcher.exe'), 'test_patterns': ('NDI Test Patterns.exe', 'Test Patterns.exe')}
EXCLUDE_NAME_PARTS = ('unins', 'setup', 'install', 'update', 'webcamhelp')

def _norm_dir(path: str) -> str:
    return os.path.normpath(path.strip().strip('"'))

def _name_matches(path: str, keywords: Tuple[str, ...]) -> bool:
    base = os.path.basename(path).lower()
    if any((x in base for x in EXCLUDE_NAME_PARTS)):
        return False
    return all((k in base for k in keywords))

def _tool_relative_paths(tool_key: str) -> Tuple[str, ...]:
    if is_macos():
        return TOOL_RELATIVE_PATHS_MAC.get(tool_key, TOOL_RELATIVE_PATHS.get(tool_key, ()))
    return TOOL_RELATIVE_PATHS.get(tool_key, ())

def _iter_search_roots(tools_dir: str) -> List[str]:
    roots: List[str] = []
    td = _norm_dir(tools_dir)
    if td and os.path.isdir(td):
        roots.append(td)
    if is_macos():
        for extra in default_tools_dir_candidates():
            extra = _norm_dir(extra)
            if extra and os.path.isdir(extra) and (extra not in roots):
                roots.append(extra)
        return roots
    pf = os.environ.get('ProgramFiles', 'C:\\Program Files')
    for extra in (os.path.join(pf, 'NDI', 'NDI 6 Tools'), os.path.join(pf, 'NDI', 'NDI 5 Tools'), os.path.join(os.environ.get('LOCALAPPDATA', ''), 'Programs', 'NDI'), os.path.join(os.environ.get('ProgramData', ''), 'Microsoft', 'Windows', 'Start Menu', 'Programs', 'NDI')):
        extra = _norm_dir(extra)
        if extra and os.path.isdir(extra) and (extra not in roots):
            roots.append(extra)
    return roots

def _iter_exes(roots: Iterable[str]) -> List[str]:
    found: List[str] = []
    seen = set()
    for root in roots:
        for dirpath, _dirnames, filenames in os.walk(root):
            if dirpath[len(root):].count(os.sep) > 8:
                continue
            for name in filenames:
                low = name.lower()
                if not low.endswith('.exe'):
                    continue
                full = os.path.abspath(os.path.join(dirpath, name))
                key = os.path.normcase(full)
                if key in seen:
                    continue
                seen.add(key)
                found.append(full)
    return found

def find_tool_exe(tools_dir: str, tool_key: str) -> Optional[str]:
    roots = _iter_search_roots(tools_dir)
    if not roots:
        return None
    td = _norm_dir(tools_dir)
    if td and os.path.isdir(td):
        for rel in _tool_relative_paths(tool_key):
            direct = os.path.join(td, rel)
            if os.path.isfile(direct):
                return os.path.abspath(direct)
    exact = TOOL_EXACT_NAMES.get(tool_key, ())
    for root in roots:
        for name in exact:
            direct = os.path.join(root, name)
            if os.path.isfile(direct):
                return os.path.abspath(direct)
    keywords = TOOL_KEYWORDS.get(tool_key)
    if not keywords:
        return None
    candidates = [p for p in _iter_exes(roots) if _name_matches(p, keywords)]
    if tool_key == 'discovery':
        ui = [p for p in candidates if '\\discovery\\' in p.lower() and 'ui' in p.lower()]
        if ui:
            candidates = ui
        else:
            candidates = [p for p in candidates if 'service' not in os.path.basename(p).lower()] or candidates
    if not candidates:
        return None
    td = _norm_dir(tools_dir).lower() if tools_dir else ''

    def sort_key(p: str) -> Tuple[int, int, str]:
        in_user = 0 if td and p.lower().startswith(td) else 1
        return (in_user, len(p), p.lower())
    candidates.sort(key=sort_key)
    return candidates[0]

def list_resolved_tools(tools_dir: str) -> Dict[str, Optional[str]]:
    keys = set(TOOL_KEYWORDS) | set(TOOL_RELATIVE_PATHS)
    return {key: find_tool_exe(tools_dir, key) for key in sorted(keys)}

def format_tools_diagnostic(tools_dir: str) -> str:
    td = _norm_dir(tools_dir)
    lines = [f'設定目錄：{td}', f'目錄存在：{(os.path.isdir(td) if td else False)}', '']
    resolved = list_resolved_tools(tools_dir)
    lines.append('已解析工具：')
    for key, path in resolved.items():
        lines.append(f'  {key}: {path or '（未找到）'}')
    roots = _iter_search_roots(tools_dir)
    exes = _iter_exes(roots)
    lines.append('')
    lines.append(f'掃描到 {len(exes)} 個 .exe（前 25 個）：')
    for p in exes[:25]:
        lines.append(f'  {p}')
    if len(exes) > 25:
        lines.append(f'  … 另有 {len(exes) - 25} 個')
    return '\n'.join(lines)

def launch_tool(tools_dir: str, tool_key: str, extra_args: Optional[List[str]]=None) -> str:
    exe = find_tool_exe(tools_dir, tool_key)
    if not exe:
        diag = format_tools_diagnostic(tools_dir)
        raise FileNotFoundError(f'未找到 {tool_key}。\n{diag}')
    cmd = [exe] + (extra_args or [])
    subprocess.Popen(cmd, cwd=os.path.dirname(exe))
    return exe

def _studio_monitor_web_ports(timeout: float=0.12) -> List[int]:
    alive: List[int] = []
    for port in STUDIO_MONITOR_WEB_PORTS:
        url = f'http://127.0.0.1:{port}/v1/configuration'
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                if resp.status == 200:
                    alive.append(port)
        except urllib.error.HTTPError as exc:
            if exc.code in (401, 403):
                alive.append(port)
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return alive

def _claim_new_studio_monitor_port(ports_before: set[int]) -> Optional[int]:
    now = set(_studio_monitor_web_ports())
    newcomers = sorted((p for p in now if p not in ports_before))
    with _STUDIO_MONITOR_PORT_LOCK:
        for port in newcomers:
            if port not in _STUDIO_MONITOR_CLAIMED_PORTS:
                _STUDIO_MONITOR_CLAIMED_PORTS.add(port)
                return port
    return None

def _release_studio_monitor_port(port: int) -> None:
    with _STUDIO_MONITOR_PORT_LOCK:
        _STUDIO_MONITOR_CLAIMED_PORTS.discard(port)

def _studio_monitor_post_source(port: int, source_name: str) -> bool:
    url = f'http://127.0.0.1:{port}/v1/configuration'
    body = json.dumps({'version': 1, 'NDI_source': source_name}, ensure_ascii=False).encode('utf-8')
    auth_b64 = base64.b64encode(f'{STUDIO_MONITOR_WEB_AUTH[0]}:{STUDIO_MONITOR_WEB_AUTH[1]}'.encode()).decode('ascii')
    for headers in ({'Content-Type': 'application/json'}, {'Content-Type': 'application/json', 'Authorization': f'Basic {auth_b64}'}):
        req = urllib.request.Request(url, data=body, method='POST', headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                continue
        except (urllib.error.URLError, OSError, TimeoutError):
            continue
    return False

def _studio_monitor_launch_and_tune(exe: str, source_name: str, on_result: Optional[Callable[[bool, str], None]], timeout_sec: float=20.0) -> None:
    ports_before = set(_studio_monitor_web_ports())
    subprocess.Popen([exe], cwd=os.path.dirname(exe))
    deadline = time.time() + timeout_sec
    claimed: Optional[int] = None
    ok = False
    try:
        while time.time() < deadline:
            if claimed is None:
                claimed = _claim_new_studio_monitor_port(ports_before)
            if claimed is not None and _studio_monitor_post_source(claimed, source_name):
                ok = True
                msg = f'已開新 Monitor 播放：{source_name}'
                if on_result:
                    on_result(True, msg)
                return
            time.sleep(0.35)
    finally:
        if claimed is not None and (not ok):
            _release_studio_monitor_port(claimed)
    msg = f'Monitor 已啟動，但未能自動選源。\n請確認已開啟「Allow Web Control」，或在新窗口手動選擇：\n{source_name}'
    if on_result:
        on_result(False, msg)

def launch_studio_monitor(tools_dir: str, source_name: Optional[str]=None, on_source_result: Optional[Callable[[bool, str], None]]=None) -> str:
    exe = find_tool_exe(tools_dir, 'studio_monitor')
    if not exe:
        raise FileNotFoundError(format_tools_diagnostic(tools_dir))
    if source_name:
        threading.Thread(target=_studio_monitor_launch_and_tune, args=(exe, source_name, on_source_result), daemon=True).start()
    else:
        subprocess.Popen([exe], cwd=os.path.dirname(exe))
    return exe
