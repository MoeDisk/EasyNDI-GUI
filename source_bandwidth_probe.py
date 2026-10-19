from __future__ import annotations
import time
from typing import Dict, Tuple
from ndi_bandwidth import get_bandwidth_options, init_bandwidth_options
try:
    import NDIlib as ndi
except ImportError:
    ndi = None
PROBE_SECONDS = 1.1

def probe_bandwidth_support(source) -> Dict[str, str]:
    init_bandwidth_options()
    if ndi is None:
        return {}
    out: Dict[str, str] = {}
    for oid, bw, desc, expect_video, _short in get_bandwidth_options():
        out[oid] = _probe_one_mode(source, bw, expect_video, oid)
    return out

def _probe_one_mode(source, bandwidth, expect_video: bool, oid: str) -> str:
    create = ndi.RecvCreateV3()
    create.bandwidth = bandwidth
    create.color_format = ndi.RECV_COLOR_FORMAT_FASTEST
    recv = ndi.recv_create_v3(create)
    if recv is None:
        return '—'
    got_video = False
    got_audio = False
    got_meta = False
    video_info = ''
    try:
        ndi.recv_connect(recv, source)
        end = time.monotonic() + PROBE_SECONDS
        while time.monotonic() < end:
            _t, video, audio, metadata = ndi.recv_capture_v2(recv, 120)
            if video is not None:
                got_video = True
                video_info = f'{video.xres}×{video.yres}'
                ndi.recv_free_video_v2(recv, video)
                if expect_video:
                    break
            if audio is not None:
                got_audio = True
                ndi.recv_free_audio_v2(recv, audio)
            if metadata is not None:
                got_meta = True
                ndi.recv_free_metadata(recv, metadata)
            if oid == 'metadata' and got_meta:
                break
            if oid == 'audio' and got_audio and (not expect_video):
                break
    except Exception:
        return '—'
    finally:
        try:
            ndi.recv_destroy(recv)
        except Exception:
            pass
    if oid == 'metadata':
        return '✓' if got_meta else '—'
    if oid == 'audio':
        return '✓' if got_audio else '—'
    if expect_video:
        if got_video:
            return f'✓ {video_info}' if video_info else '✓'
        return '—'
    return '—'

def support_table_text(support: Dict[str, str]) -> str:
    init_bandwidth_options()
    lines = ['支援的接收清晰度（實測，約 1 秒/項）：', '']
    for oid, _bw, desc, _ev, short in get_bandwidth_options():
        st = support.get(oid, '—')
        mark = '✓' if str(st).startswith('✓') else '—'
        detail = st[2:].strip() if str(st).startswith('✓') and len(str(st)) > 2 else ''
        col = f'[{short:5}] {desc:22}'
        lines.append(f'  {mark}  {col}  {detail}')
    return '\n'.join(lines)
