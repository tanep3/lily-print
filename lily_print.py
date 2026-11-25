# -*- coding: utf-8 -*-
"""
Lily Print — Thermal Mini Printer -Lily- (LX-D02) 用 Windows GUI 印刷アプリ

ファイルを選ぶ → 正方形プレビュー（収まるよう縮小表示）→ 印刷ボタン。
プレビュー上で切り抜き（クロップ）もできる。
印刷エンジンは OSS の lxd02-printer ライブラリを使う。

使い方:
    pip install lxd02-printer tkinterdnd2
    python lily_print.py
    (tkinterdnd2 が無い場合はドラッグ＆ドロップだけ無効になる)
"""
import asyncio
import json
import math
import queue
import sys
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    HAS_DND = True
except ImportError:  # ドラッグ＆ドロップは任意機能
    HAS_DND = False

from PIL import Image, ImageGrab, ImageOps, ImageTk

from lxd02 import LXD02, PrinterError, find_printer
from lxd02.protocol import WIDTH, rasterize  # noqa: F401  (rasterize は検証用に参照)

if getattr(sys, "frozen", False):  # PyInstaller の exe では exe の隣に設定を置く
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
CONFIG_PATH = APP_DIR / "lily_print_config.json"
APP_VERSION = "0.3.1"
CREDIT = "© Tane Channel Technology"

# ---------------------------------------------------------------- theme
BG = "#FFF7FA"        # ほんのりピンクの白
BAND = "#FFD9E8"      # ヘッダの帯
PANEL = "#FFFFFF"
ACCENT = "#FF7AA2"    # メインのピンク
ACCENT_D = "#F75F8F"
PURPLE = "#A78BFA"    # ラベンダー
TEXT = "#4A3F55"      # 柔らかい紫グレー
SUBTLE = "#9A86AD"
LINE = "#FFC2D6"
DISABLED = "#EFE6EC"

FONT = "Yu Gothic UI"
F_TITLE = (FONT, 26, "bold")
F_BASE = (FONT, 15)
F_BIGBTN = (FONT, 19, "bold")
F_BTN = (FONT, 15, "bold")
F_STATUS = (FONT, 14)
F_SMALL = (FONT, 11)
F_MONO = ("Consolas", 14)


# ---------------------------------------------------------------- rendering
def to_gray_384(img: Image.Image) -> Image.Image:
    """PIL 画像を横 384px のグレースケール (L) に整形する。"""
    gray = img.convert("L")
    if gray.width != WIDTH:
        h = max(1, round(gray.height * WIDTH / gray.width))
        gray = gray.resize((WIDTH, h), Image.LANCZOS)
    return gray


def load_gray_384(path: str) -> Image.Image:
    """画像ファイルを開き、横 384px のグレースケール (L) に整形する。"""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)  # 写真の回転情報を反映
    return to_gray_384(img)


def to_bw(gray: Image.Image, dither: bool) -> Image.Image:
    """lxd02.protocol.rasterize と同じ変換で 1bit 化する (mode '1')。"""
    inv = ImageOps.invert(gray)
    if dither:
        return inv.convert("1")  # Floyd-Steinberg
    return inv.point(lambda p: 255 if p >= 128 else 0, "1")


def preview_image(gray: Image.Image, dither: bool) -> Image.Image:
    """印刷される絵そのもののプレビュー (L, 白地に黒) を返す。"""
    bw = to_bw(gray, dither)
    return ImageOps.invert(bw.convert("L"))


def crop_box(rect, w: int, h: int):
    """割合 (fx0, fy0, fx1, fy1) の切り抜き矩形をピクセル座標に変換する。"""
    fx0, fy0, fx1, fy1 = rect
    x0 = min(w - 1, max(0, round(fx0 * w)))
    x1 = min(w, max(x0 + 1, round(fx1 * w)))
    y0 = min(h - 1, max(0, round(fy0 * h)))
    y1 = min(h, max(y0 + 1, round(fy1 * h)))
    return (x0, y0, x1, y1)


def is_full_rect(rect) -> bool:
    fx0, fy0, fx1, fy1 = rect
    return fx0 <= 0.001 and fy0 <= 0.001 and fx1 >= 0.999 and fy1 >= 0.999


# ---------------------------------------------------------------- printer I/O
# 各ジョブは (表示メッセージ, 使ったアドレス) を返す。アドレスが空なら
# find_printer() で自動探索し、見つけたアドレスを呼び出し側が保存する。
async def _resolve(address: str | None) -> str:
    return address if address else await find_printer()


async def _find_job() -> tuple[str, str]:
    address = await find_printer()
    return f"見つけたよ：{address}（保存しました）", address


async def _print_job(address: str | None, gray: Image.Image, energy: int,
                     copies: int, dither: bool) -> tuple[str, str]:
    address = await _resolve(address)
    async with LXD02(address) as printer:
        status = await printer.wait_status()
        if status.no_paper:
            raise PrinterError("紙がありません (no paper)")
        for _ in range(copies):
            await printer.print_image(gray, energy=energy, dither=dither)
    return f"印刷しました（{copies} 部）♡", address


async def _status_job(address: str | None) -> tuple[str, str]:
    address = await _resolve(address)
    async with LXD02(address) as printer:
        s = await printer.wait_status()
    paper = "紙なし" if s.no_paper else "紙 OK"
    charge = "・充電中" if s.charging else ("・充電完了" if s.charged else "")
    hot = "・過熱" if s.overheated else ""
    return f"電池 {s.battery}% / {paper} / 濃さ {s.energy}{charge}{hot}", address


def explain_error(exc: Exception) -> str:
    msg = str(exc)
    if "not advertising" in msg or "No LX-D02 found" in msg:
        return ("プリンタが見つかりません。電源が入っているか、"
                "スマホの Bluetooth が OFF か、USB 給電中かを確認してください。\n"
                f"({msg})")
    if "no status" in msg:
        return f"プリンタから状態が返りませんでした。電源を入れ直してみてください。\n({msg})"
    return f"エラー: {msg}"


# ---------------------------------------------------------------- config
def load_config() -> dict:
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_config(cfg: dict) -> None:
    try:
        CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                               encoding="utf-8")
    except Exception:
        pass


# ---------------------------------------------------------------- widgets
def _shade(hex_color: str, factor: float) -> str:
    r = min(255, int(int(hex_color[1:3], 16) * factor))
    g = min(255, int(int(hex_color[3:5], 16) * factor))
    b = min(255, int(int(hex_color[5:7], 16) * factor))
    return f"#{r:02x}{g:02x}{b:02x}"


def _rounded_points(x0, y0, x1, y1, r, steps=5):
    """角丸矩形の輪郭点列（Canvas ポリゴン用）。"""
    pts = []
    corners = ((x1 - r, y0 + r, -90), (x1 - r, y1 - r, 0),
               (x0 + r, y1 - r, 90), (x0 + r, y0 + r, 180))
    for cx, cy, start in corners:
        for i in range(steps + 1):
            a = math.radians(start + 90 * i / steps)
            pts.extend((cx + r * math.cos(a), cy + r * math.sin(a)))
    return pts


class RoundedButton(tk.Canvas):
    """Canvas で描く角丸ボタン。tk.Button の代わりに使う。

    config(state="disabled" / "normal") と config(text=...) に対応する。
    """

    def __init__(self, parent, text, command, bg, fg="#FFFFFF", font=F_BTN,
                 parent_bg=BG, padx=18, pady=8, border=None):
        self._command = command
        self._bg = bg
        self._fg = fg
        self._border = border
        self._state = "normal"
        measure = tk.Label(parent, text=text, font=font)
        w = measure.winfo_reqwidth() + padx * 2
        h = measure.winfo_reqheight() + pady * 2
        measure.destroy()
        super().__init__(parent, width=w, height=h, bg=parent_bg,
                         highlightthickness=0, bd=0, cursor="hand2")
        self._bw, self._bh = w, h
        radius = min(14, h // 2 - 1)
        self._poly = self.create_polygon(
            _rounded_points(1, 1, w - 1, h - 1, radius),
            fill=bg, outline=border or bg, width=1)
        self._label = self.create_text(w // 2, h // 2, text=text,
                                       fill=fg, font=font)
        self.bind("<Enter>", lambda e: self._redraw("hover"))
        self.bind("<Leave>", lambda e: self._redraw("normal"))
        self.bind("<ButtonPress-1>", lambda e: self._redraw("pressed"))
        self.bind("<ButtonRelease-1>", self._on_release)

    def _redraw(self, mode="normal"):
        if self._state == "disabled":
            fill, fg = DISABLED, SUBTLE
        elif mode == "hover":
            fill, fg = _shade(self._bg, 0.93), self._fg
        elif mode == "pressed":
            fill, fg = _shade(self._bg, 0.85), self._fg
        else:
            fill, fg = self._bg, self._fg
        self.itemconfig(self._poly, fill=fill,
                        outline=self._border or fill)
        self.itemconfig(self._label, fill=fg)

    def _on_release(self, event):
        self._redraw("hover")
        if (self._state != "disabled"
                and 0 <= event.x <= self._bw and 0 <= event.y <= self._bh):
            self._command()

    def set_selected(self, selected: bool):
        """チップ型トグルとしての見た目に切り替える。"""
        if selected:
            self._bg, self._fg, self._border = ACCENT, "#FFFFFF", ACCENT
        else:
            self._bg, self._fg, self._border = PANEL, TEXT, LINE
        self._redraw("normal")

    def configure(self, cnf=None, **kw):
        if cnf:
            kw.update(cnf)
        state = kw.pop("state", None)
        if state is not None:
            self._state = state
            self._redraw("normal")
        if "text" in kw:
            self.itemconfig(self._label, text=kw.pop("text"))
        if kw:
            return super().configure(**kw)
        return None

    config = configure


# ---------------------------------------------------------------- GUI
class LilyPrintApp(TkinterDnD.Tk if HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lily Print ♡ LX-D02")
        self.configure(bg=BG)
        self.resizable(False, False)

        cfg = load_config()
        self.gray: Image.Image | None = None        # 印刷対象（切り抜き適用後）
        self.original_gray: Image.Image | None = None  # 読み込んだままの画像
        self.crop_rect = None                       # (fx0, fy0, fx1, fy1) 割合
        self.cropping = False
        self._crop_map = None                       # (ox, oy, dw, dh) 表示写像
        self._drag = None                           # クロップ中のドラッグ状態
        self.results: queue.Queue = queue.Queue()
        self.busy = False
        self._job_loop = None   # 実行中ジョブの asyncio ループ（キャンセル用）
        self._job_task = None
        self._cancelling = False

        self.address_var = tk.StringVar(value=cfg.get("address", ""))
        self.dither_var = tk.BooleanVar(value=bool(cfg.get("dither", True)))
        try:
            energy0 = int(cfg.get("energy", 3))
        except (TypeError, ValueError):
            energy0 = 3
        self.energy_var = tk.IntVar(value=min(6, max(1, energy0)))
        self.copies_var = tk.IntVar(value=1)  # 部数は記憶しない（毎回 1）
        self.file_var = tk.StringVar(value="まだ選ばれていません")
        self.status_var = tk.StringVar(
            value="画像ファイルを選んでね ♪" if self.address_var.get()
            else "画像ファイルを選んでね ♪（プリンタは「探す」か印刷時に自動で探すよ）")

        # ヘッダ帯
        band = tk.Frame(self, bg=BAND)
        band.pack(fill="x")
        tk.Label(band, text="✿ Lily Print ✿", font=F_TITLE,
                 bg=BAND, fg=TEXT).pack(pady=(10, 0))
        tk.Label(band, text="サーマルミニプリンター Lily（LX-D02）であそぼ",
                 font=F_STATUS, bg=BAND, fg=SUBTLE).pack(pady=(0, 10))

        # ファイル選択
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=16, pady=(14, 4))
        RoundedButton(top, "ファイルを開く", self.open_file, PURPLE).pack(side="left")
        RoundedButton(top, "貼り付け", self.paste_clipboard, PURPLE).pack(
            side="left", padx=(10, 0))
        tk.Label(top, textvariable=self.file_var, font=F_BASE,
                 bg=BG, fg=TEXT).pack(side="left", padx=12)

        # 切り抜きツールバー
        cropbar = tk.Frame(self, bg=BG)
        cropbar.pack(fill="x", padx=16, pady=(6, 0))
        self.crop_btn = RoundedButton(cropbar, "切り抜き", self.toggle_crop,
                                      PURPLE)
        self.crop_btn.pack(side="left")
        self.crop_cancel_btn = RoundedButton(cropbar, "やめる",
                                             self.cancel_crop, PANEL,
                                             fg=TEXT, border=LINE)
        self.crop_clear_btn = RoundedButton(cropbar, "切り抜き解除",
                                            self.clear_crop, PANEL,
                                            fg=TEXT, border=LINE)
        self.crop_note = tk.Label(cropbar, text="", font=F_STATUS,
                                  bg=BG, fg=SUBTLE)
        self.crop_note.pack(side="left", padx=12)

        # プレビュー（正方形エリアに収まるよう縮小表示、スクロールなし）
        panel = tk.Frame(self, bg=PANEL, highlightbackground=LINE,
                         highlightthickness=2)
        panel.pack(padx=16, pady=10)
        self.pv_size = max(300, min(460, self.winfo_screenheight() - 470))
        self.preview_canvas = tk.Canvas(
            panel, width=self.pv_size, height=self.pv_size, bg=PANEL,
            highlightthickness=0)
        self.preview_canvas.pack(padx=6, pady=6)
        self.preview_canvas.create_text(
            self.pv_size // 2, self.pv_size // 2, anchor="center",
            text="ここにプレビューが出ます\n（印刷ドットを正方形エリアに\n収まるよう縮小して表示）\n\n"
                 "画像ファイルをドラッグ＆ドロップでもOK\n"
                 "コピーした画像は「貼り付け」か Ctrl+V でもOK",
            font=F_BASE, fill=SUBTLE, justify="center", tags="placeholder")
        self.preview_canvas.bind("<ButtonPress-1>", self._crop_press)
        self.preview_canvas.bind("<B1-Motion>", self._crop_motion)
        self.preview_canvas.bind("<ButtonRelease-1>", self._crop_release)
        self._photo = None

        # オプション（モードはチップ型トグル）
        opts = tk.Frame(self, bg=BG)
        opts.pack(fill="x", padx=16, pady=4)
        self.dither_chip = RoundedButton(opts, "ディザ（写真むき）",
                                         lambda: self._set_dither(True),
                                         PANEL, fg=TEXT, border=LINE)
        self.dither_chip.pack(side="left", padx=(0, 8))
        self.binary_chip = RoundedButton(opts, "二値（QR・線画むき）",
                                         lambda: self._set_dither(False),
                                         PANEL, fg=TEXT, border=LINE)
        self.binary_chip.pack(side="left", padx=(0, 14))
        tk.Label(opts, text="濃さ", font=F_BASE, bg=BG, fg=TEXT).pack(side="left")
        tk.Spinbox(opts, from_=1, to=6, width=3, textvariable=self.energy_var,
                   font=F_BASE, command=self._persist,
                   bg=PANEL, buttonbackground=BAND, relief="flat",
                   highlightthickness=1, highlightbackground=LINE,
                   highlightcolor=ACCENT).pack(side="left", padx=(2, 12))
        tk.Label(opts, text="部数", font=F_BASE, bg=BG, fg=TEXT).pack(side="left")
        tk.Spinbox(opts, from_=1, to=9, width=3, textvariable=self.copies_var,
                   font=F_BASE, bg=PANEL, buttonbackground=BAND,
                   relief="flat", highlightthickness=1,
                   highlightbackground=LINE,
                   highlightcolor=ACCENT).pack(side="left", padx=2)
        self._refresh_mode_chips()

        # MAC アドレス（空なら自動探索して保存する）
        addr = tk.Frame(self, bg=BG)
        addr.pack(fill="x", padx=16, pady=4)
        tk.Label(addr, text="プリンタ MAC", font=F_BASE,
                 bg=BG, fg=TEXT).pack(side="left")
        self.address_entry = tk.Entry(
            addr, textvariable=self.address_var, width=19, font=F_MONO,
            bg=PANEL, fg=TEXT, relief="flat", highlightthickness=1,
            highlightbackground=LINE, highlightcolor=ACCENT)
        self.address_entry.pack(side="left", padx=6)
        self.find_btn = RoundedButton(addr, "探す", self.find_printer_ui, PURPLE)
        self.find_btn.pack(side="left")

        # ボタン行
        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", padx=16, pady=(10, 2))
        self.status_btn = RoundedButton(btns, "状態を確認", self.check_status,
                                        PANEL, fg=TEXT, border=LINE)
        self.status_btn.pack(side="left")
        self.print_btn = RoundedButton(btns, "♡ 印刷する ♡", self.do_print,
                                       ACCENT, font=F_BIGBTN, padx=30, pady=12)
        self.print_btn.pack(side="right")
        # 印刷中だけ印刷ボタンの左に出るキャンセルボタン
        self.cancel_btn = RoundedButton(btns, "キャンセル", self.cancel_print,
                                        PANEL, fg=TEXT, border=LINE,
                                        font=F_BIGBTN, padx=20, pady=12)

        # ステータス
        tk.Label(self, textvariable=self.status_var, font=F_STATUS,
                 bg=BG, fg=TEXT, wraplength=800, justify="left",
                 anchor="w").pack(fill="x", padx=16, pady=(6, 2))

        # フッタ（バージョンとクレジット）
        foot = tk.Frame(self, bg=BG)
        foot.pack(fill="x", padx=16, pady=(0, 10))
        tk.Label(foot, text=f"Lily Print v{APP_VERSION}", font=F_SMALL,
                 bg=BG, fg=SUBTLE).pack(side="left")
        tk.Label(foot, text=CREDIT, font=F_SMALL,
                 bg=BG, fg=SUBTLE).pack(side="right")

        # ドラッグ＆ドロップ（tkinterdnd2 がある場合のみ）
        if HAS_DND:
            for w in (self, self.preview_canvas):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

        # Ctrl+V でクリップボードの画像を貼る（MAC 欄にフォーカス中は除く）
        self.bind("<Control-v>", self._on_ctrl_v)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.after(100, self._poll_results)

    # ------------------------------------------------------------ settings
    def _persist(self):
        cfg = load_config()
        cfg.update({
            "address": self.address_var.get().strip(),
            "dither": bool(self.dither_var.get()),
            "energy": int(self.energy_var.get()),
        })
        save_config(cfg)

    def _on_close(self):
        self._persist()
        self.destroy()

    def _set_dither(self, value: bool):
        self.dither_var.set(value)
        self._refresh_mode_chips()
        self._persist()
        self.refresh_preview()

    def _refresh_mode_chips(self):
        self.dither_chip.set_selected(self.dither_var.get())
        self.binary_chip.set_selected(not self.dither_var.get())

    # ------------------------------------------------------------ actions
    def open_file(self):
        path = filedialog.askopenfilename(
            title="印刷する画像を選ぶ",
            filetypes=[("画像", "*.png *.jpg *.jpeg *.bmp *.gif *.webp"),
                       ("すべて", "*.*")])
        if path:
            self.load_path(path)

    def on_drop(self, event):
        paths = self.tk.splitlist(event.data)
        if not paths:
            return
        self.load_path(paths[0])
        if len(paths) > 1:
            self.status_var.set(
                f"{len(paths)} 個落とされたので、先頭の1個だけ読み込んだよ")

    def load_path(self, path: str):
        try:
            gray = load_gray_384(path)
        except Exception as exc:
            messagebox.showerror("読み込み失敗", str(exc))
            return
        self._set_gray(gray, Path(path).name)

    def _set_gray(self, gray: Image.Image, name: str):
        self.original_gray = gray
        self.gray = gray
        self.crop_rect = None
        self.cropping = False
        self._sync_cropbar()
        self.file_var.set(name)
        self.status_var.set(
            f"読み込んだよ：横 {gray.width} × 縦 {gray.height} ドット")
        self.refresh_preview()

    def _on_ctrl_v(self, event=None):
        if self.focus_get() is self.address_entry:
            return  # MAC 欄への文字貼り付けは通常動作に任せる
        self.paste_clipboard()

    def paste_clipboard(self):
        try:
            data = ImageGrab.grabclipboard()
        except Exception:
            data = None
        if isinstance(data, Image.Image):
            self._set_gray(to_gray_384(data), "クリップボードの画像")
        elif isinstance(data, list) and data:  # エクスプローラーでコピーしたファイル
            self.load_path(str(data[0]))
        else:
            self.status_var.set(
                "クリップボードに画像がありません。"
                "ブラウザで画像を右クリック→「画像をコピー」してから貼ってね。")

    # ------------------------------------------------------------ preview
    def refresh_preview(self):
        if self.cropping or self.gray is None:
            return
        pv = preview_image(self.gray, self.dither_var.get())
        scale = min(self.pv_size / pv.width, self.pv_size / pv.height)
        dw = max(1, round(pv.width * scale))
        dh = max(1, round(pv.height * scale))
        disp = pv.resize((dw, dh), Image.NEAREST)
        self._photo = ImageTk.PhotoImage(disp)
        cv = self.preview_canvas
        cv.delete("all")
        cv.create_image(self.pv_size // 2, self.pv_size // 2,
                        anchor="center", image=self._photo)

    # ------------------------------------------------------------ crop
    def _sync_cropbar(self):
        if self.cropping:
            self.crop_btn.config(text="適用する")
            self.crop_cancel_btn.pack(side="left", padx=(8, 0))
            self.crop_clear_btn.pack_forget()
            self.crop_note.config(
                text="ドラッグで範囲指定／枠内ドラッグで移動／角でリサイズ")
        else:
            self.crop_btn.config(text="切り抜き")
            self.crop_cancel_btn.pack_forget()
            if self.crop_rect is not None:
                self.crop_clear_btn.pack(side="left", padx=(8, 0))
                self.crop_note.config(text="切り抜き中（解除で全体に戻ります）")
            else:
                self.crop_clear_btn.pack_forget()
                self.crop_note.config(text="")

    def toggle_crop(self):
        if self.cropping:
            self.apply_crop()
            return
        if self.original_gray is None:
            self.status_var.set("先に画像ファイルを選んでね。")
            return
        self.cropping = True
        if self.crop_rect is None:
            self.crop_rect = (0.0, 0.0, 1.0, 1.0)
        self._sync_cropbar()
        self._show_crop_canvas()

    def cancel_crop(self):
        self.cropping = False
        self._sync_cropbar()
        self.refresh_preview()

    def clear_crop(self):
        if self.original_gray is None:
            return
        self.crop_rect = None
        self.gray = self.original_gray
        self._sync_cropbar()
        self.status_var.set("切り抜きを解除したよ（全体に戻しました）")
        self.refresh_preview()

    def apply_crop(self):
        self.cropping = False
        if self.original_gray is not None and self.crop_rect is not None:
            if is_full_rect(self.crop_rect):
                self.crop_rect = None
                self.gray = self.original_gray
            else:
                box = crop_box(self.crop_rect, self.original_gray.width,
                               self.original_gray.height)
                cropped = self.original_gray.crop(box)
                self.gray = to_gray_384(cropped)
                self.status_var.set(
                    f"切り抜いたよ：元画像の {box[2] - box[0]} × "
                    f"{box[3] - box[1]} ドット → 横 {self.gray.width} × "
                    f"縦 {self.gray.height} ドット")
        self._sync_cropbar()
        self.refresh_preview()

    def _show_crop_canvas(self):
        img = self.original_gray
        scale = min(self.pv_size / img.width, self.pv_size / img.height)
        dw = max(1, round(img.width * scale))
        dh = max(1, round(img.height * scale))
        ox, oy = (self.pv_size - dw) // 2, (self.pv_size - dh) // 2
        self._crop_map = (ox, oy, dw, dh)
        disp = img.resize((dw, dh), Image.LANCZOS)
        self._photo = ImageTk.PhotoImage(disp)
        cv = self.preview_canvas
        cv.delete("all")
        cv.create_image(self.pv_size // 2, self.pv_size // 2,
                        anchor="center", image=self._photo)
        self._draw_crop_overlay()

    def _draw_crop_overlay(self):
        cv = self.preview_canvas
        cv.delete("crop")
        if self._crop_map is None or self.crop_rect is None:
            return
        ox, oy, dw, dh = self._crop_map
        fx0, fy0, fx1, fy1 = self.crop_rect
        x0, y0 = ox + fx0 * dw, oy + fy0 * dh
        x1, y1 = ox + fx1 * dw, oy + fy1 * dh
        cv.create_rectangle(x0, y0, x1, y1, outline=ACCENT_D, width=2,
                            tags="crop")
        for cx, cy in ((x0, y0), (x1, y0), (x0, y1), (x1, y1)):
            cv.create_rectangle(cx - 6, cy - 6, cx + 6, cy + 6,
                                fill=ACCENT, outline="#FFFFFF", width=2,
                                tags="crop")

    def _to_fraction(self, px, py):
        ox, oy, dw, dh = self._crop_map
        fx = min(1.0, max(0.0, (px - ox) / dw))
        fy = min(1.0, max(0.0, (py - oy) / dh))
        return fx, fy

    def _crop_press(self, event):
        if not self.cropping or self._crop_map is None:
            return
        fx, fy = self._to_fraction(event.x, event.y)
        ox, oy, dw, dh = self._crop_map
        fx0, fy0, fx1, fy1 = self.crop_rect
        corners = {"nw": (fx0, fy0), "ne": (fx1, fy0),
                   "sw": (fx0, fy1), "se": (fx1, fy1)}
        for name, (cx, cy) in corners.items():
            if abs(event.x - (ox + cx * dw)) <= 9 and \
                    abs(event.y - (oy + cy * dh)) <= 9:
                self._drag = ("resize", name)
                return
        if not is_full_rect(self.crop_rect) and \
                fx0 <= fx <= fx1 and fy0 <= fy <= fy1:
            self._drag = ("move", fx - fx0, fy - fy0)
        else:
            # 全体選択のときも、ドラッグで新しい範囲を引き直せる
            self._drag = ("new", fx, fy)
            self.crop_rect = (fx, fy, fx, fy)

    def _crop_motion(self, event):
        if not self.cropping or self._drag is None:
            return
        fx, fy = self._to_fraction(event.x, event.y)
        fx0, fy0, fx1, fy1 = self.crop_rect
        kind = self._drag[0]
        if kind == "new":
            _, ax, ay = self._drag
            self.crop_rect = (min(ax, fx), min(ay, fy),
                              max(ax, fx), max(ay, fy))
        elif kind == "move":
            _, offx, offy = self._drag
            w, h = fx1 - fx0, fy1 - fy0
            nx0 = min(1.0 - w, max(0.0, fx - offx))
            ny0 = min(1.0 - h, max(0.0, fy - offy))
            self.crop_rect = (nx0, ny0, nx0 + w, ny0 + h)
        else:  # resize: 対角を固定して角を動かす（最小サイズ 0.04）
            name = self._drag[1]
            min_size = 0.04
            if "w" in name:
                fx0 = min(fx, fx1 - min_size)
            if "e" in name:
                fx1 = max(fx, fx0 + min_size)
            if "n" in name:
                fy0 = min(fy, fy1 - min_size)
            if "s" in name:
                fy1 = max(fy, fy0 + min_size)
            self.crop_rect = (fx0, fy0, fx1, fy1)
        self._draw_crop_overlay()

    def _crop_release(self, event):
        if self._drag is not None:
            self._drag = None
            if self.crop_rect is not None and is_full_rect(self.crop_rect):
                pass  # 全体選択は適用時に「切り抜きなし」として扱う
            self._draw_crop_overlay()

    # ------------------------------------------------------------ printer
    def _run_bg(self, coro_factory, busy_msg: str, kind: str | None = None):
        if self.busy:
            return
        self.busy = True
        self._cancelling = False
        for btn in (self.print_btn, self.status_btn, self.find_btn):
            btn.config(state="disabled")
        if kind == "print":
            self.cancel_btn.pack(side="right", padx=(0, 10))
        self.status_var.set(busy_msg)
        self._persist()

        def worker():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            task = loop.create_task(coro_factory())
            self._job_loop, self._job_task = loop, task
            try:
                result = loop.run_until_complete(task)
                self.results.put(("ok", result))
            except asyncio.CancelledError:
                self.results.put(("cancelled", None))
            except Exception as exc:  # PrinterError ほか
                self.results.put(("error", explain_error(exc)))
            finally:
                self._job_loop = self._job_task = None
                loop.close()

        threading.Thread(target=worker, daemon=True).start()

    def cancel_print(self):
        """印刷ジョブを中断する。プロトコルに停止コマンドは無いので、
        送信タスクをキャンセルして BLE を切断する。送信済みのパケット
        （数行分）は印刷されてしまう。"""
        loop, task = self._job_loop, self._job_task
        if loop is None or task is None or self._cancelling:
            return
        self._cancelling = True
        self.cancel_btn.config(state="disabled")
        self.status_var.set("キャンセルしています…")
        loop.call_soon_threadsafe(task.cancel)

    def find_printer_ui(self):
        self._run_bg(_find_job, "プリンタを探しています…")

    def check_status(self):
        address = self.address_var.get().strip() or None
        self._run_bg(lambda: _status_job(address), "プリンタに接続しています…")

    def do_print(self):
        if self.gray is None:
            messagebox.showinfo("Lily Print", "先に画像ファイルを選んでね。")
            return
        if self.cropping:
            self.apply_crop()
        address = self.address_var.get().strip() or None
        gray, energy = self.gray, int(self.energy_var.get())
        copies, dither = int(self.copies_var.get()), self.dither_var.get()
        self._run_bg(lambda: _print_job(address, gray, energy, copies, dither),
                     "印刷しています… ♪", kind="print")

    def _poll_results(self):
        try:
            while True:
                kind, payload = self.results.get_nowait()
                self.busy = False
                for btn in (self.print_btn, self.status_btn, self.find_btn):
                    btn.config(state="normal")
                self.cancel_btn.pack_forget()
                self.cancel_btn.config(state="normal")
                if kind == "cancelled":
                    self.status_var.set(
                        "印刷をキャンセルしました（送信済みの数行は出ます）")
                elif kind == "ok":
                    msg, address = payload
                    if address:
                        self.address_var.set(address)
                        self._persist()
                    self.status_var.set(msg)
                else:
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_results)


if __name__ == "__main__":
    LilyPrintApp().mainloop()
