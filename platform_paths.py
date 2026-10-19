from __future__ import annotations
import os
import platform
import sys

def system_name() -> str:
    return platform.system()

def is_windows() -> bool:
    return sys.platform.startswith('win')

def is_macos() -> bool:
    return sys.platform == 'darwin'

def system_ndi_config_path() -> str:
    if is_windows():
        base = os.environ.get('PROGRAMDATA', 'C:\\ProgramData')
        return os.path.join(base, 'NDI', 'ndi-config.v1.json')
    if is_macos():
        return os.path.expanduser('~/Library/Preferences/NDI/ndi-config.v1.json')
    return os.path.expanduser('~/.config/ndi/ndi-config.v1.json')

def default_analysis_dirs() -> tuple[str, ...]:
    if is_macos():
        return ('/Applications/NDI/NDI Analysis', os.path.expanduser('~/Applications/NDI/NDI Analysis'), '/Applications/NDI Analysis')
    return ('D:\\Program Files\\NDI\\NDI 6 Analysis\\x64', os.path.join(os.environ.get('ProgramFiles', 'C:\\Program Files'), 'NDI', 'NDI 6 Analysis', 'x64'))

def default_tools_dir_candidates() -> tuple[str, ...]:
    if is_macos():
        return ('/Applications/NDI Tools', '/Applications/NDI/NDI 6 Tools', os.path.expanduser('~/Applications/NDI Tools'))
    return ('C:\\Program Files\\NDI\\NDI 6 Tools', 'D:\\Program Files\\NDI\\NDI 6 Tools', os.path.join(os.environ.get('ProgramFiles', 'C:\\Program Files'), 'NDI', 'NDI 6 Tools'))

def analysis_binary_names() -> tuple[str, ...]:
    if is_macos():
        return ('NDIAnalysis', 'NDIAnalysis64', 'NDIAnalysis.app')
    return ('NDIAnalysis.exe', 'NDIAnalysis64.exe')
