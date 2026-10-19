from __future__ import annotations
import csv
import ipaddress
import json
import os
import queue
import socket
import threading
import time
import tkinter as tk
from dataclasses import dataclass, field
from tkinter import filedialog, messagebox, scrolledtext, ttk
from typing import Callable, Dict, List, Optional, Tuple
from xml.etree import ElementTree as ET
import psutil
from analysis_formatter import AnalysisStreamSummary, parse_find_summary
from app_config import AppConfig, load_config, save_config
from i18n import APP_BRAND, I18n, format_adapter_label
from platform_paths import system_ndi_config_path
from ui_theme import apply_theme, style_text_widget
from ndi_analysis_bridge import find_ndi_analysis_exe, parse_find_output, run_analysis_find, run_analysis_source_streaming
from ndi_bandwidth import format_support_line
from ndi_preview import NdiPreviewWindow
from source_bandwidth_probe import probe_bandwidth_support, support_table_text
from ndi_tools_launcher import format_tools_diagnostic, launch_studio_monitor, launch_tool, list_resolved_tools
try:
    import NDIlib as ndi
except ImportError:
    ndi = None
LIVE_POLL_MS = 800
SCAN_POLL_MS = 1000
SCAN_MIN_SECONDS = 8.0
SCAN_MAX_SECONDS = 20.0
SCAN_STABLE_ROUNDS = 4
DETAIL_CAPTURE_MS = 2500
SYSTEM_NDI_CONFIG = system_ndi_config_path()

@dataclass
class IPv4OnNic:
    address: str
    netmask: str

    @property
    def network(self) -> ipaddress.IPv4Network:
        return ipaddress.ip_network(f'{self.address}/{self.netmask}', strict=False)

    def contains(self, ip: str) -> bool:
        try:
            return ipaddress.ip_address(ip) in self.network
        except ValueError:
            return False

@dataclass
class NetAdapter:
    key: str
    name: str
    mac: str
    is_up: bool
    ipv4: List[IPv4OnNic] = field(default_factory=list)

@dataclass
class NdiSourceRow:
    ndi_name: str
    url_address: str
    host_ip: str
    port: str
    on_selected_nic: bool
    connection_type: str = ''
    bandwidth_support: str = ''
    video_resolution: str = ''

def list_adapters() -> List[NetAdapter]:
    stats = psutil.net_if_stats()
    addrs = psutil.net_if_addrs()
    link_family = getattr(psutil, 'AF_LINK', None)
    out: List[NetAdapter] = []
    for name, addr_list in addrs.items():
        ipv4: List[IPv4OnNic] = []
        mac = ''
        for a in addr_list:
            if link_family is not None and a.family == link_family:
                mac = (a.address or '').replace('-', ':').upper()
            elif a.family == socket.AF_INET:
                if a.address and a.netmask:
                    ipv4.append(IPv4OnNic(a.address, a.netmask))
        if not ipv4 and name.lower().startswith(('loopback', 'lo')):
            continue
        st = stats.get(name)
        is_up = bool(st.isup) if st else False
        out.append(NetAdapter(key=name, name=name, mac=mac, is_up=is_up, ipv4=ipv4))
    out.sort(key=lambda x: (not x.is_up, x.name))
    return out

def parse_ndi_url(url_address: str) -> Tuple[str, str]:
    if not url_address:
        return ('', '')
    if ':' in url_address:
        host, port = url_address.rsplit(':', 1)
        return (host, port)
    return (url_address, '')

def source_matches_nic(host_ip: str, adapter: Optional[NetAdapter]) -> bool:
    if adapter is None:
        return True
    if not host_ip:
        return False
    return any((v4.contains(host_ip) for v4 in adapter.ipv4))

def load_json_file(path: str) -> Optional[dict]:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None

def format_ndi_config_for_nic(cfg: Optional[dict], adapter: Optional[NetAdapter]) -> str:
    lines: List[str] = []
    if cfg is None:
        lines.append('（未找到系統 NDI 設定檔）')
    else:
        lines.append(json.dumps(cfg, ensure_ascii=False, indent=2))
    if adapter and adapter.ipv4:
        lines.append('')
        lines.append('目前網卡網段（列表篩選依此判斷，不修改系統 NDI 設定）：')
        for v4 in adapter.ipv4:
            lines.append(f'  {v4.address}/{v4.netmask}  →  {v4.network}')
    return '\n'.join(lines)

def parse_capabilities_xml(xml_text: str) -> Dict[str, str]:
    out: Dict[str, str] = {}
    if not xml_text or not xml_text.strip():
        return out
    try:
        root = ET.fromstring(xml_text.strip())
    except ET.ParseError:
        return {'raw_metadata': xml_text}
    for k, v in root.attrib.items():
        out[k] = v
    return out

def _source_key(ndi_name: str, url_address: str) -> str:
    return f'{url_address}\x00{ndi_name}'

def sources_to_rows(sources: List, adapter: Optional[NetAdapter], type_by_name: Optional[Dict[str, str]]=None) -> List[NdiSourceRow]:
    type_by_name = type_by_name or {}
    rows: List[NdiSourceRow] = []
    for s in sources:
        host, port = parse_ndi_url(s.url_address)
        rows.append(NdiSourceRow(ndi_name=s.ndi_name, url_address=s.url_address, host_ip=host, port=port, on_selected_nic=source_matches_nic(host, adapter), connection_type=type_by_name.get(s.ndi_name, '')))
    rows.sort(key=lambda r: (r.ndi_name.lower(), r.url_address))
    return rows

def resolution_from_support(support: dict) -> str:
    for key in ('highest', 'max', 'lowest'):
        st = str(support.get(key, '') or '')
        if '×' in st:
            return st.replace('✓', '').strip()
    return ''

def merge_type_hints(rows: List[NdiSourceRow], type_by_name: Dict[str, str]) -> None:
    for r in rows:
        if r.ndi_name in type_by_name:
            r.connection_type = type_by_name[r.ndi_name]
            if not r.bandwidth_support:
                t = r.connection_type.upper()
                hints = []
                if 'HX' in t:
                    hints.append('●HX?')
                if 'NDI' in t:
                    hints.append('●NDI?')
                if hints:
                    r.bandwidth_support = ' '.join(hints) + ' (Analysis)'

class NdiSession:

    def __init__(self) -> None:
        self._initialized = False
        self._finder = None

    def ensure_initialized(self) -> None:
        if self._initialized:
            return
        if ndi is None:
            raise RuntimeError('未安裝 ndi-python，請執行：py -3.13 -m pip install -r requirements.txt')
        if not ndi.initialize():
            raise RuntimeError('NDI 初始化失敗')
        self._initialized = True
        self._recreate_finder()

    def _recreate_finder(self) -> None:
        if self._finder is not None:
            try:
                ndi.find_destroy(self._finder)
            except Exception:
                pass
            self._finder = None
        create = ndi.FindCreate(show_local_sources=True)
        self._finder = ndi.find_create_v2(create)
        if self._finder is None:
            raise RuntimeError('無法建立 NDI 搜尋實例')

    def discover_thorough(self, stop_event: threading.Event, on_progress: Optional[Callable[[float, int], None]]=None) -> List:
        self.ensure_initialized()
        self._recreate_finder()
        merged: Dict[str, object] = {}
        stable_rounds = 0
        last_count = -1
        start = time.monotonic()
        while True:
            if stop_event.is_set():
                break
            elapsed = time.monotonic() - start
            if elapsed >= SCAN_MAX_SECONDS:
                break
            ndi.find_wait_for_sources(self._finder, SCAN_POLL_MS)
            for s in ndi.find_get_current_sources(self._finder) or []:
                merged[_source_key(s.ndi_name, s.url_address)] = s
            count = len(merged)
            if on_progress:
                on_progress(elapsed, count)
            if count == last_count:
                stable_rounds += 1
            else:
                stable_rounds = 0
            last_count = count
            if elapsed >= SCAN_MIN_SECONDS and stable_rounds >= SCAN_STABLE_ROUNDS:
                break
        return list(merged.values())

    def discover_live(self, stop_event: threading.Event, on_tick: Callable[[List], None]) -> List:
        self.ensure_initialized()
        self._recreate_finder()
        merged: Dict[str, object] = {}
        while not stop_event.is_set():
            ndi.find_wait_for_sources(self._finder, LIVE_POLL_MS)
            for s in ndi.find_get_current_sources(self._finder) or []:
                merged[_source_key(s.ndi_name, s.url_address)] = s
            on_tick(list(merged.values()))
        return list(merged.values())

    def make_source(self, row: NdiSourceRow):
        return ndi.Source(p_ndi_name=row.ndi_name, p_url_address=row.url_address)

    def probe_source(self, source) -> Dict[str, object]:
        self.ensure_initialized()
        info: Dict[str, object] = {'ndi_name': source.ndi_name, 'url_address': source.url_address, 'metadata_xml': None, 'capabilities': {}, 'ptz_supported': False, 'web_control': '', 'performance_total': None, 'performance_dropped': None, 'bandwidth_support': {}}
        create = ndi.RecvCreateV3()
        create.bandwidth = ndi.RECV_BANDWIDTH_METADATA_ONLY
        create.color_format = ndi.RECV_COLOR_FORMAT_BEST
        recv = ndi.recv_create_v3(create)
        if recv is None:
            info['error'] = '無法建立接收端'
            return info
        try:
            ndi.recv_connect(recv, source)
            deadline = time.time() + DETAIL_CAPTURE_MS / 1000.0
            while time.time() < deadline:
                frame_type, video, audio, metadata = ndi.recv_capture_v2(recv, min(500, DETAIL_CAPTURE_MS))
                if video is not None:
                    ndi.recv_free_video_v2(recv, video)
                if audio is not None:
                    ndi.recv_free_audio_v2(recv, audio)
                if metadata is not None:
                    info['metadata_xml'] = metadata.data
                    info['capabilities'] = parse_capabilities_xml(metadata.data or '')
                    ndi.recv_free_metadata(recv, metadata)
                    break
                if frame_type == ndi.FRAME_TYPE_NONE:
                    continue
            info['ptz_supported'] = bool(ndi.recv_ptz_is_supported(recv))
            info['web_control'] = ndi.recv_get_web_control(recv) or ''
            perf = ndi.recv_get_performance(recv)
            if isinstance(perf, tuple) and len(perf) >= 2:
                info['performance_total'] = perf[0]
                info['performance_dropped'] = perf[1]
            info['bandwidth_support'] = probe_bandwidth_support(source)
        finally:
            ndi.recv_destroy(recv)
        return info

    def shutdown(self) -> None:
        if self._finder is not None:
            try:
                ndi.find_destroy(self._finder)
            except Exception:
                pass
            self._finder = None
        if self._initialized:
            try:
                ndi.destroy()
            except Exception:
                pass
            self._initialized = False

def format_source_details(row: NdiSourceRow, probe: Optional[Dict[str, object]], adapter: Optional[NetAdapter]) -> str:
    lines = [f'NDI 名稱：{row.ndi_name}', f'連線位址：{row.url_address}', f'主機 IP：{row.host_ip}', f'埠號：{row.port or '（預設）'}', f'與目前網卡同網段：{('是' if row.on_selected_nic else '否')}']
    if row.connection_type:
        lines.append(f'連線類型（Analysis /find）：{row.connection_type}')
    if row.video_resolution:
        lines.append(f'視訊分辨率（實測）：{row.video_resolution}')
    if row.bandwidth_support:
        lines.append(f'清晰度（實測摘要）：{row.bandwidth_support}')
    if probe and probe.get('bandwidth_support'):
        lines.append('')
        lines.append(support_table_text(probe['bandwidth_support']))
    if adapter:
        lines.append(f'目前網卡：{adapter.name}')
        if adapter.mac:
            lines.append(f'網卡 MAC：{adapter.mac}')
        for v4 in adapter.ipv4:
            lines.append(f'  本機 IP {v4.address} / {v4.netmask}  （網段 {v4.network}）')
    if probe:
        if probe.get('error'):
            lines.append(f'\n詳細探測：{probe['error']}')
        else:
            lines.append('\n--- 連線探測 ---')
            lines.append(f'PTZ 支援：{('是' if probe.get('ptz_supported') else '否')}')
            web = probe.get('web_control') or ''
            if web:
                lines.append(f'Web 控制：{web}')
            caps = probe.get('capabilities') or {}
            if caps:
                lines.append('能力（metadata）：')
                for k, v in caps.items():
                    lines.append(f'  {k} = {v}')
            xml_meta = probe.get('metadata_xml')
            if xml_meta:
                lines.append('\n原始 metadata XML：')
                lines.append(str(xml_meta))
            pt = probe.get('performance_total')
            pd = probe.get('performance_dropped')
            if pt is not None and pd is not None:
                lines.append('\n效能計數（總計 / 丟棄）：')
                lines.append(f'  視訊 {pt.video_frames} / {pd.video_frames}，音訊 {pt.audio_frames} / {pd.audio_frames}，metadata {pt.metadata_frames} / {pd.metadata_frames}')
    return '\n'.join(lines)

class NdiAnalysisGuiApp:

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.root.geometry('1040x740')
        self.root.minsize(760, 520)
        self._adapters: List[NetAdapter] = []
        self._adapter_by_label: Dict[str, NetAdapter] = {}
        self._rows: List[NdiSourceRow] = []
        self._session = NdiSession()
        self._work_q: queue.Queue = queue.Queue()
        self._scan_generation = 0
        self._stop_worker = threading.Event()
        self._active_mode: Optional[str] = None
        self._cfg = load_config()
        self._i18n = I18n(self._cfg.language)
        apply_theme(root)
        self._analysis_summary = AnalysisStreamSummary()
        self._preview_window: Optional[NdiPreviewWindow] = None
        self._type_by_name: Dict[str, str] = {}
        self._batch_probing = False
        self._probe_generation = 0
        self.show_all_var = tk.BooleanVar(value=False)
        self._reload_paths()
        self._build_ui()
        self._apply_i18n()
        self.root.protocol('WM_DELETE_WINDOW', self._on_close)
        self.root.after(100, self._poll_worker)
        self._reload_adapters()
        self._update_scan_buttons()
        if ndi is None:
            messagebox.showerror(self._i18n.t('missing_deps_title'), self._i18n.t('missing_deps_body'))
        else:
            self._refresh_ready_status()

    def _build_ui(self) -> None:
        outer_pad = 10
        top = ttk.Frame(self.root, padding=(outer_pad, outer_pad, outer_pad, 4))
        top.pack(fill=tk.X)
        self._lbl_nic = ttk.Label(top, text='')
        self._lbl_nic.pack(side=tk.LEFT)
        self.nic_var = tk.StringVar()
        self.nic_combo = ttk.Combobox(top, textvariable=self.nic_var, state='readonly', width=72)
        self.nic_combo.pack(side=tk.LEFT, padx=(6, 0), fill=tk.X, expand=True)
        self.nic_combo.bind('<<ComboboxSelected>>', self._on_nic_changed)
        self._nic_rate_var = tk.StringVar(value='')
        self._lbl_nic_rate = ttk.Label(top, textvariable=self._nic_rate_var, style='Muted.TLabel')
        self._lbl_nic_rate.pack(side=tk.LEFT, padx=(12, 0))
        self._net_io_sample: Optional[Tuple[int, int, float]] = None
        self.status_var = tk.StringVar(value='')
        self._lf_scan = ttk.LabelFrame(self.root, text='', padding=10)
        self._lf_scan.pack(fill=tk.X, padx=outer_pad, pady=(0, 6))
        scan_bar = ttk.Frame(self._lf_scan)
        scan_bar.pack(fill=tk.X)
        scan_left = ttk.Frame(scan_bar)
        scan_left.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.btn_deep = ttk.Button(scan_left, text='', command=self._start_deep_scan)
        self.btn_deep.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_live = ttk.Button(scan_left, text='', command=self._start_live_scan)
        self.btn_live.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_stop = ttk.Button(scan_left, text='', command=self._stop_scan)
        self.btn_stop.pack(side=tk.LEFT, padx=(0, 12))
        ttk.Separator(scan_left, orient=tk.VERTICAL).pack(side=tk.LEFT, fill=tk.Y, padx=(0, 12))
        self.btn_detail = ttk.Button(scan_left, text='', command=self._probe_selected)
        self.btn_detail.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_analysis_src = ttk.Button(scan_left, text='', command=self._run_analysis_source)
        self.btn_analysis_src.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_analysis_stop = ttk.Button(scan_left, text='', command=self._stop_analysis, style='Secondary.TButton')
        self.btn_analysis_stop.pack(side=tk.LEFT)
        self._btn_help = ttk.Button(scan_bar, text='', command=self._open_about, style='Secondary.TButton')
        self._btn_help.pack(side=tk.RIGHT)
        self._btn_settings = ttk.Button(scan_bar, text='', command=self._open_settings, style='Secondary.TButton')
        self._btn_settings.pack(side=tk.RIGHT, padx=(0, 6))
        self._lf_tools = ttk.LabelFrame(self.root, text='', padding=10)
        self._lf_tools.pack(fill=tk.X, padx=outer_pad, pady=(0, 6))
        tools_bar = ttk.Frame(self._lf_tools)
        tools_bar.pack(fill=tk.X)
        self._btn_preview = ttk.Button(tools_bar, text='', command=self._open_preview)
        self._btn_preview.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_monitor = ttk.Button(tools_bar, text='', command=self._open_studio_monitor)
        self._btn_monitor.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_probe_bw = ttk.Button(tools_bar, text='', command=self._probe_selected_bandwidth)
        self.btn_probe_bw.pack(side=tk.LEFT, padx=(0, 6))
        self.btn_probe_all = ttk.Button(tools_bar, text='', command=self._probe_all_filtered)
        self.btn_probe_all.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_tool_launcher = ttk.Button(tools_bar, text='NDI Launcher', command=lambda: self._open_tool('launcher'), style='Secondary.TButton')
        self._btn_tool_launcher.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_tool_discovery = ttk.Button(tools_bar, text='Discovery', command=lambda: self._open_tool('discovery'), style='Secondary.TButton')
        self._btn_tool_discovery.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_tool_access = ttk.Button(tools_bar, text='Access Manager', command=lambda: self._open_tool('access_manager'), style='Secondary.TButton')
        self._btn_tool_access.pack(side=tk.LEFT, padx=(0, 6))
        self._btn_export = ttk.Button(tools_bar, text='', command=self._export_csv, style='Secondary.TButton')
        self._btn_export.pack(side=tk.RIGHT)
        self._status_frame = ttk.Frame(self.root, style='Surface.TFrame', padding=(outer_pad, 0))
        self._status_frame.pack(fill=tk.X, padx=outer_pad, pady=(0, 6))
        self._lbl_status = ttk.Label(self._status_frame, textvariable=self.status_var, style='Status.TLabel', anchor=tk.W)
        self._lbl_status.pack(fill=tk.X)
        mid = ttk.Panedwindow(self.root, orient=tk.VERTICAL)
        mid.pack(fill=tk.BOTH, expand=True, padx=outer_pad, pady=(0, outer_pad))
        self._lf_sources = ttk.LabelFrame(mid, text='', padding=4)
        list_frame = self._lf_sources
        mid.add(list_frame, weight=1)
        list_tool = ttk.Frame(list_frame)
        list_tool.pack(fill=tk.X, pady=(0, 4))
        self._lbl_search = ttk.Label(list_tool, text='')
        self._lbl_search.pack(side=tk.LEFT)
        self.search_var = tk.StringVar()
        self.search_var.trace_add('write', lambda *_: self._apply_filter_to_tree())
        ttk.Entry(list_tool, textvariable=self.search_var, width=28).pack(side=tk.LEFT, padx=(2, 8), fill=tk.X, expand=True)
        cols = ('name', 'url', 'ip', 'resolution', 'support', 'type', 'match')
        self.tree = ttk.Treeview(list_frame, columns=cols, show='headings', height=8)
        self.tree.heading('name', text='名稱')
        self.tree.heading('url', text='URL')
        self.tree.heading('ip', text='IP')
        self.tree.heading('resolution', text='分辨率')
        self.tree.heading('support', text='支援清晰度')
        self.tree.heading('type', text='類型')
        self.tree.heading('match', text='同網段')
        self.tree.column('name', width=180)
        self.tree.column('url', width=150)
        self.tree.column('ip', width=96)
        self.tree.column('resolution', width=100)
        self.tree.column('support', width=120)
        self.tree.column('type', width=88)
        self.tree.column('match', width=44)
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        sb = ttk.Scrollbar(list_frame, orient=tk.VERTICAL, command=self.tree.yview)
        sb.pack(side=tk.RIGHT, fill=tk.Y)
        self.tree.configure(yscrollcommand=sb.set)
        self.tree.bind('<<TreeviewSelect>>', self._on_tree_select)
        self.tree.bind('<Double-1>', lambda _e: self._open_preview())
        self._tree_menu = tk.Menu(self.tree, tearoff=0)
        self._tree_menu.add_command(label='內建預覽', command=self._open_preview)
        self._tree_menu.add_command(label='Studio Monitor 播放', command=self._open_studio_monitor)
        self._tree_menu.add_command(label='探測清晰度/分辨率', command=self._probe_selected_bandwidth)
        self._tree_menu.add_command(label='Analysis 連線分析', command=self._run_analysis_source)
        self._tree_menu.add_command(label='複製 NDI 名稱', command=self._copy_source_name)
        self._tree_menu.add_command(label='複製 URL', command=self._copy_source_url)
        self.tree.bind('<Button-3>', self._on_tree_context)
        self.tree.bind('<Button-2>', self._on_tree_context)
        bottom = ttk.Panedwindow(mid, orient=tk.HORIZONTAL)
        mid.add(bottom, weight=4)
        self._lf_detail = ttk.LabelFrame(bottom, text='', padding=4)
        detail_frame = self._lf_detail
        bottom.add(detail_frame, weight=2)
        self.detail_text = scrolledtext.ScrolledText(detail_frame, wrap=tk.WORD, font=('Consolas', 10))
        style_text_widget(self.detail_text)
        self.detail_text.pack(fill=tk.BOTH, expand=True)
        self._lf_extended = ttk.LabelFrame(bottom, text='', padding=4)
        bottom.add(self._lf_extended, weight=3)
        self.analysis_note = ttk.Notebook(self._lf_extended)
        self.analysis_note.pack(fill=tk.BOTH, expand=True)
        sum_tab = ttk.Frame(self.analysis_note)
        log_tab = ttk.Frame(self.analysis_note)
        cfg_tab = ttk.Frame(self.analysis_note)
        self.analysis_note.add(sum_tab, text='摘要')
        self.analysis_note.add(log_tab, text='完整日誌')
        self.analysis_note.add(cfg_tab, text='NDI 配置')
        self.config_text = scrolledtext.ScrolledText(cfg_tab, wrap=tk.WORD, font=('Consolas', 9))
        style_text_widget(self.config_text)
        self.config_text.pack(fill=tk.BOTH, expand=True)
        self.analysis_summary_text = scrolledtext.ScrolledText(sum_tab, wrap=tk.WORD, height=6, font=('Consolas', 9))
        style_text_widget(self.analysis_summary_text)
        self.analysis_summary_text.pack(fill=tk.BOTH, expand=True)
        self.analysis_log_text = scrolledtext.ScrolledText(log_tab, wrap=tk.WORD, height=6, font=('Consolas', 9))
        style_text_widget(self.analysis_log_text)
        self.analysis_log_text.pack(fill=tk.BOTH, expand=True)
        for w in (self.analysis_summary_text, self.analysis_log_text):
            w.tag_configure('err', foreground='#c0392b')
            w.tag_configure('info', foreground='#2c3e50')
            w.tag_configure('head', foreground='#1a5276', font=('Consolas', 9, 'bold'))
        self._write_analysis_intro()
        opt = ttk.Frame(self.root, padding=(8, 0, 8, 8))
        opt.pack(fill=tk.X)
        self._chk_show_all = ttk.Checkbutton(opt, text='', variable=self.show_all_var, command=lambda: self._apply_filter_to_tree())
        self._chk_show_all.pack(anchor=tk.W)
        self.root.after(800, self._poll_nic_rate)

    def _poll_nic_rate(self) -> None:
        if not self.root.winfo_exists():
            return
        adapter = self._selected_adapter()
        if adapter is None:
            self._nic_rate_var.set(self._i18n.t('nic_rate_idle'))
            self._net_io_sample = None
        else:
            counters = psutil.net_io_counters(pernic=True)
            stat = counters.get(adapter.key)
            if stat is None:
                self._nic_rate_var.set(self._i18n.t('nic_rate_na'))
                self._net_io_sample = None
            else:
                now = time.monotonic()
                sent, recv_b, ts = self._net_io_sample if self._net_io_sample else (stat.bytes_sent, stat.bytes_recv, now)
                dt = max(now - ts, 0.001)
                if self._net_io_sample is not None:
                    up = (stat.bytes_sent - sent) * 8.0 / dt / 1000000.0
                    down = (stat.bytes_recv - recv_b) * 8.0 / dt / 1000000.0
                    self._nic_rate_var.set(self._i18n.t('nic_rate', up=up, down=down))
                else:
                    self._nic_rate_var.set(self._i18n.t('nic_rate_wait'))
                self._net_io_sample = (stat.bytes_sent, stat.bytes_recv, now)
        self.root.after(1000, self._poll_nic_rate)

    def _apply_i18n(self) -> None:
        tr = self._i18n
        self.root.title(tr.t('window_title'))
        self._lbl_nic.configure(text=tr.t('nic_label'))
        self._lf_scan.configure(text=tr.t('section_scan'))
        self._lf_tools.configure(text=tr.t('section_play'))
        self._lbl_search.configure(text=tr.t('search'))
        self._lf_sources.configure(text=tr.t('frame_sources'))
        self._lf_detail.configure(text=tr.t('frame_detail'))
        self._lf_extended.configure(text=tr.t('frame_extended'))
        self._chk_show_all.configure(text=tr.t('chk_show_all'))
        self.btn_deep.configure(text=tr.t('btn_deep_scan'))
        self.btn_live.configure(text=tr.t('btn_live_scan'))
        self.btn_stop.configure(text=tr.t('btn_stop_scan'))
        self.btn_detail.configure(text=tr.t('btn_probe_full'))
        self.btn_analysis_src.configure(text=tr.t('btn_analysis_src'))
        self.btn_analysis_stop.configure(text=tr.t('btn_analysis_stop'))
        self._btn_settings.configure(text=tr.t('btn_settings'))
        self._btn_help.configure(text=tr.t('btn_help'))
        self.btn_probe_bw.configure(text=tr.t('btn_probe_bw'))
        self.btn_probe_all.configure(text=tr.t('btn_probe_all'))
        self._btn_preview.configure(text=tr.t('btn_preview'))
        self._btn_monitor.configure(text=tr.t('btn_monitor'))
        self._btn_export.configure(text=tr.t('btn_export_csv'))
        self.tree.heading('name', text=tr.t('col_name'))
        self.tree.heading('url', text=tr.t('col_url'))
        self.tree.heading('ip', text=tr.t('col_ip'))
        self.tree.heading('resolution', text=tr.t('col_resolution'))
        self.tree.heading('support', text=tr.t('col_support'))
        self.tree.heading('type', text=tr.t('col_type'))
        self.tree.heading('match', text=tr.t('col_match'))
        self.analysis_note.tab(0, text=tr.t('tab_summary'))
        self.analysis_note.tab(1, text=tr.t('tab_log'))
        self.analysis_note.tab(2, text=tr.t('tab_ndi_cfg'))
        self._tree_menu.entryconfigure(0, label=tr.t('ctx_preview'))
        self._tree_menu.entryconfigure(1, label=tr.t('ctx_monitor'))
        self._tree_menu.entryconfigure(2, label=tr.t('ctx_probe_bw'))
        self._tree_menu.entryconfigure(3, label=tr.t('ctx_analysis'))
        self._tree_menu.entryconfigure(4, label=tr.t('ctx_copy_name'))
        self._tree_menu.entryconfigure(5, label=tr.t('ctx_copy_url'))
        if not self.status_var.get().strip():
            self.status_var.set(tr.t('status_ready_scan'))
        self._reload_adapters()
        self._apply_filter_to_tree()

    def _open_about(self) -> None:
        tr = self._i18n
        dlg = tk.Toplevel(self.root)
        dlg.title(tr.t('about_title'))
        dlg.geometry('400x260')
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.configure(bg='#e8f4fc')
        dlg.grab_set()
        frm = tk.Frame(dlg, bg='#e8f4fc', padx=24, pady=20)
        frm.pack(fill=tk.BOTH, expand=True)
        tk.Label(frm, text=APP_BRAND, bg='#e8f4fc', fg='#1565c0', font=('Microsoft YaHei UI', 16, 'bold')).pack(anchor=tk.W)
        tk.Label(frm, text=tr.t('about_made_by'), bg='#e8f4fc', fg='#1a2b3c', font=('Microsoft YaHei UI', 11)).pack(anchor=tk.W, pady=(8, 18))
        tk.Label(frm, text=tr.t('about_language'), bg='#e8f4fc', fg='#1a2b3c').pack(anchor=tk.W)
        lang_var = tk.StringVar(value=tr.lang)
        lang_row = tk.Frame(frm, bg='#e8f4fc')
        lang_row.pack(anchor=tk.W, pady=6)
        for code, key in (('zh', 'lang_zh'), ('ja', 'lang_ja'), ('en', 'lang_en')):
            tk.Radiobutton(lang_row, text=tr.t(key), variable=lang_var, value=code, bg='#e8f4fc', activebackground='#d6eaf8', fg='#1a2b3c', selectcolor='#90caf9').pack(side=tk.LEFT, padx=(0, 12))

        def apply_lang() -> None:
            code = lang_var.get()
            self._cfg.language = code
            save_config(self._cfg)
            self._i18n.set_language(code)
            self._apply_i18n()
            self._refresh_ready_status()
            self._write_analysis_intro()
        btn_row = tk.Frame(frm, bg='#e8f4fc')
        btn_row.pack(fill=tk.X, pady=(20, 0))
        ttk.Button(btn_row, text=tr.t('about_apply'), command=apply_lang).pack(side=tk.LEFT, padx=(0, 8))
        ttk.Button(btn_row, text=tr.t('about_close'), command=dlg.destroy).pack(side=tk.LEFT)

    def _reload_paths(self) -> None:
        self._cfg = load_config()
        self._analysis_dir = self._cfg.analysis_x64_dir
        self._tools_dir = self._cfg.normalized_tools_dir()
        self._analysis_exe = find_ndi_analysis_exe()
        self._resolved_tools = list_resolved_tools(self._tools_dir)

    def _refresh_ready_status(self) -> None:
        sm = self._resolved_tools.get('studio_monitor')
        tr = self._i18n
        parts = []
        if self._analysis_exe:
            parts.append(tr.t('analysis_ok'))
        else:
            parts.append(tr.t('analysis_missing'))
        if sm:
            parts.append(tr.t('tools_ok'))
        else:
            parts.append(tr.t('tools_partial'))
        self.status_var.set(tr.t('status_ready_parts', parts=' · '.join(parts)))

    def _write_analysis_intro(self) -> None:
        self.analysis_summary_text.delete('1.0', tk.END)
        lines = ['本程式為 NDI Analysis 的圖形前端，並整合 NDI Tools 快捷啟動。', f'Analysis 目錄：{self._analysis_dir}', f'NDI Tools 目錄：{self._tools_dir}', f'NDIAnalysis：{self._analysis_exe or '（未找到）'}', f'Studio Monitor：{self._resolved_tools.get('studio_monitor') or '（未找到）'}', '', '「Analysis 連線分析」的解析結果會顯示在本摘要頁。']
        self.analysis_summary_text.insert(tk.END, '\n'.join(lines))

    def _open_settings(self) -> None:
        dlg = tk.Toplevel(self.root)
        dlg.title('路徑與 Analysis 設定')
        dlg.geometry('620x280')
        dlg.transient(self.root)
        dlg.grab_set()
        v_analysis = tk.StringVar(value=self._cfg.analysis_x64_dir)
        v_tools = tk.StringVar(value=self._cfg.ndi_tools_dir or self._tools_dir)
        v_find = tk.IntVar(value=self._cfg.analysis_find_seconds)
        v_src = tk.IntVar(value=self._cfg.analysis_source_seconds)
        v_deep_af = tk.BooleanVar(value=self._cfg.deep_scan_run_analysis_find)

        def browse(var: tk.StringVar) -> None:
            path = filedialog.askdirectory(parent=dlg)
            if path:
                var.set(path)
        frm = ttk.Frame(dlg, padding=12)
        frm.pack(fill=tk.BOTH, expand=True)
        ttk.Label(frm, text='NDI Analysis x64 目錄').grid(row=0, column=0, sticky=tk.W, pady=4)
        e1 = ttk.Entry(frm, textvariable=v_analysis, width=52)
        e1.grid(row=0, column=1, padx=6)
        ttk.Button(frm, text='瀏覽…', command=lambda: browse(v_analysis)).grid(row=0, column=2)
        ttk.Label(frm, text='NDI Tools 目錄').grid(row=1, column=0, sticky=tk.W, pady=4)
        e2 = ttk.Entry(frm, textvariable=v_tools, width=52)
        e2.grid(row=1, column=1, padx=6)
        ttk.Button(frm, text='瀏覽…', command=lambda: browse(v_tools)).grid(row=1, column=2)
        ttk.Label(frm, text='Analysis /find 秒數').grid(row=2, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(frm, from_=5, to=120, textvariable=v_find, width=8).grid(row=2, column=1, sticky=tk.W)
        ttk.Label(frm, text='Analysis /source 秒數').grid(row=3, column=0, sticky=tk.W, pady=4)
        ttk.Spinbox(frm, from_=10, to=600, textvariable=v_src, width=8).grid(row=3, column=1, sticky=tk.W)
        ttk.Checkbutton(frm, text='深度掃描完成後自動執行 Analysis /find（補充類型資訊）', variable=v_deep_af).grid(row=4, column=0, columnspan=3, sticky=tk.W, pady=8)

        def test_tools() -> None:
            path = v_tools.get().strip().strip('"')
            messagebox.showinfo('NDI Tools 检测', format_tools_diagnostic(path), parent=dlg)

        def save_and_close() -> None:
            self._cfg = AppConfig(analysis_x64_dir=v_analysis.get().strip().strip('"'), ndi_tools_dir=v_tools.get().strip().strip('"'), analysis_find_seconds=int(v_find.get()), analysis_source_seconds=int(v_src.get()), deep_scan_run_analysis_find=bool(v_deep_af.get()), language=self._cfg.language)
            save_config(self._cfg)
            self._reload_paths()
            self._write_analysis_intro()
            self._update_scan_buttons()
            self._refresh_ready_status()
            dlg.destroy()
        btns = ttk.Frame(frm)
        btns.grid(row=5, column=0, columnspan=3, pady=12)
        ttk.Button(btns, text='检测 Tools', command=test_tools).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text='保存', command=save_and_close).pack(side=tk.LEFT, padx=6)
        ttk.Button(btns, text='取消', command=dlg.destroy).pack(side=tk.LEFT)

    def _get_selected_row(self) -> Optional[NdiSourceRow]:
        sel = self.tree.selection()
        if not sel:
            return None
        name = self.tree.item(sel[0], 'values')[0]
        return next((r for r in self._rows if r.ndi_name == name), None)

    def _on_tree_context(self, event) -> None:
        iid = self.tree.identify_row(event.y)
        if iid:
            self.tree.selection_set(iid)
            self._tree_menu.tk_popup(event.x_root, event.y_root)

    def _copy_source_name(self) -> None:
        row = self._get_selected_row()
        if row:
            self.root.clipboard_clear()
            self.root.clipboard_append(row.ndi_name)

    def _copy_source_url(self) -> None:
        row = self._get_selected_row()
        if row:
            self.root.clipboard_clear()
            self.root.clipboard_append(row.url_address)

    def _open_preview(self) -> None:
        if ndi is None:
            return
        row = self._get_selected_row()
        if not row:
            messagebox.showinfo('提示', '請先選擇一個 NDI 來源。')
            return
        if self._is_scanning():
            messagebox.showinfo('提示', '請先停止掃描再預覽。')
            return
        try:
            if self._preview_window:
                try:
                    self._preview_window.close()
                except Exception:
                    pass
            self._preview_window = NdiPreviewWindow(self.root, self._session, row)
        except Exception as exc:
            messagebox.showerror('預覽失敗', str(exc))

    def _open_studio_monitor(self) -> None:
        row = self._get_selected_row()
        if not row:
            messagebox.showinfo('提示', '請先在列表中選擇要播放的 NDI 來源。')
            return

        def on_result(ok: bool, msg: str) -> None:

            def ui() -> None:
                if ok:
                    self.status_var.set(msg)
                else:
                    messagebox.showwarning('Studio Monitor', msg)
            self.root.after(0, ui)
        try:
            launch_studio_monitor(self._tools_dir, row.ndi_name, on_source_result=on_result)
            self.status_var.set(f'正在開新 Monitor：{row.ndi_name}…')
        except FileNotFoundError as exc:
            messagebox.showerror('未找到 Studio Monitor', str(exc))

    def _open_tool(self, key: str) -> None:
        try:
            path = launch_tool(self._tools_dir, key)
            self.status_var.set(f'已啟動：{path}')
        except FileNotFoundError as exc:
            messagebox.showerror('未找到程式', str(exc))

    def _export_csv(self) -> None:
        if not self._rows:
            messagebox.showinfo('提示', '目前沒有可匯出的來源。')
            return
        path = filedialog.asksaveasfilename(defaultextension='.csv', filetypes=[('CSV', '*.csv')], initialfile=f'ndi_sources_{time.strftime('%Y%m%d_%H%M%S')}.csv')
        if not path:
            return
        with open(path, 'w', encoding='utf-8-sig', newline='') as f:
            w = csv.writer(f)
            w.writerow(['ndi_name', 'url', 'ip', 'port', 'resolution', 'support', 'type', 'on_nic'])
            for r in self._rows:
                w.writerow([r.ndi_name, r.url_address, r.host_ip, r.port, r.video_resolution, r.bandwidth_support, r.connection_type, 'yes' if r.on_selected_nic else 'no'])
        self.status_var.set(f'已匯出 {len(self._rows)} 條 → {path}')

    def _selected_adapter(self) -> Optional[NetAdapter]:
        return self._adapter_by_label.get(self.nic_var.get())

    def _reload_adapters(self) -> None:
        self._adapters = list_adapters()
        self._adapter_by_label = {format_adapter_label(a, self._i18n): a for a in self._adapters}
        labels = list(self._adapter_by_label.keys())
        self.nic_combo['values'] = labels
        if labels:
            prefer = next((a for a in self._adapters if a.is_up and a.ipv4), self._adapters[0])
            self.nic_var.set(format_adapter_label(prefer, self._i18n))
        self._update_config_panel()

    def _update_config_panel(self) -> None:
        cfg = load_json_file(SYSTEM_NDI_CONFIG)
        text = format_ndi_config_for_nic(cfg, self._selected_adapter())
        self.config_text.delete('1.0', tk.END)
        self.config_text.insert(tk.END, text)

    def _is_scanning(self) -> bool:
        return self._active_mode in ('deep', 'live', 'analysis')

    def _update_scan_buttons(self) -> None:
        mode = self._active_mode
        scan_busy = mode in ('deep', 'live', 'analysis')
        self.btn_deep.configure(state=tk.DISABLED if scan_busy else tk.NORMAL)
        self.btn_live.configure(state=tk.DISABLED if scan_busy else tk.NORMAL)
        self.btn_stop.configure(state=tk.NORMAL if mode in ('deep', 'live') else tk.DISABLED)
        self.nic_combo.configure(state='disabled' if scan_busy else 'readonly')
        analysis_busy = mode == 'analysis'
        has_analysis = bool(self._analysis_exe)
        self.btn_analysis_src.configure(state=tk.DISABLED if analysis_busy or not has_analysis else tk.NORMAL)
        self.btn_analysis_stop.configure(state=tk.NORMAL if analysis_busy else tk.DISABLED)

    def _append_analysis_log(self, text: str, tag: str='info') -> None:
        self.analysis_log_text.insert(tk.END, text + '\n', tag)
        self.analysis_log_text.see(tk.END)

    def _set_analysis_summary(self, text: str) -> None:
        self.analysis_summary_text.delete('1.0', tk.END)
        self.analysis_summary_text.insert(tk.END, text)

    def _stop_scan(self) -> None:
        if self._active_mode not in ('deep', 'live'):
            return
        self._stop_worker.set()
        self._scan_generation += 1
        was = self._active_mode
        self._active_mode = None
        self._update_scan_buttons()
        mode = self._i18n.t('stopped_live') if was == 'live' else self._i18n.t('stopped_empty')
        self.status_var.set(self._i18n.t('stopped_scan', mode=mode, time=time.strftime('%H:%M:%S')))

    def _stop_analysis(self) -> None:
        if self._active_mode != 'analysis':
            return
        self._stop_worker.set()
        self._scan_generation += 1
        self._active_mode = None
        self._update_scan_buttons()
        self._append_analysis_log(self._i18n.t('analysis_stopped_log'), 'head')
        self.status_var.set(self._i18n.t('analysis_stopped', time=time.strftime('%H:%M:%S')))

    def _begin_scan_mode(self, mode: str, status: str) -> int:
        self._stop_worker.set()
        self._scan_generation += 1
        generation = self._scan_generation
        self._stop_worker.clear()
        self._active_mode = mode
        self._update_scan_buttons()
        self.status_var.set(status)
        return generation

    def _finish_scan_mode(self, generation: int, status: str) -> None:
        if generation != self._scan_generation:
            return
        self._active_mode = None
        self._update_scan_buttons()
        self.status_var.set(status)

    def _on_nic_changed(self, _event=None) -> None:
        self._net_io_sample = None
        self._update_config_panel()
        for r in self._rows:
            r.on_selected_nic = source_matches_nic(r.host_ip, self._selected_adapter())
        self._apply_filter_to_tree()
        if self._active_mode != 'live':
            shown = sum((1 for r in self._rows if r.on_selected_nic)) if not self.show_all_var.get() else len(self._rows)
            if self._rows:
                self.status_var.set(f'已切換網卡，依網段重新篩選（顯示 {shown}/{len(self._rows)}）· {time.strftime('%H:%M:%S')}')

    def _require_adapter(self) -> Optional[NetAdapter]:
        adapter = self._selected_adapter()
        if adapter is None:
            messagebox.showinfo('提示', '沒有可用的網卡。')
        return adapter

    def _start_deep_scan(self) -> None:
        if ndi is None or self._is_scanning():
            return
        adapter = self._require_adapter()
        if adapter is None:
            return
        generation = self._begin_scan_mode('deep', '深度掃描中…（約 8–20 秒，可點「停止掃描」）')

        def worker() -> None:
            err: Optional[str] = None
            rows: List[NdiSourceRow] = []

            def progress(elapsed: float, count: int) -> None:
                self._work_q.put(('scan_progress', generation, 'deep', elapsed, count))
            try:
                sources = self._session.discover_thorough(self._stop_worker, progress)
                rows = sources_to_rows(sources, adapter, self._type_by_name)
                if self._cfg.deep_scan_run_analysis_find and self._analysis_exe and (not self._stop_worker.is_set()):
                    proc = run_analysis_find(self._analysis_exe, self._cfg.analysis_find_seconds)
                    out = (proc.stdout or '') + (proc.stderr or '')
                    self._work_q.put(('analysis_find_inline', generation, out))
            except Exception as exc:
                err = str(exc)
            self._work_q.put(('scan_done', generation, 'deep', rows, err))
        threading.Thread(target=worker, daemon=True).start()

    def _start_live_scan(self) -> None:
        if ndi is None or self._is_scanning():
            return
        adapter = self._require_adapter()
        if adapter is None:
            return
        generation = self._begin_scan_mode('live', '實時掃描中… 列表會持續更新（點「停止掃描」結束）')

        def worker() -> None:
            err: Optional[str] = None
            rows: List[NdiSourceRow] = []

            def on_tick(sources: List) -> None:
                tick_rows = sources_to_rows(sources, adapter, self._type_by_name)
                self._work_q.put(('live_tick', generation, tick_rows))
            try:
                self._session.discover_live(self._stop_worker, on_tick)
            except Exception as exc:
                err = str(exc)
            self._work_q.put(('scan_done', generation, 'live', rows, err))
        threading.Thread(target=worker, daemon=True).start()

    def _row_matches_search(self, row: NdiSourceRow, query: str) -> bool:
        if not query:
            return True
        hay = ' '.join([row.ndi_name, row.url_address, row.host_ip, row.connection_type, row.bandwidth_support, row.video_resolution]).lower()
        return query in hay

    def _filtered_rows(self) -> List[NdiSourceRow]:
        show_all = self.show_all_var.get()
        q = self.search_var.get().strip().lower()
        out: List[NdiSourceRow] = []
        for r in self._rows:
            if not show_all and (not r.on_selected_nic):
                continue
            if not self._row_matches_search(r, q):
                continue
            out.append(r)
        return out

    def _apply_probe_support(self, row: NdiSourceRow, support: dict) -> None:
        row.bandwidth_support = format_support_line(support)
        row.video_resolution = resolution_from_support(support)

    def _probe_selected_bandwidth(self) -> None:
        if ndi is None:
            return
        if self._is_scanning():
            messagebox.showinfo(self._i18n.t('tip_title'), self._i18n.t('stop_scan_probe'))
            return
        if self._batch_probing:
            messagebox.showinfo(self._i18n.t('tip_title'), self._i18n.t('batch_probe_busy'))
            return
        row = self._get_selected_row()
        if row is None:
            messagebox.showinfo(self._i18n.t('tip_title'), self._i18n.t('select_for_probe'))
            return
        self._probe_generation += 1
        generation = self._probe_generation

        def worker() -> None:
            err: Optional[str] = None
            support: dict = {}
            try:
                support = probe_bandwidth_support(self._session.make_source(row))
            except Exception as exc:
                err = str(exc)
            self._work_q.put(('probe_bw_done', generation, row.ndi_name, support, err))
        threading.Thread(target=worker, daemon=True).start()
        self.status_var.set(self._i18n.t('probing_bw'))

    def _finish_batch_probe_ui(self) -> None:
        self._batch_probing = False
        try:
            self.btn_probe_all.configure(state=tk.NORMAL)
            self.btn_probe_bw.configure(state=tk.NORMAL)
        except tk.TclError:
            pass

    def _probe_all_filtered(self) -> None:
        if ndi is None:
            return
        if self._is_scanning():
            messagebox.showinfo(self._i18n.t('tip_title'), self._i18n.t('stop_scan_probe'))
            return
        if self._batch_probing:
            messagebox.showinfo(self._i18n.t('tip_title'), self._i18n.t('batch_probe_busy'))
            return
        rows = self._filtered_rows()
        if not rows:
            messagebox.showinfo(self._i18n.t('tip_title'), self._i18n.t('no_probe_rows'))
            return
        self._batch_probing = True
        self.btn_probe_all.configure(state=tk.DISABLED)
        self.btn_probe_bw.configure(state=tk.DISABLED)
        self._probe_generation += 1
        generation = self._probe_generation
        total = len(rows)

        def worker() -> None:
            err: Optional[str] = None
            try:
                self._session.ensure_initialized()
                for i, row in enumerate(rows, 1):
                    try:
                        support = probe_bandwidth_support(self._session.make_source(row))
                    except Exception as exc:
                        support = {}
                        if err is None:
                            err = f'{row.ndi_name}: {exc}'
                    self._work_q.put(('probe_row_done', generation, row.ndi_name, support, i, total))
            except Exception as exc:
                err = str(exc)
            self._work_q.put(('probe_batch_done', generation, err))
        threading.Thread(target=worker, daemon=True).start()
        self.status_var.set(self._i18n.t('batch_probing', i=0, total=total))

    def _probe_selected(self) -> None:
        if ndi is None or self._is_scanning():
            if self._is_scanning():
                messagebox.showinfo('提示', '請先停止掃描再探測。')
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('提示', '請先在列表中選擇一個 NDI 來源。')
            return
        name = self.tree.item(sel[0], 'values')[0]
        probe_row = next((r for r in self._rows if r.ndi_name == name), None)
        if probe_row is None:
            return
        generation = self._scan_generation

        def worker() -> None:
            err: Optional[str] = None
            probe: Optional[Dict[str, object]] = None
            try:
                probe = self._session.probe_source(self._session.make_source(probe_row))
            except Exception as exc:
                err = str(exc)
            self._work_q.put(('probe_done', generation, name, probe, err))
        threading.Thread(target=worker, daemon=True).start()
        self.status_var.set('正在探測來源與各清晰度模式（約 5–8 秒）…')

    def _run_analysis_source(self) -> None:
        if not self._analysis_exe or self._is_scanning():
            if self._is_scanning():
                messagebox.showinfo('提示', '請先停止其他掃描。')
            return
        sel = self.tree.selection()
        if not sel:
            messagebox.showinfo('提示', '請先選擇要分析的 NDI 來源。')
            return
        name = self.tree.item(sel[0], 'values')[0]
        generation = self._begin_scan_mode('analysis', f'Analysis 連線分析：{name}')
        self._analysis_summary = AnalysisStreamSummary()
        self._append_analysis_log(f'\n>>> /source:"{name}" /time:{self._cfg.analysis_source_seconds}', 'head')
        self._set_analysis_summary(f'正在分析：{name}\n請稍候…')

        def worker() -> None:
            err: Optional[str] = None
            try:
                run_analysis_source_streaming(self._analysis_exe, name, self._cfg.analysis_source_seconds, lambda line: self._work_q.put(('analysis_line', generation, line)), self._stop_worker)
            except Exception as exc:
                err = str(exc)
            self._work_q.put(('analysis_source_done', generation, err))
        threading.Thread(target=worker, daemon=True).start()

    def _poll_worker(self) -> None:
        try:
            while True:
                msg = self._work_q.get_nowait()
                self._handle_worker_msg(msg)
        except queue.Empty:
            pass
        self.root.after(120, self._poll_worker)

    def _handle_worker_msg(self, msg) -> None:
        kind = msg[0]
        if kind == 'scan_progress':
            _, generation, mode, elapsed, count = msg
            if generation != self._scan_generation or self._active_mode != mode:
                return
            self.status_var.set(f'深度掃描中… {elapsed:.0f}s，已累積 {count} 個來源')
            return
        if kind == 'live_tick':
            _, generation, rows = msg
            if generation != self._scan_generation or self._active_mode != 'live':
                return
            self._rows = rows
            self._apply_filter_to_tree()
            shown = sum((1 for r in rows if r.on_selected_nic)) if not self.show_all_var.get() else len(rows)
            self.status_var.set(f'實時掃描中… 共 {len(rows)} 個來源，列表顯示 {shown} 個 · {time.strftime('%H:%M:%S')}')
            return
        if kind == 'scan_done':
            _, generation, mode, rows, err = msg
            if generation != self._scan_generation:
                return
            if mode == 'deep' and rows:
                self._rows = rows
                self._apply_filter_to_tree()
            if err and self._active_mode == mode:
                self._finish_scan_mode(generation, f'錯誤：{err}')
                messagebox.showerror('掃描失敗', err)
                return
            if self._active_mode == mode:
                if mode == 'deep':
                    shown = sum((1 for r in self._rows if r.on_selected_nic)) if not self.show_all_var.get() else len(self._rows)
                    self._finish_scan_mode(generation, f'深度掃描完成：共 {len(self._rows)} 個來源，顯示 {shown} 個 · {time.strftime('%H:%M:%S')}')
                elif mode == 'live':
                    self._finish_scan_mode(generation, f'實時掃描已結束 · {time.strftime('%H:%M:%S')}')
            return
        if kind == 'probe_bw_done':
            _, generation, name, support, err = msg
            if generation != self._probe_generation:
                return
            if err:
                messagebox.showerror(self._i18n.t('probe_fail'), err)
                return
            row = next((r for r in self._rows if r.ndi_name == name), None)
            if row:
                self._apply_probe_support(row, support)
                self._apply_filter_to_tree()
                self._show_detail(row, {'bandwidth_support': support})
            self.status_var.set(self._i18n.t('probe_bw_done', time=time.strftime('%H:%M:%S')))
            return
        if kind == 'probe_row_done':
            _, generation, name, support, index, total = msg
            if generation != self._probe_generation:
                return
            row = next((r for r in self._rows if r.ndi_name == name), None)
            if row:
                self._apply_probe_support(row, support)
                self._apply_filter_to_tree()
            self.status_var.set(self._i18n.t('batch_probing', i=index, total=total))
            return
        if kind == 'probe_batch_done':
            _, generation, err = msg
            self._finish_batch_probe_ui()
            if generation != self._probe_generation:
                return
            if err:
                messagebox.showerror(self._i18n.t('batch_probe_fail'), err)
            self.status_var.set(self._i18n.t('batch_done', time=time.strftime('%H:%M:%S')))
            return
        if kind == 'probe_done':
            _, generation, name, probe, err = msg
            if generation != self._scan_generation:
                return
            if err:
                messagebox.showerror('探測失敗', err)
                return
            row = next((r for r in self._rows if r.ndi_name == name), None)
            if row and probe:
                bw = probe.get('bandwidth_support') or {}
                self._apply_probe_support(row, bw)
                self._apply_filter_to_tree()
                self._show_detail(row, probe)
                web = (probe.get('web_control') or '').strip()
                if web:
                    row_note = f'\n\nWeb 控制：{web}'
                    self.detail_text.insert(tk.END, row_note)
            self.status_var.set(f'探測完成（含清晰度）· {time.strftime('%H:%M:%S')}')
            return
        if kind == 'analysis_line':
            _, generation, line = msg
            if generation != self._scan_generation:
                return
            tag = 'err' if 'error' in line.lower() or 'failed' in line.lower() else 'info'
            self._append_analysis_log(line, tag)
            self._analysis_summary.ingest_line(line)
            self._set_analysis_summary(self._analysis_summary.to_text())
            return
        if kind == 'analysis_find_inline':
            _, generation, output = msg
            if generation != self._scan_generation:
                return
            if output:
                self._append_analysis_log(output, 'info')
                self._set_analysis_summary(parse_find_summary(output))
                for entry in parse_find_output(output):
                    if entry.name and entry.connection_type:
                        self._type_by_name[entry.name] = entry.connection_type
                merge_type_hints(self._rows, self._type_by_name)
                self._apply_filter_to_tree()
            return
        if kind == 'analysis_source_done':
            _, generation, err = msg
            if generation != self._scan_generation:
                return
            if err:
                self._append_analysis_log(f'錯誤：{err}', 'err')
            self._set_analysis_summary(self._analysis_summary.to_text())
            self.analysis_note.select(0)
            self._finish_scan_mode(generation, f'Analysis 連線分析結束 · {time.strftime('%H:%M:%S')}')

    def _apply_filter_to_tree(self) -> None:
        selected = self.tree.selection()
        selected_name = self.tree.item(selected[0], 'values')[0] if selected else None
        self.tree.delete(*self.tree.get_children())
        show_all = self.show_all_var.get()
        restore_iid = None
        q = self.search_var.get().strip().lower()
        for r in self._rows:
            if not show_all and (not r.on_selected_nic):
                continue
            if not self._row_matches_search(r, q):
                continue
            iid = self.tree.insert('', tk.END, values=(r.ndi_name, r.url_address, r.host_ip, r.video_resolution or '—', r.bandwidth_support or '—', r.connection_type, self._i18n.t('yes') if r.on_selected_nic else self._i18n.t('no')))
            if selected_name and r.ndi_name == selected_name:
                restore_iid = iid
        if restore_iid:
            self.tree.selection_set(restore_iid)
            self.tree.focus(restore_iid)

    def _on_tree_select(self, _event=None) -> None:
        sel = self.tree.selection()
        if not sel:
            return
        vals = self.tree.item(sel[0], 'values')
        name = vals[0]
        row = next((r for r in self._rows if r.ndi_name == name), None)
        if row:
            self._show_detail(row, None)

    def _show_detail(self, row: NdiSourceRow, probe: Optional[Dict[str, object]]) -> None:
        text = format_source_details(row, probe, self._selected_adapter())
        if probe is None:
            text += f'\n\n{self._i18n.t('detail_hint')}'
        self.detail_text.delete('1.0', tk.END)
        self.detail_text.insert(tk.END, text)

    def _on_close(self) -> None:
        self._stop_worker.set()
        self._scan_generation += 1
        if self._preview_window:
            try:
                self._preview_window.close()
            except Exception:
                pass
        try:
            self._session.shutdown()
        except Exception:
            pass
        self.root.destroy()

def main() -> None:
    root = tk.Tk()
    NdiAnalysisGuiApp(root)
    root.mainloop()
if __name__ == '__main__':
    main()
