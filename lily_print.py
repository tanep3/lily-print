# -*- coding: utf-8 -*-
"""
Lily Print — Thermal Mini Printer -Lily- (LX-D02) 用 Windows GUI 印刷アプリ

ファイルを選ぶ → 384px 実寸プレビュー → 印刷ボタン、の単発アプリ。
印刷エンジンは OSS の lxd02-printer ライブラリを使う。

使い方:
    pip install lxd02-printer tkinterdnd2
    python lily_print.py
    (tkinterdnd2 が無い場合はドラッグ＆ドロップだけ無効になる)
"""
import asyncio
import json
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
PREVIEW_ZOOM = 2  # プレビュー表示倍率（ドットが均等になる整数倍のみ）

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

FONT = "Yu Gothic UI"
F_TITLE = (FONT, 26, "bold")
F_BASE = (FONT, 15)
F_BIGBTN = (FONT, 19, "bold")
F_BTN = (FONT, 15, "bold")
F_STATUS = (FONT, 14)
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


# ---------------------------------------------------------------- GUI
def cute_button(parent, text, command, bg, fg="#FFFFFF", font=F_BTN, **kw):
    kw.setdefault("padx", 18)
    kw.setdefault("pady", 8)
    return tk.Button(parent, text=text, command=command, font=font,
                     bg=bg, fg=fg, activebackground=bg, activeforeground=fg,
                     relief="flat", bd=0, cursor="hand2", **kw)


class LilyPrintApp(TkinterDnD.Tk if HAS_DND else tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Lily Print ♡ LX-D02")
        self.configure(bg=BG)
        self.resizable(False, False)

        cfg = load_config()
        self.gray: Image.Image | None = None
        self.results: queue.Queue = queue.Queue()
        self.busy = False

        self.address_var = tk.StringVar(value=cfg.get("address", ""))
        self.dither_var = tk.BooleanVar(value=True)
        self.energy_var = tk.IntVar(value=3)
        self.copies_var = tk.IntVar(value=1)
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
        cute_button(top, "ファイルを開く", self.open_file, PURPLE).pack(side="left")
        cute_button(top, "貼り付け", self.paste_clipboard, PURPLE).pack(
            side="left", padx=(10, 0))
        tk.Label(top, textvariable=self.file_var, font=F_BASE,
                 bg=BG, fg=TEXT).pack(side="left", padx=12)

        # プレビュー（白パネル＋ピンクの枠、縦はスクロール）
        panel = tk.Frame(self, bg=PANEL, highlightbackground=LINE,
                         highlightthickness=2)
        panel.pack(padx=16, pady=10)
        # 画面の高さに収まるよう、プレビュー枠の高さを決める
        pv_h = max(240, min(440, self.winfo_screenheight() - 470))
        self.preview_canvas = tk.Canvas(
            panel, width=WIDTH * PREVIEW_ZOOM, height=pv_h, bg=PANEL,
            highlightthickness=0)
        self.preview_canvas.pack(side="left", padx=6, pady=6)
        pv_scroll = tk.Scrollbar(panel, orient="vertical",
                                 command=self.preview_canvas.yview)
        pv_scroll.pack(side="right", fill="y", pady=6)
        self.preview_canvas.config(yscrollcommand=pv_scroll.set)
        self.preview_canvas.create_text(
            WIDTH * PREVIEW_ZOOM // 2, pv_h // 2, anchor="center",
            text="ここにプレビューが出ます\n（横 384 ドット実寸 ×2）\n\n"
                 "画像ファイルをドラッグ＆ドロップでもOK\n"
                 "コピーした画像は「貼り付け」か Ctrl+V でもOK\n"
                 "縦に長い画像はホイールでスクロール",
            font=F_BASE, fill=SUBTLE, justify="center", tags="placeholder")
        self.preview_canvas.bind(
            "<MouseWheel>",
            lambda e: self.preview_canvas.yview_scroll(
                -1 if e.delta > 0 else 1, "units"))
        self._photo = None

        # オプション
        opts = tk.Frame(self, bg=BG)
        opts.pack(fill="x", padx=16, pady=4)
        for text, val in (("ディザ（写真むき）", True), ("二値（QR・線画むき）", False)):
            tk.Radiobutton(opts, text=text, variable=self.dither_var, value=val,
                           command=self.refresh_preview, font=F_BASE,
                           bg=BG, fg=TEXT, selectcolor=BG,
                           activebackground=BG).pack(side="left", padx=(0, 10))
        tk.Label(opts, text="濃さ", font=F_BASE, bg=BG, fg=TEXT).pack(side="left")
        tk.Spinbox(opts, from_=1, to=6, width=3, textvariable=self.energy_var,
                   font=F_BASE).pack(side="left", padx=(2, 12))
        tk.Label(opts, text="部数", font=F_BASE, bg=BG, fg=TEXT).pack(side="left")
        tk.Spinbox(opts, from_=1, to=9, width=3, textvariable=self.copies_var,
                   font=F_BASE).pack(side="left", padx=2)

        # MAC アドレス（空なら自動探索して保存する）
        addr = tk.Frame(self, bg=BG)
        addr.pack(fill="x", padx=16, pady=4)
        tk.Label(addr, text="プリンタ MAC", font=F_BASE,
                 bg=BG, fg=TEXT).pack(side="left")
        self.address_entry = tk.Entry(addr, textvariable=self.address_var,
                                      width=19, font=F_MONO)
        self.address_entry.pack(side="left", padx=6)
        self.find_btn = cute_button(addr, "探す", self.find_printer_ui, PURPLE)
        self.find_btn.pack(side="left")

        # ボタン行
        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", padx=16, pady=(10, 2))
        self.status_btn = cute_button(btns, "状態を確認", self.check_status,
                                      PANEL, fg=TEXT)
        self.status_btn.config(highlightbackground=LINE, highlightthickness=1)
        self.status_btn.pack(side="left")
        self.print_btn = cute_button(btns, "♡ 印刷する ♡", self.do_print,
                                     ACCENT, font=F_BIGBTN, padx=30, pady=12)
        self.print_btn.pack(side="right")

        # ステータス
        tk.Label(self, textvariable=self.status_var, font=F_STATUS,
                 bg=BG, fg=TEXT, wraplength=800, justify="left",
                 anchor="w").pack(fill="x", padx=16, pady=(6, 14))

        # ドラッグ＆ドロップ（tkinterdnd2 がある場合のみ）
        if HAS_DND:
            for w in (self, self.preview_canvas):
                w.drop_target_register(DND_FILES)
                w.dnd_bind("<<Drop>>", self.on_drop)

        # Ctrl+V でクリップボードの画像を貼る（MAC 欄にフォーカス中は除く）
        self.bind("<Control-v>", self._on_ctrl_v)

        self.after(100, self._poll_results)

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
        self.gray = gray
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

    def refresh_preview(self):
        if self.gray is None:
            return
        pv = preview_image(self.gray, self.dither_var.get())
        zoomed = pv.resize((pv.width * PREVIEW_ZOOM, pv.height * PREVIEW_ZOOM),
                           Image.NEAREST)
        self._photo = ImageTk.PhotoImage(zoomed)
        cv = self.preview_canvas
        cv.delete("all")
        cv.create_image(0, 0, anchor="nw", image=self._photo)
        cv.config(scrollregion=(0, 0, zoomed.width, zoomed.height))
        cv.yview_moveto(0)

    def _run_bg(self, coro_factory, busy_msg: str):
        if self.busy:
            return
        self.busy = True
        for btn in (self.print_btn, self.status_btn, self.find_btn):
            btn.config(state="disabled")
        self.status_var.set(busy_msg)
        if self.address_var.get().strip():
            save_config({"address": self.address_var.get().strip()})

        def worker():
            try:
                result = asyncio.run(coro_factory())
                self.results.put(("ok", result))
            except Exception as exc:  # PrinterError ほか
                self.results.put(("error", explain_error(exc)))

        threading.Thread(target=worker, daemon=True).start()

    def find_printer_ui(self):
        self._run_bg(_find_job, "プリンタを探しています…")

    def check_status(self):
        address = self.address_var.get().strip() or None
        self._run_bg(lambda: _status_job(address), "プリンタに接続しています…")

    def do_print(self):
        if self.gray is None:
            messagebox.showinfo("Lily Print", "先に画像ファイルを選んでね。")
            return
        address = self.address_var.get().strip() or None
        gray, energy = self.gray, int(self.energy_var.get())
        copies, dither = int(self.copies_var.get()), self.dither_var.get()
        self._run_bg(lambda: _print_job(address, gray, energy, copies, dither),
                     "印刷しています… ♪")

    def _poll_results(self):
        try:
            while True:
                kind, payload = self.results.get_nowait()
                self.busy = False
                for btn in (self.print_btn, self.status_btn, self.find_btn):
                    btn.config(state="normal")
                if kind == "ok":
                    msg, address = payload
                    if address:
                        self.address_var.set(address)
                        save_config({"address": address})
                    self.status_var.set(msg)
                else:
                    self.status_var.set(payload)
        except queue.Empty:
            pass
        self.after(100, self._poll_results)


if __name__ == "__main__":
    LilyPrintApp().mainloop()
