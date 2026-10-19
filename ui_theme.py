from __future__ import annotations
import tkinter as tk
from tkinter import ttk
COLOR_BG = '#e8f4fc'
COLOR_SURFACE = '#ffffff'
COLOR_SURFACE_ALT = '#f7fbff'
COLOR_ACCENT = '#1565c0'
COLOR_ACCENT_LIGHT = '#42a5f5'
COLOR_TEXT = '#1a2b3c'
COLOR_TEXT_MUTED = '#5c6b7a'
COLOR_BORDER = '#b6d4ea'

def _base_font(root: tk.Tk) -> tuple[str, int]:
    ws = root.tk.call('tk', 'windowingsystem')
    if ws == 'win32':
        return ('Microsoft YaHei UI', 10)
    if ws == 'aqua':
        return ('PingFang SC', 11)
    return ('Segoe UI', 10)

def apply_theme(root: tk.Tk) -> ttk.Style:
    root.configure(bg=COLOR_BG)
    style = ttk.Style(root)
    try:
        style.theme_use('clam')
    except tk.TclError:
        pass
    font = _base_font(root)
    font_bold = (font[0], font[1], 'bold')
    style.configure('.', background=COLOR_BG, foreground=COLOR_TEXT, font=font)
    style.configure('TFrame', background=COLOR_BG)
    style.configure('Surface.TFrame', background=COLOR_SURFACE)
    style.configure('TLabelframe', background=COLOR_SURFACE, bordercolor=COLOR_BORDER, relief='solid', borderwidth=1)
    style.configure('TLabelframe.Label', background=COLOR_SURFACE, foreground=COLOR_ACCENT, font=font_bold)
    style.configure('TButton', background=COLOR_ACCENT_LIGHT, foreground='#ffffff', borderwidth=0, padding=(12, 6), font=font)
    style.map('TButton', background=[('active', '#1e88e5'), ('pressed', '#1565c0'), ('disabled', '#b0bec5')], foreground=[('disabled', '#eceff1')])
    style.configure('Secondary.TButton', background=COLOR_SURFACE_ALT, foreground=COLOR_ACCENT, borderwidth=1)
    style.map('Secondary.TButton', background=[('active', '#e3f2fd'), ('disabled', '#dde4ea')], foreground=[('disabled', COLOR_TEXT_MUTED)], bordercolor=[('disabled', COLOR_BORDER)])
    style.configure('TCheckbutton', background=COLOR_BG, foreground=COLOR_TEXT, font=font)
    style.configure('TRadiobutton', background=COLOR_BG, foreground=COLOR_TEXT, font=font)
    style.configure('TLabel', background=COLOR_BG, foreground=COLOR_TEXT, font=font)
    style.configure('Muted.TLabel', background=COLOR_BG, foreground=COLOR_TEXT_MUTED, font=(font[0], 9))
    style.configure('Status.TLabel', background=COLOR_SURFACE, foreground=COLOR_ACCENT, font=(font[0], 9), padding=(10, 6))
    style.configure('Treeview', background=COLOR_SURFACE, fieldbackground=COLOR_SURFACE, foreground=COLOR_TEXT, rowheight=28, bordercolor=COLOR_BORDER, font=font)
    style.configure('Treeview.Heading', background='#d6eaf8', foreground=COLOR_ACCENT, font=font_bold, relief='flat')
    style.map('Treeview', background=[('selected', '#bbdefb')], foreground=[('selected', COLOR_TEXT)])
    style.configure('TCombobox', fieldbackground=COLOR_SURFACE, font=font)
    style.configure('TEntry', fieldbackground=COLOR_SURFACE, font=font)
    style.configure('TNotebook', background=COLOR_BG)
    style.configure('TNotebook.Tab', background='#d6eaf8', padding=(14, 7), font=font)
    style.map('TNotebook.Tab', background=[('selected', COLOR_ACCENT_LIGHT)], foreground=[('selected', '#ffffff'), ('!selected', COLOR_TEXT)])
    return style

def style_text_widget(widget: tk.Text) -> None:
    widget.configure(bg=COLOR_SURFACE, fg=COLOR_TEXT, insertbackground=COLOR_ACCENT, relief='flat', borderwidth=1, highlightthickness=1, highlightbackground=COLOR_BORDER, highlightcolor=COLOR_ACCENT_LIGHT)
