from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Dict, List
from ndi_analysis_bridge import AnalysisFindEntry, parse_find_output
VIDEO_FMT_RE = re.compile('Video format changed\\.\\s*(.+)', re.I)
AUDIO_FMT_RE = re.compile('Audio format changed\\.\\s*(.+)', re.I)
VIDEO_RATE_RE = re.compile('Video data rate \\(MBps\\)\\.\\s*Avg=([\\d.]+)', re.I)
AUDIO_RATE_RE = re.compile('Audio data rate \\(MBps\\)\\.\\s*Avg=([\\d.]+)', re.I)
CAP_RE = re.compile('Capabilities,\\s*(.+)', re.I)
PRODUCT_RE = re.compile('Product,\\s*(.+)', re.I)
CONN_RE = re.compile('(Video|Audio) connection opened', re.I)
ERROR_RE = re.compile('error|failed|could not', re.I)

@dataclass
class AnalysisStreamSummary:
    lines_kept: List[str] = field(default_factory=list)
    video_format: str = ''
    audio_format: str = ''
    video_mbps: str = ''
    audio_mbps: str = ''
    capabilities: List[str] = field(default_factory=list)
    products: List[str] = field(default_factory=list)
    connections: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def ingest_line(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.lines_kept.append(line)
        if ERROR_RE.search(line):
            self.warnings.append(line)
        m = VIDEO_FMT_RE.search(line)
        if m:
            self.video_format = m.group(1).strip()
        m = AUDIO_FMT_RE.search(line)
        if m:
            self.audio_format = m.group(1).strip()
        m = VIDEO_RATE_RE.search(line)
        if m:
            self.video_mbps = m.group(1)
        m = AUDIO_RATE_RE.search(line)
        if m:
            self.audio_mbps = m.group(1)
        m = CAP_RE.search(line)
        if m:
            self.capabilities.append(m.group(1).strip())
        m = PRODUCT_RE.search(line)
        if m:
            self.products.append(m.group(1).strip())
        m = CONN_RE.search(line)
        if m:
            self.connections.append(line)

    def to_text(self) -> str:
        parts = ['═══ Analysis 連線摘要 ═══']
        if self.connections:
            parts.append('連線事件：')
            parts.extend((f'  · {x}' for x in self.connections[-6:]))
        if self.video_format:
            parts.append(f'視訊：{self.video_format}')
        if self.audio_format:
            parts.append(f'音訊：{self.audio_format}')
        if self.video_mbps:
            parts.append(f'視訊碼率（約）：{self.video_mbps} MB/s')
        if self.audio_mbps:
            parts.append(f'音訊碼率（約）：{self.audio_mbps} MB/s')
        if self.products:
            parts.append('產品資訊：')
            parts.extend((f'  · {x}' for x in self.products[:8]))
        if self.capabilities:
            parts.append('能力：')
            parts.extend((f'  · {x}' for x in self.capabilities[:12]))
        if self.warnings:
            parts.append('注意：')
            parts.extend((f'  ! {x}' for x in self.warnings[-8:]))
        if len(parts) == 1:
            parts.append('（尚無可解析的統計行，請查看「完整日誌」）')
        return '\n'.join(parts)

def format_find_table(entries: List[AnalysisFindEntry]) -> str:
    if not entries:
        return '（Analysis /find 未解析到來源）'
    lines = ['═══ Analysis /find 結果 ═══', f'共 {len(entries)} 個來源', '']
    for i, e in enumerate(entries, 1):
        lines.append(f'{i}. {e.name}')
        if e.host:
            lines.append(f'   主機：{e.host}' + (f'  埠：{e.port}' if e.port else ''))
        if e.connection_type:
            lines.append(f'   類型：{e.connection_type}')
        lines.append('')
    return '\n'.join(lines)

def parse_find_summary(text: str) -> str:
    return format_find_table(parse_find_output(text))
