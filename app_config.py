from __future__ import annotations
import json
import os
from dataclasses import dataclass, asdict
from typing import Optional
from platform_paths import default_analysis_dirs, default_tools_dir_candidates
APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(APP_DIR, 'app_config.json')
LEGACY_ANALYSIS_CONFIG = os.path.join(APP_DIR, 'analysis_config.json')
DEFAULT_ANALYSIS_X64 = default_analysis_dirs()[0]
DEFAULT_TOOLS_CANDIDATES = default_tools_dir_candidates()

@dataclass
class AppConfig:
    analysis_x64_dir: str = DEFAULT_ANALYSIS_X64
    ndi_tools_dir: str = ''
    analysis_find_seconds: int = 15
    analysis_source_seconds: int = 120
    deep_scan_run_analysis_find: bool = False
    language: str = 'zh'

    def normalized_tools_dir(self) -> str:
        custom = self.ndi_tools_dir.strip().strip('"') if self.ndi_tools_dir else ''
        if custom:
            return os.path.normpath(custom)
        for candidate in DEFAULT_TOOLS_CANDIDATES:
            if os.path.isdir(candidate):
                return os.path.normpath(candidate)
        return os.path.normpath(DEFAULT_TOOLS_CANDIDATES[0])

def _load_legacy_analysis_dir() -> Optional[str]:
    if not os.path.isfile(LEGACY_ANALYSIS_CONFIG):
        return None
    try:
        with open(LEGACY_ANALYSIS_CONFIG, 'r', encoding='utf-8') as f:
            data = json.load(f)
        return (data.get('analysis_x64_dir') or '').strip() or None
    except (OSError, json.JSONDecodeError):
        return None

def load_config() -> AppConfig:
    if os.path.isfile(CONFIG_PATH):
        try:
            with open(CONFIG_PATH, 'r', encoding='utf-8') as f:
                data = json.load(f)
            lang = (data.get('language') or 'zh').strip().lower()
            if lang not in ('zh', 'ja', 'en'):
                lang = 'zh'
            return AppConfig(analysis_x64_dir=(data.get('analysis_x64_dir') or DEFAULT_ANALYSIS_X64).strip(), ndi_tools_dir=(data.get('ndi_tools_dir') or '').strip(), analysis_find_seconds=int(data.get('analysis_find_seconds', 15)), analysis_source_seconds=int(data.get('analysis_source_seconds', 120)), deep_scan_run_analysis_find=bool(data.get('deep_scan_run_analysis_find', False)), language=lang)
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            pass
    legacy = _load_legacy_analysis_dir()
    cfg = AppConfig()
    if legacy:
        cfg.analysis_x64_dir = legacy
    return cfg

def save_config(cfg: AppConfig) -> None:
    with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
        json.dump(asdict(cfg), f, ensure_ascii=False, indent=2)
