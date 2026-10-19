from __future__ import annotations
import math
import os
import queue
import threading
import time
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, messagebox, ttk
from typing import TYPE_CHECKING, Optional, Tuple
import numpy as np
try:
    from PIL import Image, ImageTk
except ImportError:
    Image = None
    ImageTk = None
from ndi_bandwidth import bandwidth_by_id, get_bandwidth_options, init_bandwidth_options
if TYPE_CHECKING:
    from app import NdiSession, NdiSourceRow
try:
    import NDIlib as ndi
except ImportError:
    ndi = None
PREVIEW_MAX_W = 960
PREVIEW_MAX_H = 540
UI_MAX_FPS = 20
AUDIO_METER_FPS = 8
AUDIO_IDLE_SEC = 2.0
RATE_WINDOW_SEC = 1.0
DB_FLOOR = -60.0
FramePacket = Tuple[int, int, int, int, bytes]
AudioLevelPacket = Tuple[float, float, int]

def samples_to_dbfs(samples: np.ndarray, no_channels: int) -> Tuple[float, float]:
    if samples is None or samples.size == 0:
        return (DB_FLOOR, DB_FLOOR)
    if no_channels > 1:
        mono = samples.reshape(-1, no_channels).mean(axis=1)
    else:
        mono = samples.reshape(-1).astype(np.float32, copy=False)
    peak = float(np.max(np.abs(mono))) if mono.size else 0.0
    rms = float(np.sqrt(np.mean(mono * mono))) if mono.size else 0.0
    db_peak = 20.0 * math.log10(max(peak, 1e-12))
    db_rms = 20.0 * math.log10(max(rms, 1e-12))
    return (max(db_rms, DB_FLOOR), max(db_peak, DB_FLOOR))

def video_frame_to_packet(video) -> Optional[FramePacket]:
    if Image is None:
        return None
    w, h = (int(video.xres), int(video.yres))
    if w <= 0 or h <= 0:
        return None
    stride = int(video.line_stride_in_bytes)
    need = stride * h
    buf = video.data
    if buf is None:
        return None
    try:
        raw = buf.tobytes() if hasattr(buf, 'tobytes') else bytes(memoryview(buf)[:need])
    except (TypeError, ValueError):
        raw = np.asarray(buf, dtype=np.uint8).tobytes()[:need]
    if len(raw) < need:
        return None
    try:
        img = Image.frombuffer('RGBA', (w, h), raw[:need], 'raw', 'BGRA', stride, 1)
    except Exception:
        return None
    scale = min(PREVIEW_MAX_W / w, PREVIEW_MAX_H / h, 1.0)
    dw, dh = (int(w * scale), int(h * scale))
    if scale < 1.0 and dw > 0 and (dh > 0):
        img = img.resize((dw, dh), Image.Resampling.BILINEAR)
    else:
        dw, dh = (w, h)
    rgb = img.convert('RGB')
    return (w, h, dw, dh, rgb.tobytes())

class NdiPreviewWindow:

    def __init__(self, parent: tk.Tk, session: 'NdiSession', row: 'NdiSourceRow') -> None:
        init_bandwidth_options()
        if ndi is None:
            raise RuntimeError('未安裝 ndi-python')
        if Image is None or ImageTk is None:
            raise RuntimeError('預覽需要 Pillow')
        self._bw_options = get_bandwidth_options()
        if not self._bw_options:
            raise RuntimeError('NDI 清晰度選項未初始化')
        self._session = session
        self._row = row
        self._stop = threading.Event()
        self._reconnect = threading.Event()
        self._photo: Optional[ImageTk.PhotoImage] = None
        self._frame_queue: queue.Queue = queue.Queue(maxsize=2)
        self._audio_queue: queue.Queue = queue.Queue(maxsize=1)
        self._rate_queue: queue.Queue = queue.Queue(maxsize=1)
        self._recv_thread: Optional[threading.Thread] = None
        self._recv_ref = {'recv': None}
        self._recording = tk.BooleanVar(value=False)
        self._record_file = ''
        self._show_meter = tk.BooleanVar(value=True)
        self._show_osd = tk.BooleanVar(value=True)
        self._last_video_push = 0.0
        self._last_audio_push = 0.0
        self._current_bw_id = 'highest'
        self._audio_channels = 0
        self._last_audio_mono = time.monotonic() - AUDIO_IDLE_SEC * 2
        self._meter_rms_db = DB_FLOOR
        self.win = tk.Toplevel(parent)
        self.win.title(f'NDI 預覽 — {row.ndi_name}')
        self.win.geometry('1024x640')
        self.win.protocol('WM_DELETE_WINDOW', self.close)
        top = ttk.Frame(self.win, padding=6)
        top.pack(fill=tk.X)
        ttk.Label(top, text=row.ndi_name, font=('', 10, 'bold')).pack(side=tk.LEFT)
        self.btn_record = ttk.Button(top, text='● 錄製 MOV', command=self._toggle_record)
        self.btn_record.pack(side=tk.RIGHT, padx=4)
        ttk.Button(top, text='關閉', command=self.close).pack(side=tk.RIGHT)
        body = ttk.Panedwindow(self.win, orient=tk.HORIZONTAL)
        body.pack(fill=tk.BOTH, expand=True, padx=6, pady=4)
        left = ttk.LabelFrame(body, text='接收清晰度', padding=4)
        body.add(left, weight=1)
        self._bw_list = tk.Listbox(left, height=10, exportselection=False, font=('Consolas', 9))
        self._bw_list.pack(fill=tk.BOTH, expand=True)
        default_idx = 1
        for _oid, _bw, desc, _has_video, short in self._bw_options:
            self._bw_list.insert(tk.END, f'{short:5}  {desc}')
        if self._bw_options:
            default_idx = min(default_idx, len(self._bw_options) - 1)
            self._bw_list.selection_set(default_idx)
            self._current_bw_id = self._bw_options[default_idx][0]
        self._bw_list.bind('<<ListboxSelect>>', self._on_bandwidth_changed)
        right = ttk.Frame(body)
        body.add(right, weight=5)
        opt_bar = ttk.Frame(right)
        opt_bar.pack(fill=tk.X, pady=(0, 4))
        ttk.Checkbutton(opt_bar, text='畫面 OSD', variable=self._show_osd, command=self._toggle_osd).pack(side=tk.LEFT)
        ttk.Checkbutton(opt_bar, text='音訊 dB 表', variable=self._show_meter, command=self._toggle_meter).pack(side=tk.LEFT, padx=(12, 0))
        self.status = tk.StringVar(value='連線中…')
        self.resolution = tk.StringVar(value='分辨率：—')
        self.audio_status = tk.StringVar(value='音訊：—')
        self.audio_db = tk.StringVar(value='dBFS：—')
        self.bitrate = tk.StringVar(value='速率：—')
        self._video_stack = tk.Frame(right, bg='#0d0d0d')
        self._video_stack.pack(fill=tk.BOTH, expand=True)
        self.canvas = tk.Label(self._video_stack, bg='#0d0d0d', text='等待視訊…', fg='#666')
        self.canvas.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._osd_panel = tk.Frame(self._video_stack, bg='#121212', highlightbackground='#3d5a80', highlightthickness=1)
        osd_font = ('Microsoft YaHei UI', 10) if os.name == 'nt' else ('Segoe UI', 10)
        osd_mono = ('Consolas', 10)
        for var, fg, mono in ((self.status, '#f5f5f5', False), (self.resolution, '#90caf9', True), (self.audio_status, '#80cbc4', False), (self.audio_db, '#ce93d8', True), (self.bitrate, '#ffcc80', True)):
            tk.Label(self._osd_panel, textvariable=var, bg='#121212', fg=fg, font=osd_mono if mono else osd_font, anchor=tk.W, padx=10, pady=2).pack(fill=tk.X)
        self._osd_panel.place(x=10, y=10, anchor=tk.NW)
        self.meter_frame = ttk.Frame(right, height=56)
        self.meter_frame.pack(fill=tk.X, pady=(4, 0))
        self.meter_frame.pack_propagate(False)
        self.meter_canvas = tk.Canvas(self.meter_frame, height=52, bg='#0a0a10', highlightthickness=0)
        self.meter_canvas.pack(fill=tk.BOTH, expand=True)
        self._start_recv_thread()
        self._poll_ui()

    def _selected_bandwidth_id(self) -> str:
        opts = self._bw_options
        if not opts:
            return 'highest'
        sel = self._bw_list.curselection()
        idx = sel[0] if sel else 1
        idx = min(max(idx, 0), len(opts) - 1)
        return opts[idx][0]

    def _on_bandwidth_changed(self, _event=None) -> None:
        if self._recording.get():
            messagebox.showinfo('提示', '請先停止錄製再切換清晰度。', parent=self.win)
            return
        self._current_bw_id = self._selected_bandwidth_id()
        self._reconnect.set()

    def _toggle_osd(self) -> None:
        if self._show_osd.get():
            self._osd_panel.place(x=10, y=10, anchor=tk.NW)
        else:
            self._osd_panel.place_forget()

    def _toggle_meter(self) -> None:
        if self._show_meter.get():
            self.meter_frame.pack(fill=tk.X, pady=(4, 0))
        else:
            self.meter_frame.pack_forget()

    def _toggle_record(self) -> None:
        recv = self._recv_ref.get('recv')
        if recv is None:
            messagebox.showinfo('提示', '尚未連線，請稍候。', parent=self.win)
            return
        if not self._recording.get():
            if not ndi.recv_recording_is_supported(recv):
                messagebox.showerror('錄製', '此來源/模式不支援 SDK 錄製。', parent=self.win)
                return
            default = os.path.join(os.path.expanduser('~'), 'Desktop', f'ndi_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mov')
            path = filedialog.asksaveasfilename(parent=self.win, defaultextension='.mov', filetypes=[('QuickTime MOV', '*.mov'), ('所有檔案', '*.*')], initialfile=os.path.basename(default), initialdir=os.path.dirname(default))
            if not path:
                return
            if not path.lower().endswith('.mov'):
                path += '.mov'
            ok = ndi.recv_recording_start(recv, path)
            if not ok:
                err = ndi.recv_recording_get_error(recv) or '未知錯誤'
                messagebox.showerror('錄製失敗', err, parent=self.win)
                return
            self._record_file = ndi.recv_recording_get_filename(recv) or path
            self._recording.set(True)
            self.btn_record.configure(text='■ 停止錄製')
            self.status.set(f'錄製中 → {self._record_file}')
        else:
            ndi.recv_recording_stop(recv)
            self._recording.set(False)
            self.btn_record.configure(text='● 錄製 MOV')
            fn = ndi.recv_recording_get_filename(recv) or self._record_file
            self.status.set(f'錄製已保存：{fn}')

    def _start_recv_thread(self) -> None:
        self._recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._recv_thread.start()

    def close(self) -> None:
        self._stop.set()
        self._reconnect.set()
        recv = self._recv_ref.get('recv')
        if recv is not None and self._recording.get():
            try:
                ndi.recv_recording_stop(recv)
            except Exception:
                pass
        try:
            self.win.destroy()
        except tk.TclError:
            pass

    def _recv_loop(self) -> None:
        recv = None
        current_bw_id = ''
        byte_accum = 0
        rate_window_start = time.monotonic()
        try:
            self._session.ensure_initialized()
            while not self._stop.is_set():
                bid = self._current_bw_id
                if recv is None or bid != current_bw_id or self._reconnect.is_set():
                    if recv is not None:
                        if self._recording.get():
                            try:
                                ndi.recv_recording_stop(recv)
                            except Exception:
                                pass
                            self._recording.set(False)
                        try:
                            ndi.recv_destroy(recv)
                        except Exception:
                            pass
                        recv = None
                        self._recv_ref['recv'] = None
                    self._reconnect.clear()
                    current_bw_id = bid
                    byte_accum = 0
                    rate_window_start = time.monotonic()
                    bw = bandwidth_by_id(bid)
                    create = ndi.RecvCreateV3()
                    create.bandwidth = bw
                    create.color_format = ndi.RECV_COLOR_FORMAT_BGRX_BGRA
                    recv = ndi.recv_create_v3(create)
                    if recv is None:
                        self._set_status('無法建立接收端')
                        return
                    ndi.recv_connect(recv, self._session.make_source(self._row))
                    self._recv_ref['recv'] = recv
                    short = bid
                    for oid, _b, _desc, _v, short_l in self._bw_options:
                        if oid == bid:
                            short = short_l
                            break
                    self._set_status(f'已連線 · {short} · 等待視訊…')
                _t, video, audio, metadata = ndi.recv_capture_v2(recv, 200)
                now = time.monotonic()
                if audio is not None:
                    try:
                        samples = np.asarray(audio.data, dtype=np.float32)
                        ch = int(audio.no_channels)
                        byte_accum += int(samples.nbytes)
                        self._last_audio_mono = now
                        if now - self._last_audio_push >= 1.0 / AUDIO_METER_FPS:
                            self._last_audio_push = now
                            db_rms, db_peak = samples_to_dbfs(samples, ch)
                            pkt: AudioLevelPacket = (db_rms, db_peak, ch)
                            try:
                                while not self._audio_queue.empty():
                                    self._audio_queue.get_nowait()
                                self._audio_queue.put_nowait(pkt)
                            except queue.Full:
                                pass
                    except Exception:
                        pass
                    ndi.recv_free_audio_v2(recv, audio)
                if metadata is not None:
                    ndi.recv_free_metadata(recv, metadata)
                if video is not None:
                    stride = int(video.line_stride_in_bytes)
                    h = int(video.yres)
                    byte_accum += max(stride * h, 0)
                    packet = video_frame_to_packet(video)
                    ndi.recv_free_video_v2(recv, video)
                    if packet is not None:
                        if now - self._last_video_push >= 1.0 / UI_MAX_FPS:
                            self._last_video_push = now
                            try:
                                while not self._frame_queue.empty():
                                    self._frame_queue.get_nowait()
                                self._frame_queue.put_nowait(packet)
                            except queue.Full:
                                pass
                if now - rate_window_start >= RATE_WINDOW_SEC:
                    elapsed = max(now - rate_window_start, 0.001)
                    mbps = byte_accum * 8.0 / elapsed / 1000000.0
                    dropped = None
                    try:
                        perf = ndi.recv_get_performance(recv)
                        if isinstance(perf, tuple) and len(perf) >= 2:
                            dropped = int(getattr(perf[1], 'video_frames', 0) or 0)
                    except Exception:
                        pass
                    try:
                        while not self._rate_queue.empty():
                            self._rate_queue.get_nowait()
                        self._rate_queue.put_nowait((mbps, dropped))
                    except queue.Full:
                        pass
                    byte_accum = 0
                    rate_window_start = now
        except Exception as exc:
            self._set_status(f'錯誤：{exc}')
        finally:
            if recv is not None:
                try:
                    ndi.recv_destroy(recv)
                except Exception:
                    pass
            self._recv_ref['recv'] = None

    def _poll_ui(self) -> None:
        if self._stop.is_set():
            return
        try:
            sw, sh, dw, dh, data = self._frame_queue.get_nowait()
            photo = ImageTk.PhotoImage(Image.frombytes('RGB', (dw, dh), data))
            self._photo = photo
            self.canvas.configure(image=photo, text='')
            self._osd_panel.lift()
            self.resolution.set(f'分辨率：{sw}×{sh}  （預覽 {dw}×{dh}）')
            short = self._current_bw_id
            for oid, _b, _desc, _v, short_l in self._bw_options:
                if oid == short:
                    short = short_l
                    break
            if not self._recording.get():
                self.status.set(f'播放中 · {short}')
        except queue.Empty:
            pass
        try:
            mbps, dropped = self._rate_queue.get_nowait()
            extra = ''
            if dropped is not None and dropped > 0:
                extra = f' · 丟棄視訊 {dropped}'
            self.bitrate.set(f'速率：{mbps:.2f} Mbps{extra}')
        except queue.Empty:
            pass
        now = time.monotonic()
        if now - self._last_audio_mono <= AUDIO_IDLE_SEC:
            try:
                db_rms, db_peak, ch = self._audio_queue.get_nowait()
                self._audio_channels = ch
                self._meter_rms_db = db_rms
                self.audio_status.set(f'音訊：有（{ch}ch）')
                self.audio_db.set(f'dBFS  RMS {db_rms:+.1f}  Peak {db_peak:+.1f}')
            except queue.Empty:
                if self._audio_channels:
                    self.audio_status.set(f'音訊：有（{self._audio_channels}ch）')
        else:
            self.audio_status.set('音訊：無')
            self.audio_db.set('dBFS：—')
            self._meter_rms_db = DB_FLOOR
        if self._show_meter.get():
            self._draw_db_meter(self._meter_rms_db)
        try:
            self.win.after(40, self._poll_ui)
        except tk.TclError:
            pass

    def _draw_db_meter(self, rms_db: float) -> None:
        c = self.meter_canvas
        c.delete('m')
        w = max(c.winfo_width(), 120)
        h = max(c.winfo_height(), 40)
        margin = 8
        bar_y = h * 0.55
        bar_h = 14
        x0, x1 = (margin, w - margin)
        c.create_rectangle(x0, bar_y, x1, bar_y + bar_h, fill='#1a1a22', outline='#333', tags='m')
        t = (rms_db - DB_FLOOR) / (0.0 - DB_FLOOR)
        t = max(0.0, min(1.0, t))
        fill_x = x0 + (x1 - x0) * t
        if fill_x > x0:
            color = '#43a047' if rms_db < -12 else '#fb8c00' if rms_db < -3 else '#e53935'
            c.create_rectangle(x0, bar_y, fill_x, bar_y + bar_h, fill=color, outline='', tags='m')
        c.create_text(margin, bar_y - 6, anchor=tk.SW, text=f'{rms_db:+.1f} dBFS', fill='#b0bec5', font=('Consolas', 10), tags='m')
        for db in (-60, -40, -20, -6):
            frac = (db - DB_FLOOR) / (0.0 - DB_FLOOR)
            tx = x0 + (x1 - x0) * frac
            c.create_line(tx, bar_y + bar_h + 2, tx, bar_y + bar_h + 6, fill='#555', tags='m')
            c.create_text(tx, bar_y + bar_h + 8, text=str(db), fill='#666', font=('', 7), tags='m')

    def _set_status(self, text: str) -> None:
        try:
            self.win.after(0, lambda: self.status.set(text))
        except tk.TclError:
            pass
