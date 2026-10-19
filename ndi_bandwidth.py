from __future__ import annotations
from typing import List, Tuple
try:
    import NDIlib as ndi
except ImportError:
    ndi = None
BANDWIDTH_OPTIONS: List[Tuple[str, object, str, bool, str]] = []

def init_bandwidth_options() -> None:
    global BANDWIDTH_OPTIONS
    if BANDWIDTH_OPTIONS:
        return
    if ndi is None:
        return
    BANDWIDTH_OPTIONS = [('max', ndi.RECV_BANDWIDTH_MAX, '最大（MAX）', True, 'MAX'), ('highest', ndi.RECV_BANDWIDTH_HIGHEST, 'Full NDI（最高畫質）', True, 'Full'), ('lowest', ndi.RECV_BANDWIDTH_LOWEST, 'HX / 低碼率', True, 'HX'), ('audio', ndi.RECV_BANDWIDTH_AUDIO_ONLY, '僅音訊', False, 'Audio'), ('metadata', ndi.RECV_BANDWIDTH_METADATA_ONLY, '僅 Metadata', False, 'Meta')]

def get_bandwidth_options() -> List[Tuple[str, object, str, bool, str]]:
    init_bandwidth_options()
    return BANDWIDTH_OPTIONS

def bandwidth_by_id(bid: str):
    init_bandwidth_options()
    for oid, bw, _d, _v, _s in get_bandwidth_options():
        if oid == bid:
            return bw
    return ndi.RECV_BANDWIDTH_LOWEST if ndi else None

def format_support_line(support: dict) -> str:
    init_bandwidth_options()
    parts = []
    for oid, _bw, _desc, _vid, short in get_bandwidth_options():
        st = str(support.get(oid, '') or '')
        if st.startswith('✓'):
            parts.append(f'●{short}')
    return ' '.join(parts) if parts else '（未探測）'
