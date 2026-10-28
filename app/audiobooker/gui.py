"""Audiobooker desktop UI (PySide6)."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QThread, QTimer, QUrl, Signal
from PySide6.QtGui import QAction, QDesktopServices, QFont, QIcon, QKeySequence
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QFrame,
    QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSlider, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import APP_NAME, __version__, paths
from .settings import Settings

log = logging.getLogger("audiobooker")

WORDS_PER_MINUTE = 155
OUTPUT_MODES = [
    ("m4b", "M4B audiobook (one file, chapters)"),
    ("mp3_single", "Single MP3 (with chapter markers)"),
    ("mp3_chapters", "One MP3 per chapter"),
]
BITRATES = [(48, "48 kbps — smallest"), (64, "64 kbps — recommended for speech"),
            (96, "96 kbps — high"), (128, "128 kbps — very high")]
OCR_MODES = [("auto", "Automatic (scanned pages only)"), ("always", "Always OCR every page"),
             ("never", "Never (use PDF text only)")]


def fmt_duration(seconds: float) -> str:
    seconds = int(max(0, seconds))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h {m:02d}m"
    if m:
        return f"{m}m {s:02d}s"
    return f"{s}s"


# --------------------------------------------------------------------------
# Background work
# --------------------------------------------------------------------------

class Worker(QThread):
    """Runs fn(report, cancelled) in a thread. report(obj) emits `progress`."""
    progress = Signal(object)
    done = Signal(object)
    failed = Signal(str, str)

    def __init__(self, fn: Callable, parent=None):
        super().__init__(parent)
        self.fn = fn
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        try:
            result = self.fn(self.progress.emit, self.cancel_event.is_set)
            self.done.emit(result)
        except BaseException as e:  # noqa: BLE001
            tb = traceback.format_exc()
            log.error("Background task failed:\n%s", tb)
            name = type(e).__name__
            self.failed.emit(f"{name}: {e}" if str(e) else name, tb)


# --------------------------------------------------------------------------
# Main window
# --------------------------------------------------------------------------

class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = Settings.load()
        self.book = None
        self.worker: Optional[Worker] = None
        self.preview_worker: Optional[Worker] = None
        self.last_outputs: list[Path] = []
        self.gpu_text = "Checking for a GPU…"
        self._engines_cache = None
        self._player = None
        self._audio_out = None

        self.setWindowTitle(APP_NAME)
        self.setAcceptDrops(True)
        self.resize(1280, 820)
        self._build_menu()
        self._build_ui()
        self._load_settings_into_ui()
        if self.settings.geometry:
            try:
                from PySide6.QtCore import QByteArray
                self.restoreGeometry(QByteArray.fromHex(self.settings.geometry.encode()))
            except Exception:
                pass
        QTimer.singleShot(50, self._probe_gpu)

    # ---------------------------------------------------------------- UI build
    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        a = QAction("&Open PDF…", self, shortcut=QKeySequence.Open, triggered=self.open_pdf_dialog)
        m.addAction(a)
        m.addSeparator()
        m.addAction(QAction("E&xit", self, triggered=self.close))

        t = self.menuBar().addMenu("&Tools")
        t.addAction(QAction("Open voice clips folder", self,
                            triggered=lambda: self._open_folder(paths.voices_dir())))
        t.addAction(QAction("Open models folder", self, triggered=lambda: self._open_folder(paths.models_dir())))
        self.clear_cache_action = QAction("Clear rendered-audio cache…", self, triggered=self.clear_cache)
        t.addAction(self.clear_cache_action)
        t.addSeparator()
        t.addAction(QAction("View log", self, triggered=lambda: self._open_folder(paths.logs_dir())))

        h = self.menuBar().addMenu("&Help")
        h.addAction(QAction(f"About {APP_NAME}", self, triggered=self.about))

    def _build_ui(self):
        root = QWidget()
        outer = QVBoxLayout(root)
        outer.setContentsMargins(12, 10, 12, 10)
        outer.setSpacing(10)

        # --- top: book ----------------------------------------------------
        top = QHBoxLayout()
        self.open_btn = QPushButton("Open PDF…")
        self.open_btn.clicked.connect(self.open_pdf_dialog)
        top.addWidget(self.open_btn)
        top.addSpacing(8)
        top.addWidget(QLabel("Title"))
        self.title_edit = QLineEdit()
        self.title_edit.setPlaceholderText("Open or drop a PDF to begin")
        top.addWidget(self.title_edit, 3)
        top.addWidget(QLabel("Author"))
        self.author_edit = QLineEdit()
        top.addWidget(self.author_edit, 2)
        outer.addLayout(top)

        body = QSplitter(Qt.Horizontal)

        # --- left: chapters + preview ------------------------------------
        left = QSplitter(Qt.Vertical)
        chap_box = QWidget()
        cl = QVBoxLayout(chap_box)
        cl.setContentsMargins(0, 0, 0, 0)
        head = QHBoxLayout()
        lbl = QLabel("Chapters")
        f = lbl.font()
        f.setBold(True)
        f.setPointSizeF(f.pointSizeF() + 1)
        lbl.setFont(f)
        head.addWidget(lbl)
        self.book_info = QLabel("")
        self.book_info.setStyleSheet("color: palette(mid);")
        head.addWidget(self.book_info, 1)
        sel_all = QPushButton("Select all")
        sel_all.clicked.connect(lambda: self._set_all(True))
        sel_none = QPushButton("Select none")
        sel_none.clicked.connect(lambda: self._set_all(False))
        head.addWidget(sel_all)
        head.addWidget(sel_none)
        cl.addLayout(head)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["", "Title", "Pages", "Words", "Length"])
        self.table.verticalHeader().setVisible(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.DoubleClicked | QAbstractItemView.EditKeyPressed)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.ResizeToContents)
        hh.setSectionResizeMode(1, QHeaderView.Stretch)
        for c in (2, 3, 4):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        self.table.itemChanged.connect(self._table_item_changed)
        self.table.itemSelectionChanged.connect(self._show_selected_chapter)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._table_menu)
        cl.addWidget(self.table)
        hint = QLabel("Untick chapters to skip them (front matter, index…). Double-click a title to rename it. "
                      "Right-click to merge chapters.")
        hint.setWordWrap(True)
        hint.setStyleSheet("color: palette(mid); font-size: 11px;")
        cl.addWidget(hint)
        left.addWidget(chap_box)

        prev_box = QWidget()
        pl = QVBoxLayout(prev_box)
        pl.setContentsMargins(0, 6, 0, 0)
        plh = QLabel("Text that will be read")
        plh.setFont(f)
        pl.addWidget(plh)
        self.preview = QPlainTextEdit()
        self.preview.setReadOnly(True)
        pf = QFont("Georgia")
        pf.setPointSize(11)
        self.preview.setFont(pf)
        pl.addWidget(self.preview)
        left.addWidget(prev_box)
        left.setSizes([420, 300])
        body.addWidget(left)

        # --- right: settings ---------------------------------------------
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(6, 0, 0, 0)

        nar = QGroupBox("Narration")
        nf = QFormLayout(nar)
        nf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.engine_combo = QComboBox()
        self.engine_combo.currentIndexChanged.connect(self._engine_changed)
        nf.addRow("Engine", self.engine_combo)
        self.engine_desc = QLabel("")
        self.engine_desc.setWordWrap(True)
        self.engine_desc.setStyleSheet("color: palette(mid); font-size: 11px;")
        nf.addRow("", self.engine_desc)

        vrow = QHBoxLayout()
        self.voice_combo = QComboBox()
        self.voice_combo.setMinimumWidth(180)
        self.voice_combo.currentIndexChanged.connect(self._voice_changed)
        vrow.addWidget(self.voice_combo, 1)
        self.preview_btn = QPushButton("▶ Preview")
        self.preview_btn.setToolTip("Hear this voice read the start of the selected chapter")
        self.preview_btn.clicked.connect(self.preview_voice)
        vrow.addWidget(self.preview_btn)
        nf.addRow("Voice", vrow)
        self.add_clip_btn = QPushButton("Add your own voice clip…")
        self.add_clip_btn.setToolTip("10–20 seconds of clean speech (WAV/MP3/FLAC). The voice will be cloned.")
        self.add_clip_btn.clicked.connect(self.add_voice_clip)
        nf.addRow("", self.add_clip_btn)

        srow = QHBoxLayout()
        self.speed_slider = QSlider(Qt.Horizontal)
        self.speed_slider.setRange(70, 150)
        self.speed_slider.setSingleStep(5)
        self.speed_slider.setPageStep(10)
        self.speed_label = QLabel("1.00×")
        self.speed_label.setMinimumWidth(44)
        self.speed_slider.valueChanged.connect(self._speed_changed)
        srow.addWidget(self.speed_slider, 1)
        srow.addWidget(self.speed_label)
        nf.addRow("Speed", srow)

        self.nar_form = nf
        self.opt_rows: list[QWidget] = []
        self.opt_sliders: dict[str, tuple[QSlider, QLabel, object]] = {}
        self.model_status = QLabel("")
        self.model_status.setWordWrap(True)
        self.model_status.setStyleSheet("font-size: 11px;")
        nf.addRow("", self.model_status)
        rl.addWidget(nar)

        out = QGroupBox("Output")
        of = QFormLayout(out)
        of.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.mode_combo = QComboBox()
        for k, label in OUTPUT_MODES:
            self.mode_combo.addItem(label, k)
        of.addRow("Format", self.mode_combo)
        self.bitrate_combo = QComboBox()
        for k, label in BITRATES:
            self.bitrate_combo.addItem(label, k)
        of.addRow("Quality", self.bitrate_combo)
        drow = QHBoxLayout()
        self.out_edit = QLineEdit()
        self.out_edit.setPlaceholderText("Same folder as the PDF")
        browse = QPushButton("Browse…")
        browse.clicked.connect(self.choose_out_dir)
        drow.addWidget(self.out_edit, 1)
        drow.addWidget(browse)
        of.addRow("Save to", drow)
        self.normalize_check = QCheckBox("Even out loudness")
        of.addRow("", self.normalize_check)
        rl.addWidget(out)

        txt = QGroupBox("Reading the PDF")
        tf = QFormLayout(txt)
        tf.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.ocr_combo = QComboBox()
        for k, label in OCR_MODES:
            self.ocr_combo.addItem(label, k)
        tf.addRow("OCR", self.ocr_combo)
        self.footnote_check = QCheckBox("Skip footnotes and note markers")
        tf.addRow("", self.footnote_check)
        self.reload_btn = QPushButton("Re-read PDF with these settings")
        self.reload_btn.clicked.connect(self.reload_book)
        tf.addRow("", self.reload_btn)
        rl.addWidget(txt)
        rl.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(right)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setMinimumWidth(380)
        body.addWidget(scroll)
        body.setStretchFactor(0, 3)
        body.setStretchFactor(1, 1)
        outer.addWidget(body, 1)

        # --- bottom: progress + go ------------------------------------------
        bottom = QHBoxLayout()
        pcol = QVBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(False)
        self.progress.setFixedHeight(10)
        self.status = QLabel("Open a PDF to get started.")
        pcol.addWidget(self.progress)
        pcol.addWidget(self.status)
        bottom.addLayout(pcol, 1)
        self.open_out_btn = QPushButton("Show files")
        self.open_out_btn.setVisible(False)
        self.open_out_btn.clicked.connect(self._show_outputs)
        bottom.addWidget(self.open_out_btn)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setVisible(False)
        self.cancel_btn.clicked.connect(self.cancel_job)
        bottom.addWidget(self.cancel_btn)
        self.go_btn = QPushButton("Create audiobook")
        self.go_btn.setMinimumHeight(36)
        self.go_btn.setMinimumWidth(170)
        gf = self.go_btn.font()
        gf.setBold(True)
        self.go_btn.setFont(gf)
        self.go_btn.setDefault(True)
        self.go_btn.setEnabled(False)
        self.go_btn.clicked.connect(self.start_job)
        bottom.addWidget(self.go_btn)
        outer.addLayout(bottom)

        self.setCentralWidget(root)
        self.gpu_label = QLabel(self.gpu_text)
        self.statusBar().addPermanentWidget(self.gpu_label)

    # ---------------------------------------------------------------- engines
    def _engines(self):
        if self._engines_cache is None:
            from .engines import engine_classes
            self._engines_cache = engine_classes()
        return self._engines_cache

    def _current_engine_cls(self):
        key = self.engine_combo.currentData()
        for c in self._engines():
            if c.key == key:
                return c
        return self._engines()[0]

    def _load_settings_into_ui(self):
        s = self.settings
        self.engine_combo.blockSignals(True)
        for cls in self._engines():
            self.engine_combo.addItem(cls.name, cls.key)
        idx = self.engine_combo.findData(s.engine)
        self.engine_combo.setCurrentIndex(max(0, idx))
        self.engine_combo.blockSignals(False)
        self._engine_changed()
        self.speed_slider.setValue(int(round(s.speed * 100)))
        self._speed_changed(self.speed_slider.value())
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(s.output_mode)))
        self.bitrate_combo.setCurrentIndex(max(0, self.bitrate_combo.findData(s.bitrate_kbps)))
        self.out_edit.setText(s.out_dir)
        self.ocr_combo.setCurrentIndex(max(0, self.ocr_combo.findData(s.ocr)))
        self.footnote_check.setChecked(s.skip_footnotes)
        self.normalize_check.setChecked(s.normalize)

    def _collect_settings(self):
        s = self.settings
        s.engine = self.engine_combo.currentData()
        if self.voice_combo.currentData():
            s.voices[s.engine] = self.voice_combo.currentData()
        s.speed = self.speed_slider.value() / 100.0
        s.output_mode = self.mode_combo.currentData()
        s.bitrate_kbps = self.bitrate_combo.currentData()
        s.out_dir = self.out_edit.text().strip()
        s.ocr = self.ocr_combo.currentData()
        s.skip_footnotes = self.footnote_check.isChecked()
        s.normalize = self.normalize_check.isChecked()
        s.engine_opts[s.engine] = {k: sl.value() / 100.0 for k, (sl, _, _) in self.opt_sliders.items()}
        return s

    def _engine_changed(self, *_):
        cls = self._current_engine_cls()
        self.engine_desc.setText(cls.description)
        self._fill_voices()
        # engine-specific sliders
        for w in self.opt_rows:
            self.nar_form.removeRow(w)
        self.opt_rows = []
        self.opt_sliders = {}
        insert_at = self.nar_form.rowCount() - 1  # just above the model-status line
        saved = self.settings.engine_opts.get(cls.key, {})
        for opt in cls.options:
            row = QHBoxLayout()
            sl = QSlider(Qt.Horizontal)
            sl.setRange(int(opt.minimum * 100), int(opt.maximum * 100))
            sl.setSingleStep(int(opt.step * 100))
            sl.setValue(int(round(saved.get(opt.key, opt.default) * 100)))
            sl.setToolTip(opt.tooltip)
            vl = QLabel(f"{sl.value() / 100:.2f}")
            vl.setMinimumWidth(44)
            sl.valueChanged.connect(lambda v, vl=vl: vl.setText(f"{v / 100:.2f}"))
            row.addWidget(sl, 1)
            row.addWidget(vl)
            w = QWidget()
            w.setLayout(row)
            row.setContentsMargins(0, 0, 0, 0)
            lab = QLabel(opt.label)
            lab.setToolTip(opt.tooltip)
            self.nar_form.insertRow(insert_at, lab, w)
            insert_at += 1
            self.opt_rows.append(w)
            self.opt_sliders[opt.key] = (sl, vl, opt)
        self.add_clip_btn.setVisible(not cls.native_speed or cls.key.startswith("chatterbox"))
        self._update_model_status()

    def _update_model_status(self):
        cls = self._current_engine_cls()
        try:
            from .engines import get_engine
            ready = get_engine(cls.key).is_downloaded()
        except Exception:
            ready = False
        if ready:
            self.model_status.setText("✓ Model downloaded")
            self.model_status.setStyleSheet("font-size: 11px; color: #2e7d32;")
        else:
            self.model_status.setText(f"Model will download automatically on first use ({cls.download_size}).")
            self.model_status.setStyleSheet("font-size: 11px; color: palette(mid);")

    def _fill_voices(self):
        cls = self._current_engine_cls()
        from .engines import get_engine
        voices = get_engine(cls.key).voices()
        want = self.settings.voices.get(cls.key)
        self.voice_combo.blockSignals(True)
        self.voice_combo.clear()
        group = None
        for v in voices:
            if v.group != group:
                if group is not None:
                    self.voice_combo.insertSeparator(self.voice_combo.count())
                group = v.group
            self.voice_combo.addItem(v.label, v.id)
        idx = self.voice_combo.findData(want)
        self.voice_combo.setCurrentIndex(idx if idx >= 0 else 0)
        self.voice_combo.blockSignals(False)

    def _voice_changed(self, *_):
        if self.voice_combo.currentData():
            self.settings.voices[self.engine_combo.currentData()] = self.voice_combo.currentData()

    def _speed_changed(self, v):
        self.speed_label.setText(f"{v / 100:.2f}×")
        self._refresh_info()

    def _probe_gpu(self):
        def work(report, cancelled):
            from .engines import gpu_description
            return gpu_description()

        w = Worker(work, self)
        w.done.connect(self._gpu_probed)
        w.failed.connect(lambda m, tb: self._gpu_probed(""))
        w.start()
        self._gpu_worker = w

    def _gpu_probed(self, desc):
        if desc:
            self.gpu_label.setText(f"GPU: {desc}")
        else:
            self.gpu_label.setText("No NVIDIA GPU found — narration will run on the CPU (much slower)")
            self.gpu_label.setStyleSheet("color: #c62828;")

    # ---------------------------------------------------------------- book
    def open_pdf_dialog(self):
        start = self.settings.last_open_dir or str(Path.home())
        fn, _ = QFileDialog.getOpenFileName(self, "Open a PDF", start, "PDF files (*.pdf)")
        if fn:
            self.load_pdf(fn)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls() and any(u.toLocalFile().lower().endswith(".pdf") for u in e.mimeData().urls()):
            e.acceptProposedAction()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            if u.toLocalFile().lower().endswith(".pdf"):
                self.load_pdf(u.toLocalFile())
                break

    def reload_book(self):
        if self.book:
            self.load_pdf(self.book.path)

    def load_pdf(self, path: str):
        if self._busy():
            return
        self.settings.last_open_dir = str(Path(path).parent)
        s = self._collect_settings()
        ocr, skip = s.ocr, s.skip_footnotes

        def work(report, cancelled):
            from .pdf_extract import load_book
            return load_book(path, ocr=ocr, skip_footnotes=skip,
                             progress=lambda f, m: report((f, m)), cancel=cancelled)

        self._start_worker(work, self._book_loaded, f"Reading {Path(path).name}…")

    def _book_loaded(self, book):
        self._finish_worker()
        self.book = book
        self.title_edit.setText(book.title)
        self.author_edit.setText(book.author)
        self._fill_table()
        src = {"outline": "from the PDF's bookmarks", "headings": "from chapter headings",
               "none": "none found"}[book.chapter_source]
        msg = f"Loaded {Path(book.path).name}: {book.page_count} pages, {len(book.chapters)} chapters ({src})"
        if book.ocr_pages:
            msg += f", {book.ocr_pages} pages read with OCR"
        self.status.setText(msg)
        self.progress.setValue(0)
        self.go_btn.setEnabled(True)
        if book.warnings:
            QMessageBox.information(self, APP_NAME, "\n\n".join(book.warnings))
        if self.table.rowCount():
            first = next((i for i, c in enumerate(book.chapters) if c.include), 0)
            self.table.selectRow(first)

    def _fill_table(self):
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        for i, ch in enumerate(self.book.chapters):
            self.table.insertRow(i)
            chk = QTableWidgetItem()
            chk.setFlags(Qt.ItemIsUserCheckable | Qt.ItemIsEnabled | Qt.ItemIsSelectable)
            chk.setCheckState(Qt.Checked if ch.include else Qt.Unchecked)
            self.table.setItem(i, 0, chk)
            t = QTableWidgetItem(ch.title)
            self.table.setItem(i, 1, t)
            for col, val in ((2, f"{ch.start_page}–{ch.end_page}" if ch.end_page != ch.start_page else str(ch.start_page)),
                             (3, f"{ch.word_count:,}"), (4, "")):
                it = QTableWidgetItem(val)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsSelectable)
                it.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                self.table.setItem(i, col, it)
        self.table.blockSignals(False)
        self._refresh_info()

    def _refresh_info(self):
        if not self.book:
            return
        speed = self.speed_slider.value() / 100.0
        self.table.blockSignals(True)
        for i, ch in enumerate(self.book.chapters):
            it = self.table.item(i, 4)
            if it:
                it.setText(fmt_duration(ch.word_count / WORDS_PER_MINUTE / speed * 60))
        self.table.blockSignals(False)
        n = sum(1 for c in self.book.chapters if c.include)
        words = self.book.word_count
        self.book_info.setText(f"{n} of {len(self.book.chapters)} selected · {words:,} words · "
                               f"about {fmt_duration(words / WORDS_PER_MINUTE / speed * 60)} of audio")

    def _table_item_changed(self, item: QTableWidgetItem):
        if not self.book:
            return
        ch = self.book.chapters[item.row()]
        if item.column() == 0:
            ch.include = item.checkState() == Qt.Checked
            self._refresh_info()
        elif item.column() == 1:
            ch.title = item.text().strip() or ch.title

    def _set_all(self, on: bool):
        if not self.book:
            return
        for i in range(self.table.rowCount()):
            self.table.item(i, 0).setCheckState(Qt.Checked if on else Qt.Unchecked)

    def _selected_chapter_index(self) -> int:
        rows = self.table.selectionModel().selectedRows() if self.table.selectionModel() else []
        return rows[0].row() if rows else -1

    def _show_selected_chapter(self):
        i = self._selected_chapter_index()
        if self.book and 0 <= i < len(self.book.chapters):
            ch = self.book.chapters[i]
            parts = []
            for p in ch.paragraphs:
                parts.append(f"▌ {p.text}" if p.kind == "heading" else p.text)
            self.preview.setPlainText("\n\n".join(parts))

    def _table_menu(self, pos):
        row = self.table.rowAt(pos.y())
        if row < 0 or not self.book:
            return
        menu = QMenu(self)
        merge_prev = menu.addAction("Merge into previous chapter")
        merge_prev.setEnabled(row > 0)
        merge_next = menu.addAction("Merge next chapter into this one")
        merge_next.setEnabled(row < len(self.book.chapters) - 1)
        act = menu.exec(self.table.viewport().mapToGlobal(pos))
        if act is merge_prev:
            self._merge(row - 1, row)
        elif act is merge_next:
            self._merge(row, row + 1)

    def _merge(self, a: int, b: int):
        chs = self.book.chapters
        chs[a].paragraphs += chs[b].paragraphs
        chs[a].end_page = max(chs[a].end_page, chs[b].end_page)
        chs[a].include = chs[a].include or chs[b].include
        del chs[b]
        self._fill_table()
        self.table.selectRow(a)

    # ---------------------------------------------------------------- voices
    def add_voice_clip(self):
        fn, _ = QFileDialog.getOpenFileName(self, "Choose a voice clip (10–20 s of clean speech)",
                                            str(Path.home()), "Audio (*.wav *.mp3 *.flac *.m4a *.ogg)")
        if not fn:
            return
        import subprocess
        from . import audio
        stem = audio.safe_filename(Path(fn).stem, 60)
        dest = paths.voices_dir() / f"{stem}.wav"
        n = 2
        while dest.exists():  # never overwrite: cached narration refers to clips by name
            dest = paths.voices_dir() / f"{stem} ({n}).wav"
            n += 1
        # Convert to 24 kHz mono WAV so any format (m4a, ogg…) loads reliably.
        r = subprocess.run([audio.ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-i", fn, "-ac", "1", "-ar", "24000", "-t", "40", str(dest)],
                           capture_output=True, creationflags=0x08000000 if sys.platform == "win32" else 0)
        if r.returncode != 0 or not dest.exists():
            QMessageBox.warning(self, APP_NAME, "Could not read that audio file:\n\n"
                                + r.stderr.decode("utf-8", "replace")[-500:])
            return
        self.settings.voices[self.engine_combo.currentData()] = f"clip:{dest.name}"
        self._fill_voices()
        self.status.setText(f"Added voice clip “{dest.stem}”.")

    def _preview_text(self) -> str:
        from . import textprep
        i = self._selected_chapter_index()
        text = ""
        if self.book and 0 <= i < len(self.book.chapters):
            body = [p.text for p in self.book.chapters[i].paragraphs if p.kind == "body"]
            if body:
                sents = textprep.split_sentences(textprep.normalize(" ".join(body[:3])))
                for s in sents:
                    if len(text) + len(s) > 260 and text:
                        break
                    text = f"{text} {s}".strip()
        return text or ("Once upon a time, in a quiet village by the sea, there lived an old lighthouse keeper "
                        "who knew every star by name.")

    def preview_voice(self):
        if self._busy():
            return
        s = self._collect_settings()
        text = self._preview_text()
        key, voice, speed = s.engine, s.voices.get(s.engine), s.speed
        opts = dict(s.engine_opts.get(key, {}))

        def work(report, cancelled):
            import numpy as np
            from . import audio
            from .engines import get_engine
            from .engines.base import trim_silence
            eng = get_engine(key)
            eng.load(lambda f, m: report((f, m)), cancelled)
            if hasattr(eng, "prepare_voice"):
                eng.prepare_voice(voice, opts, lambda f, m: report((f, m)))
            report((1.0, "Speaking…"))
            wav = eng.synth(text, voice, speed if eng.native_speed else 1.0, opts)
            wav = trim_silence(wav, eng.sample_rate)
            sr = eng.sample_rate
            if not eng.native_speed and abs(speed - 1) > 1e-3:
                sr = int(sr * speed)  # quick approximation for the preview only
            out = Path(tempfile.gettempdir()) / f"audiobooker_preview_{int(time.time())}.wav"
            audio.write_wav(out, np.concatenate([wav, audio.silence(0.3, eng.sample_rate)]), sr)
            return out

        self._start_worker(work, self._play_preview, "Preparing voice preview…")

    def _play_preview(self, wav_path: Path):
        self._finish_worker()
        self._update_model_status()
        self.status.setText("Playing preview…")
        try:
            from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
            if self._player is None:
                self._player = QMediaPlayer(self)
                self._audio_out = QAudioOutput(self)
                self._player.setAudioOutput(self._audio_out)
            self._player.stop()
            self._player.setSource(QUrl.fromLocalFile(str(wav_path)))
            self._player.play()
        except Exception:
            if sys.platform == "win32":
                import winsound
                winsound.PlaySound(str(wav_path), winsound.SND_FILENAME | winsound.SND_ASYNC)
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(wav_path)))

    # ---------------------------------------------------------------- job
    def choose_out_dir(self):
        d = QFileDialog.getExistingDirectory(self, "Save audiobooks to", self.out_edit.text() or str(Path.home()))
        if d:
            self.out_edit.setText(d)

    def start_job(self):
        if not self.book or self._busy():
            return
        if not any(c.include for c in self.book.chapters):
            QMessageBox.warning(self, APP_NAME, "Tick at least one chapter to narrate.")
            return
        s = self._collect_settings()
        s.save()
        from . import audio, job

        js = job.JobSettings(
            engine=s.engine, voice=s.voices.get(s.engine), speed=s.speed, output_mode=s.output_mode,
            bitrate_kbps=s.bitrate_kbps, out_dir=s.out_dir, normalize=s.normalize,
            engine_opts=dict(s.engine_opts.get(s.engine, {})),
        )
        meta = audio.BookMeta(self.title_edit.text().strip() or self.book.title,
                              self.author_edit.text().strip(), self.book.cover_png)
        import copy
        book = copy.deepcopy(self.book)  # the UI may edit chapters while this runs
        self._job_t0 = None
        self._job_chars0 = 0
        self._job_title = meta.title

        def work(report, cancelled):
            from .engines import get_engine
            eng = get_engine(js.engine)
            return job.run_job(book, js, eng, report, cancelled, meta)

        self.open_out_btn.setVisible(False)
        self._start_worker(work, self._job_done, "Starting…", job_mode=True)

    def _job_done(self, outputs):
        self._finish_worker()
        self.last_outputs = outputs
        self.progress.setValue(1000)
        self.status.setText(f"Done! Saved {len(outputs)} file{'s' if len(outputs) != 1 else ''} to {outputs[0].parent}")
        self.open_out_btn.setVisible(True)
        self._update_model_status()
        QApplication.alert(self)

    def cancel_job(self):
        if self.worker:
            self.worker.cancel()
            self.status.setText("Cancelling… (finished chapters are kept and will be reused next time)")
            self.cancel_btn.setEnabled(False)

    def _show_outputs(self):
        if self.last_outputs:
            p = self.last_outputs[0]
            if sys.platform == "win32":
                import subprocess
                subprocess.Popen(["explorer", "/select,", str(p)])
            else:
                QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))

    # ---------------------------------------------------------------- worker glue
    def _busy(self) -> bool:
        return self.worker is not None and self.worker.isRunning()

    def _start_worker(self, fn, on_done, message: str, job_mode: bool = False):
        self.worker = Worker(fn, self)
        self.worker.progress.connect(self._on_progress)
        self.worker.done.connect(on_done)
        self.worker.failed.connect(self._on_failed)
        self._job_mode = job_mode
        self.status.setText(message)
        self.progress.setValue(0)
        for w in (self.go_btn, self.open_btn, self.preview_btn, self.reload_btn, self.engine_combo,
                  self.voice_combo, self.add_clip_btn, self.table, self.clear_cache_action):
            w.setEnabled(False)
        self.cancel_btn.setVisible(True)
        self.cancel_btn.setEnabled(True)
        self.worker.start()

    def _finish_worker(self):
        for w in (self.go_btn, self.open_btn, self.preview_btn, self.reload_btn, self.engine_combo,
                  self.voice_combo, self.add_clip_btn, self.table, self.clear_cache_action):
            w.setEnabled(True)
        self.go_btn.setEnabled(self.book is not None)
        self.cancel_btn.setVisible(False)

    def _on_progress(self, p):
        if isinstance(p, tuple):
            f, m = p
            self.progress.setValue(int(f * 1000))
            self.status.setText(m)
            return
        # job.Progress
        self.progress.setValue(int(p.fraction * 1000))
        msg = p.message
        if p.chapter_index >= 0 and p.chars_total:
            now = time.time()
            if self._job_t0 is None or "skipping" in msg:
                self._job_t0, self._job_chars0 = now, p.chars_done
            elif now - self._job_t0 > 8 and p.chars_done > self._job_chars0:
                rate = (p.chars_done - self._job_chars0) / (now - self._job_t0)
                remaining = (p.chars_total - p.chars_done) / rate
                msg += f"  ·  about {fmt_duration(remaining)} left"
        self.status.setText(msg)

    def _on_failed(self, message: str, tb: str):
        self._finish_worker()
        self.progress.setValue(0)
        if "cancel" in message.lower():
            self.status.setText("Cancelled. Finished chapters were saved and will be reused if you run it again.")
            return
        hint = ""
        low = message.lower()
        if "out of memory" in low:
            hint = "\n\nThe GPU ran out of memory. Close other GPU-heavy apps, or use the Kokoro engine."
        elif "connection" in low or "resolve" in low or "timed out" in low or "http" in low:
            hint = "\n\nThis looks like a network problem while downloading a model. Check your connection and try again."
        self.status.setText("Something went wrong — see details.")
        box = QMessageBox(QMessageBox.Critical, APP_NAME, f"{message}{hint}", parent=self)
        box.setDetailedText(tb)
        box.exec()

    # ---------------------------------------------------------------- misc
    def clear_cache(self):
        from . import job
        size = job.cache_size_bytes() / 2**20
        if QMessageBox.question(
            self, APP_NAME,
            f"Delete {size:,.0f} MB of rendered chapter audio?\n\nThis is only used to resume interrupted "
            "books and to re-export quickly. Your finished audiobooks are not affected.",
        ) == QMessageBox.Yes:
            job.clear_cache()
            self.status.setText("Cache cleared.")

    def _open_folder(self, p: Path):
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(p)))

    def about(self):
        QMessageBox.about(
            self, f"About {APP_NAME}",
            f"<b>{APP_NAME}</b> {__version__}<br><br>Turns PDFs into audiobooks using text-to-speech models "
            "that run entirely on this computer.<br><br>Engines: Kokoro-82M (Apache-2.0, hexgrad), "
            "Chatterbox and Chatterbox Turbo (MIT, Resemble AI). OCR: RapidOCR. PDF: PyMuPDF.<br><br>"
            f"Data folder: {paths.data_root()}",
        )

    def closeEvent(self, e):
        if self._busy():
            if QMessageBox.question(self, APP_NAME, "Narration is still running. Stop and quit?") != QMessageBox.Yes:
                e.ignore()
                return
            # Let the worker stop cleanly (it checks for cancel between chunks),
            # then quit; destroying a running QThread would crash.
            self.worker.cancel()
            self._save_window_state()
            self.hide()
            self.worker.finished.connect(QApplication.quit)
            e.ignore()
            return
        self._save_window_state()
        e.accept()

    def _save_window_state(self):
        try:
            self._collect_settings()
            self.settings.geometry = bytes(self.saveGeometry().toHex()).decode()
            self.settings.save()
        except Exception:
            pass


def main() -> int:
    paths.configure_model_cache()
    log_file = paths.logs_dir() / "audiobooker.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[logging.FileHandler(log_file, encoding="utf-8")]
        + ([logging.StreamHandler()] if sys.stderr else []),
    )
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("TQDM_DISABLE", "1")
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    icon = Path(__file__).with_name("icon.png")
    if icon.exists():
        app.setWindowIcon(QIcon(str(icon)))
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1 and sys.argv[1].lower().endswith(".pdf") and Path(sys.argv[1]).exists():
        QTimer.singleShot(200, lambda: w.load_pdf(sys.argv[1]))
    return app.exec()
