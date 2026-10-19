from __future__ import annotations
import glob
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional, Tuple
FIND_ENTRY_RE = re.compile('name="([^"]*)"(?:\\s+host="([^"]*)")?(?:\\s+port="([^"]*)")?(?:\\s+type="([^"]*)")?', re.IGNORECASE)
from app_config import load_config
from platform_paths import analysis_binary_names, default_analysis_dirs, is_macos
EXE_CANDIDATE_NAMES = analysis_binary_names()

@dataclass
class AnalysisFindEntry:
    name: str
    host: str = ''
    port: str = ''
    connection_type: str = ''

def load_analysis_x64_dir() -> str:
    return load_config().analysis_x64_dir

def exe_in_directory(directory: str) -> Optional[str]:
    if not directory:
        return None
    for name in EXE_CANDIDATE_NAMES:
        candidate = os.path.join(directory, name)
        if os.path.isfile(candidate):
            return os.path.abspath(candidate)
        if name.endswith('.app'):
            inner = os.path.join(candidate, 'Contents', 'MacOS', name.replace('.app', ''))
            if os.path.isfile(inner):
                return os.path.abspath(inner)
    return None

def _extra_search_directories() -> List[str]:
    dirs: List[str] = [load_analysis_x64_dir()]
    for path in default_analysis_dirs():
        dirs.append(path)
    if is_macos():
        out: List[str] = []
        seen = set()
        for d in dirs:
            norm = os.path.normcase(os.path.abspath(d))
            if norm in seen:
                continue
            seen.add(norm)
            out.append(d)
        return out
    for env_key in ('ProgramFiles', 'ProgramFiles(x86)'):
        base = os.environ.get(env_key)
        if not base:
            continue
        dirs.append(os.path.join(base, 'NDI', 'NDI 6 Analysis', 'x64'))
        dirs.append(os.path.join(base, 'NDI', 'NDI Analysis', 'x64'))
    for root in ('D:\\Program Files\\NDI', 'C:\\Program Files\\NDI'):
        if os.path.isdir(root):
            dirs.append(os.path.join(root, 'NDI 6 Analysis', 'x64'))
    out: List[str] = []
    seen = set()
    for d in dirs:
        norm = os.path.normcase(os.path.abspath(d))
        if norm in seen:
            continue
        seen.add(norm)
        out.append(d)
    return out

def find_ndi_analysis_exe() -> Optional[str]:
    for directory in _extra_search_directories():
        found = exe_in_directory(directory)
        if found:
            return found
    candidates: List[str] = []
    for bin_name in EXE_CANDIDATE_NAMES:
        which = shutil.which(bin_name)
        if which:
            candidates.append(which)
    for pattern in ('D:\\Program Files\\NDI\\**\\NDIAnalysis.exe', os.path.join(os.environ.get('ProgramFiles', 'C:\\Program Files'), 'NDI', '**', 'NDIAnalysis.exe')):
        for path in glob.glob(pattern, recursive=True):
            candidates.append(path)
    seen = set()
    for path in candidates:
        norm = os.path.normcase(os.path.abspath(path))
        if norm in seen:
            continue
        seen.add(norm)
        if os.path.isfile(path):
            return path
    return None

def analysis_runtime_for_exe(exe: str) -> Tuple[str, Dict[str, str]]:
    exe_dir = os.path.dirname(os.path.abspath(exe))
    env = os.environ.copy()
    env['PATH'] = exe_dir + os.pathsep + env.get('PATH', '')
    return (exe_dir, env)

def parse_find_output(text: str) -> List[AnalysisFindEntry]:
    entries: List[AnalysisFindEntry] = []
    for line in text.splitlines():
        line = line.strip()
        if 'name="' not in line:
            continue
        m = FIND_ENTRY_RE.search(line)
        if not m:
            continue
        entries.append(AnalysisFindEntry(name=m.group(1) or '', host=m.group(2) or '', port=m.group(3) or '', connection_type=m.group(4) or ''))
    return entries

def run_analysis_find(exe: str, timeout_sec: int=15, groups: Optional[str]=None) -> subprocess.CompletedProcess:
    if groups:
        cmd = [exe, f'/find:"{groups}"', f'/time:{timeout_sec}']
    else:
        cmd = [exe, '/find', f'/time:{timeout_sec}']
    cwd, env = analysis_runtime_for_exe(exe)
    return subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=timeout_sec + 30, cwd=cwd, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)

def run_analysis_source_streaming(exe: str, source_name: str, time_sec: int, on_line: Callable[[str], None], stop_event) -> int:
    cmd = [exe, f'/source:"{source_name}"', f'/time:{time_sec}']
    cwd, env = analysis_runtime_for_exe(exe)
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace', cwd=cwd, env=env, creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    try:
        if proc.stdout:
            for line in proc.stdout:
                if stop_event.is_set():
                    proc.terminate()
                    break
                on_line(line.rstrip('\n'))
        return proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        return -1
