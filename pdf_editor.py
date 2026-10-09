"""Редактор PDF — правка, разделение, объединение и экспорт PDF в JPG/PNG (PySide6 + PyMuPDF)."""

import math
import os
import re
import sys
from pathlib import Path

import pymupdf
from PySide6.QtCore import QPointF, QRectF, QSettings, QSize, Qt, QTimer, Signal
from PySide6.QtGui import (
    QAction, QActionGroup, QColor, QIcon, QImage, QKeySequence, QPainter,
    QPainterPath, QPalette, QPen, QPixmap,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QColorDialog, QComboBox,
    QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QMainWindow, QMessageBox, QProgressDialog, QPushButton, QRadioButton,
    QScrollArea, QSpinBox, QSplitter, QStyleFactory, QToolButton,
    QVBoxLayout, QWidget,
)

APP = "Редактор PDF"
PDF_FILTER = "PDF (*.pdf)"
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".gif", ".tif", ".tiff", ".webp"}
OPEN_FILTER = ("PDF и изображения (*.pdf *.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp);;"
               "PDF (*.pdf);;Все файлы (*)")
IMG_FILTER = "Изображения (*.jpg *.jpeg *.png *.bmp *.gif *.tif *.tiff *.webp)"
THUMB_W = 120
UNDO_LIMIT = 30
FONT_CANDIDATES = [
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/Library/Fonts/Arial.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/TTF/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
]

# Инструменты правки: id -> (подпись, подсказка)
TOOLS = {
    "pointer": ("Указатель", "Обычный просмотр"),
    "text": ("Текст", "Щёлкните на странице, чтобы добавить текст"),
    "edit": ("Править текст", "Щёлкните по строке текста, чтобы изменить её"),
    "erase": ("Стереть", "Выделите область — содержимое будет удалено (закрашено белым)"),
    "highlight": ("Маркер", "Выделите область для подсветки"),
    "box": ("Рамка", "Выделите область, чтобы нарисовать рамку"),
    "ink": ("Карандаш", "Рисование от руки, подпись"),
    "image": ("Картинка", "Выделите область, куда вставить изображение"),
}
RECT_TOOLS = {"erase", "highlight", "box", "image"}


def find_font():
    """Шрифт с кириллицей для вставки текста (None — встроенный Helvetica)."""
    for f in FONT_CANDIDATES:
        if os.path.exists(f):
            return f
    return None


def parse_ranges(text, count):
    """'1-3, 5, 8-' -> [[0,1,2],[4],[7..count-1]] (номера с 1, результат с 0)."""
    groups = []
    for part in re.split(r"[,;]", text):
        part = part.strip()
        if not part:
            continue
        m = re.fullmatch(r"(\d*)\s*-\s*(\d*)|(\d+)", part)
        if not m:
            raise ValueError(f"Непонятный диапазон: «{part}»")
        if m.group(3):
            a = b = int(m.group(3))
        else:
            a = int(m.group(1)) if m.group(1) else 1
            b = int(m.group(2)) if m.group(2) else count
        if not (1 <= a <= count and 1 <= b <= count):
            raise ValueError(f"Страницы «{part}» нет в документе (всего {count})")
        step = 1 if b >= a else -1
        groups.append(list(range(a - 1, b - 1 + step, step)))
    if not groups:
        raise ValueError("Не указаны страницы")
    return groups


def open_as_pdf(path):
    """Открыть PDF или изображение как PDF-документ."""
    path = Path(path)
    if path.suffix.lower() in IMG_EXT:
        img = pymupdf.open(path)
        doc = pymupdf.open("pdf", img.convert_to_pdf())
        img.close()
        return doc
    return pymupdf.open("pdf", path.read_bytes())


def to_qimage(pix):
    fmt = QImage.Format_RGBA8888 if pix.alpha else QImage.Format_RGB888
    return QImage(pix.samples, pix.width, pix.height, pix.stride, fmt).copy()


def color_tuple(qc):
    return (qc.redF(), qc.greenF(), qc.blueF())


class ThumbList(QListWidget):
    """Миниатюры страниц; перетаскивание меняет порядок."""

    reordered = Signal()

    def __init__(self):
        super().__init__()
        self.setIconSize(QSize(THUMB_W, int(THUMB_W * 1.42)))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setSpacing(4)
        self.setMinimumWidth(THUMB_W + 90)

    def dropEvent(self, e):
        super().dropEvent(e)
        self.reordered.emit()


class PageView(QWidget):
    """Отрисованная страница; принимает действия мышью для инструментов."""

    def __init__(self, main):
        super().__init__()
        self.main = main
        self.pix = None
        self.start = None
        self.cur = None
        self.ink = []
        self.setMouseTracking(True)

    def set_pixmap(self, pix):
        self.pix = pix
        if pix:
            self.setFixedSize(pix.deviceIndependentSize().toSize())
        self.update()

    def paintEvent(self, _):
        if not self.pix:
            return
        p = QPainter(self)
        p.drawPixmap(0, 0, self.pix)
        p.setRenderHint(QPainter.Antialiasing)
        if self.start and self.cur and self.main.tool in RECT_TOOLS:
            p.setPen(QPen(QColor(0, 120, 215), 1, Qt.DashLine))
            p.setBrush(QColor(0, 120, 215, 40))
            p.drawRect(QRectF(self.start, self.cur).normalized())
        if len(self.ink) > 1:
            p.setPen(QPen(self.main.color, max(1.0, self.main.line_width * self.main.scale),
                          Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            path = QPainterPath(self.ink[0])
            for pt in self.ink[1:]:
                path.lineTo(pt)
            p.drawPath(path)

    def mousePressEvent(self, e):
        if e.button() != Qt.LeftButton or not self.pix:
            return
        pos = e.position()
        tool = self.main.tool
        if tool == "text":
            self.main.add_text(pos)
        elif tool == "edit":
            self.main.edit_text(pos)
        elif tool in RECT_TOOLS:
            self.start = self.cur = pos
        elif tool == "ink":
            self.ink = [pos]

    def mouseMoveEvent(self, e):
        pos = e.position()
        if self.start:
            self.cur = pos
            self.update()
        elif self.ink:
            self.ink.append(pos)
            self.update()

    def mouseReleaseEvent(self, e):
        if e.button() != Qt.LeftButton:
            return
        if self.start:
            r = QRectF(self.start, e.position()).normalized()
            self.start = self.cur = None
            self.update()
            if r.width() > 3 and r.height() > 3:
                self.main.apply_rect(r)
        elif self.ink:
            pts, self.ink = self.ink, []
            self.update()
            if len(pts) > 1:
                self.main.add_ink(pts)


class ExportDialog(QDialog):
    """Параметры экспорта страниц в изображения."""

    def __init__(self, parent, count, has_selection, folder):
        super().__init__(parent)
        self.setWindowTitle("Экспорт в изображения")
        self.count = count
        form = QFormLayout(self)

        self.fmt = QComboBox()
        self.fmt.addItems(["JPG", "PNG"])
        form.addRow("Формат:", self.fmt)

        self.dpi = QSpinBox()
        self.dpi.setRange(36, 1200)
        self.dpi.setValue(150)
        self.dpi.setSuffix(" dpi")
        self.dpi.setToolTip("72 — экран, 150 — обычно, 300 — печать")
        form.addRow("Качество (разрешение):", self.dpi)

        self.quality = QSpinBox()
        self.quality.setRange(10, 100)
        self.quality.setValue(90)
        self.quality.setSuffix(" %")
        form.addRow("Сжатие JPG:", self.quality)
        self.fmt.currentTextChanged.connect(lambda t: self.quality.setEnabled(t == "JPG"))

        self.r_all = QRadioButton(f"Все страницы ({count})")
        self.r_sel = QRadioButton("Выделенные страницы")
        self.r_rng = QRadioButton("Диапазон:")
        self.rng = QLineEdit()
        self.rng.setPlaceholderText("например 1-3, 5, 8-")
        self.r_sel.setEnabled(has_selection)
        (self.r_sel if has_selection else self.r_all).setChecked(True)
        self.rng.textEdited.connect(lambda _: self.r_rng.setChecked(True))
        box = QVBoxLayout()
        box.addWidget(self.r_all)
        box.addWidget(self.r_sel)
        row = QHBoxLayout()
        row.addWidget(self.r_rng)
        row.addWidget(self.rng)
        box.addLayout(row)
        form.addRow("Страницы:", box)

        self.folder = FolderPicker(folder)
        form.addRow("Папка:", self.folder)

        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Экспорт")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class SplitDialog(QDialog):
    """Параметры разделения документа на файлы."""

    def __init__(self, parent, count, folder):
        super().__init__(parent)
        self.setWindowTitle("Разделить документ")
        form = QFormLayout(self)
        self.r_each = QRadioButton("Каждая страница — отдельный файл")
        self.r_n = QRadioButton("По")
        self.n = QSpinBox()
        self.n.setRange(1, max(1, count))
        self.n.setValue(min(2, max(1, count)))
        self.n.setSuffix(" стр. в файле")
        self.r_rng = QRadioButton("По диапазонам:")
        self.rng = QLineEdit()
        self.rng.setPlaceholderText("1-3, 4-10, 11-  (каждый — отдельный файл)")
        self.r_each.setChecked(True)
        self.n.valueChanged.connect(lambda _: self.r_n.setChecked(True))
        self.rng.textEdited.connect(lambda _: self.r_rng.setChecked(True))
        box = QVBoxLayout()
        box.addWidget(self.r_each)
        row = QHBoxLayout()
        row.addWidget(self.r_n)
        row.addWidget(self.n)
        row.addStretch()
        box.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(self.r_rng)
        row.addWidget(self.rng)
        box.addLayout(row)
        form.addRow(f"Всего страниц: {count}", box)
        self.folder = FolderPicker(folder)
        form.addRow("Папка:", self.folder)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.button(QDialogButtonBox.Ok).setText("Разделить")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)


class FolderPicker(QWidget):
    def __init__(self, folder):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.edit = QLineEdit(folder)
        btn = QPushButton("Обзор…")
        btn.clicked.connect(self.browse)
        lay.addWidget(self.edit)
        lay.addWidget(btn)

    def browse(self):
        d = QFileDialog.getExistingDirectory(self, "Папка", self.edit.text())
        if d:
            self.edit.setText(d)

    def path(self):
        return Path(self.edit.text().strip() or ".")


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.settings = QSettings("redactor", "pdf_editor")
        self.doc = None
        self.path = None
        self.page_no = 0
        self.zoom = 1.0
        self.scale = 1.0
        self.modified = False
        self.undo_stack = []
        self.redo_stack = []
        self.tool = "pointer"
        self.color = QColor(self.settings.value("color", "#000000"))
        self.line_width = 2.0
        self.font_file = find_font()

        self.thumbs = ThumbList()
        self.thumbs.currentRowChanged.connect(self.on_thumb)
        self.thumbs.reordered.connect(self.on_reordered)
        self.view = PageView(self)
        self.scroll = QScrollArea()
        self.scroll.setWidget(self.view)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setBackgroundRole(QPalette.Dark)
        self.scroll.viewport().installEventFilter(self)
        self.hint = QLabel(
            "<h2>Редактор PDF</h2><p>Откройте PDF (Ctrl+O) или перетащите файл в окно.</p>"
            "<p>Можно править текст, удалять и поворачивать страницы,<br>"
            "вставлять другие PDF и картинки, разделять документ<br>"
            "и сохранять страницы в JPG / PNG.</p>")
        self.hint.setAlignment(Qt.AlignCenter)

        splitter = QSplitter()
        splitter.addWidget(self.thumbs)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(self.hint)
        rl.addWidget(self.scroll)
        splitter.addWidget(right)
        splitter.setStretchFactor(1, 1)
        splitter.setSizes([THUMB_W + 110, 900])
        self.setCentralWidget(splitter)

        self.page_label = QLabel()
        self.zoom_label = QLabel()
        self.statusBar().addPermanentWidget(self.page_label)
        self.statusBar().addPermanentWidget(self.zoom_label)

        self.build_actions()
        self.setAcceptDrops(True)
        self.resize(1200, 850)
        geo = self.settings.value("geometry")
        if geo:
            self.restoreGeometry(geo)
        self.update_ui()

    # ---------- интерфейс ----------

    def act(self, text, slot, key=None, tip=None):
        a = QAction(text, self)
        a.triggered.connect(slot)
        if key:
            a.setShortcut(QKeySequence(key))
        if tip:
            a.setStatusTip(tip)
            a.setToolTip(tip)
        return a

    def build_actions(self):
        A = self.act
        self.a_open = A("Открыть…", self.open_dialog, QKeySequence.Open)
        self.a_new = A("Собрать PDF из файлов…", self.new_from_files, QKeySequence.New,
                       "Объединить несколько PDF и картинок в новый документ")
        self.a_save = A("Сохранить", self.save, QKeySequence.Save)
        self.a_save_as = A("Сохранить как…", self.save_as, QKeySequence.SaveAs)
        self.a_export = A("В JPG / PNG…", self.export_images, "Ctrl+E",
                          "Сохранить страницы как изображения")
        self.a_close = A("Закрыть", self.close_doc, QKeySequence.Close)
        self.a_quit = A("Выход", self.close, QKeySequence.Quit)

        self.a_undo = A("Отменить", self.undo, QKeySequence.Undo)
        self.a_redo = A("Повторить", self.redo, "Ctrl+Y")

        self.a_insert = A("Вставить PDF / картинку…", self.insert_files, "Ctrl+I",
                          "Добавить в документ страницы из другого PDF или изображения")
        self.a_blank = A("Пустая страница", self.insert_blank, tip="Вставить пустую страницу после текущей")
        self.a_split = A("Разделить…", self.split, "Ctrl+D", "Разделить документ на несколько файлов")
        self.a_extract = A("Извлечь выделенные…", self.extract,
                           tip="Сохранить выделенные страницы в новый PDF")
        self.a_rot_l = A("⟲ Влево", lambda: self.rotate(-90), "Ctrl+L")
        self.a_rot_r = A("⟳ Вправо", lambda: self.rotate(90), "Ctrl+R")
        self.a_delete = A("Удалить стр.", self.delete_pages, QKeySequence.Delete,
                          "Удалить выделенные страницы")
        self.a_up = A("▲ Выше", lambda: self.move_pages(-1), "Ctrl+Up")
        self.a_down = A("▼ Ниже", lambda: self.move_pages(1), "Ctrl+Down")

        self.a_prev = A("◀", lambda: self.go(self.page_no - 1), QKeySequence.MoveToPreviousPage)
        self.a_next = A("▶", lambda: self.go(self.page_no + 1), QKeySequence.MoveToNextPage)
        self.a_zin = A("+", lambda: self.set_zoom(self.zoom * 1.2), QKeySequence.ZoomIn)
        self.a_zout = A("−", lambda: self.set_zoom(self.zoom / 1.2), QKeySequence.ZoomOut)
        self.a_fit = A("По ширине", self.fit_width, "Ctrl+0")

        m = self.menuBar().addMenu("&Файл")
        for a in (self.a_open, self.a_new, None, self.a_save, self.a_save_as, None,
                  self.a_export, self.a_split, self.a_extract, None, self.a_close, self.a_quit):
            m.addSeparator() if a is None else m.addAction(a)
        self.recent_menu = self.menuBar().addMenu("&Недавние")
        self.recent_menu.aboutToShow.connect(self.fill_recent)
        m = self.menuBar().addMenu("&Правка")
        m.addAction(self.a_undo)
        m.addAction(self.a_redo)
        m = self.menuBar().addMenu("&Страницы")
        for a in (self.a_insert, self.a_blank, None, self.a_rot_l, self.a_rot_r, self.a_up,
                  self.a_down, None, self.a_delete, self.a_extract, self.a_split):
            m.addSeparator() if a is None else m.addAction(a)
        m = self.menuBar().addMenu("&Вид")
        for a in (self.a_zin, self.a_zout, self.a_fit, None, self.a_prev, self.a_next):
            m.addSeparator() if a is None else m.addAction(a)

        tb = self.addToolBar("Файл")
        tb.setObjectName("file")
        for a in (self.a_open, self.a_save, self.a_undo, self.a_redo, None, self.a_insert,
                  self.a_split, self.a_export, None, self.a_rot_l, self.a_rot_r, self.a_delete,
                  None, self.a_prev, self.a_next, self.a_zout, self.a_zin, self.a_fit):
            tb.addSeparator() if a is None else tb.addAction(a)

        self.addToolBarBreak()
        tb = self.addToolBar("Правка")
        tb.setObjectName("edit")
        group = QActionGroup(self)
        self.tool_actions = {}
        for key, (text, tip) in TOOLS.items():
            a = QAction(text, self, checkable=True)
            a.setStatusTip(tip)
            a.setToolTip(tip)
            a.triggered.connect(lambda _=False, k=key: self.set_tool(k))
            group.addAction(a)
            tb.addAction(a)
            self.tool_actions[key] = a
        self.tool_actions["pointer"].setChecked(True)
        tb.addSeparator()
        self.color_btn = QToolButton()
        self.color_btn.setToolTip("Цвет текста и рисования")
        self.color_btn.clicked.connect(self.pick_color)
        tb.addWidget(self.color_btn)
        tb.addWidget(QLabel(" Размер шрифта: "))
        self.font_size = QSpinBox()
        self.font_size.setRange(4, 200)
        self.font_size.setValue(int(self.settings.value("font_size", 12)))
        tb.addWidget(self.font_size)
        self.update_color_btn()

        self.doc_actions = [self.a_save, self.a_save_as, self.a_export, self.a_close, self.a_blank,
                            self.a_split, self.a_extract, self.a_rot_l, self.a_rot_r,
                            self.a_delete, self.a_up, self.a_down, self.a_prev, self.a_next,
                            self.a_zin, self.a_zout, self.a_fit, *self.tool_actions.values()]

    def update_color_btn(self):
        pm = QPixmap(18, 18)
        pm.fill(self.color)
        self.color_btn.setIcon(QIcon(pm))

    def pick_color(self):
        c = QColorDialog.getColor(self.color, self, "Цвет")
        if c.isValid():
            self.color = c
            self.settings.setValue("color", c.name())
            self.update_color_btn()

    def set_tool(self, key):
        self.tool = key
        cur = {"pointer": Qt.ArrowCursor, "text": Qt.IBeamCursor, "edit": Qt.PointingHandCursor}
        self.view.setCursor(cur.get(key, Qt.CrossCursor))
        self.statusBar().showMessage(TOOLS[key][1], 5000)

    def update_ui(self):
        has = self.doc is not None
        for a in self.doc_actions:
            a.setEnabled(has)
        self.a_undo.setEnabled(bool(self.undo_stack))
        self.a_redo.setEnabled(bool(self.redo_stack))
        self.hint.setVisible(not has)
        self.scroll.setVisible(has)
        name = self.path.name if self.path else ("без названия.pdf" if has else "")
        title = f"{'• ' if self.modified else ''}{name} — {APP}" if has else APP
        self.setWindowTitle(title)
        if has:
            self.page_label.setText(f"Стр. {self.page_no + 1} из {len(self.doc)}")
            self.zoom_label.setText(f"{round(self.zoom * 100)}%")
        else:
            self.page_label.clear()
            self.zoom_label.clear()

    # ---------- отрисовка ----------

    def render_thumbs(self):
        self.thumbs.blockSignals(True)
        self.thumbs.clear()
        for i, page in enumerate(self.doc):
            it = QListWidgetItem(self.thumb_icon(page), str(i + 1))
            it.setData(Qt.UserRole, i)
            self.thumbs.addItem(it)
        self.thumbs.setCurrentRow(self.page_no)
        self.thumbs.blockSignals(False)

    def refresh_thumb(self, i):
        self.thumbs.item(i).setIcon(self.thumb_icon(self.doc[i]))

    def thumb_icon(self, page):
        dpr = self.devicePixelRatioF()
        s = THUMB_W / max(page.rect.width, page.rect.height / 1.42, 1) * dpr
        img = to_qimage(page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False))
        p = QPainter(img)
        p.setPen(QColor(150, 150, 150))
        p.drawRect(0, 0, img.width() - 1, img.height() - 1)
        p.end()
        pm = QPixmap.fromImage(img)
        pm.setDevicePixelRatio(dpr)
        return QIcon(pm)

    def render_page(self):
        if not self.doc or not len(self.doc):
            self.view.set_pixmap(None)
            return
        self.page_no = max(0, min(self.page_no, len(self.doc) - 1))
        page = self.doc[self.page_no]
        dpr = self.devicePixelRatioF()
        self.scale = self.zoom * 96 / 72
        s = self.scale * dpr
        pix = page.get_pixmap(matrix=pymupdf.Matrix(s, s), alpha=False)
        qp = QPixmap.fromImage(to_qimage(pix))
        qp.setDevicePixelRatio(dpr)
        self.view.set_pixmap(qp)
        self.update_ui()

    def refresh(self, full=True):
        """Перерисовать после изменения документа."""
        if full:
            self.render_thumbs()
        else:
            self.refresh_thumb(self.page_no)
        self.render_page()

    def go(self, n):
        if self.doc and 0 <= n < len(self.doc):
            self.thumbs.setCurrentRow(n)

    def on_thumb(self, row):
        if row >= 0 and self.doc:
            self.page_no = row
            self.render_page()
            self.scroll.verticalScrollBar().setValue(0)

    def set_zoom(self, z):
        self.zoom = max(0.1, min(z, 8.0))
        self.render_page()

    def fit_width(self):
        if self.doc:
            w = self.scroll.viewport().width() - 20
            self.set_zoom(w / (self.doc[self.page_no].rect.width * 96 / 72))

    def eventFilter(self, obj, e):
        if obj is self.scroll.viewport() and e.type() == e.Type.Wheel:
            if e.modifiers() & Qt.ControlModifier:
                self.set_zoom(self.zoom * (1.1 if e.angleDelta().y() > 0 else 1 / 1.1))
                return True
            bar = self.scroll.verticalScrollBar()
            dy = e.angleDelta().y()
            if dy < 0 and bar.value() == bar.maximum() and self.page_no < len(self.doc or []) - 1:
                self.go(self.page_no + 1)
                return True
            if dy > 0 and bar.value() == bar.minimum() and self.page_no > 0:
                self.go(self.page_no - 1)
                QTimer.singleShot(0, lambda: bar.setValue(bar.maximum()))
                return True
        return super().eventFilter(obj, e)

    # ---------- координаты ----------

    def to_pdf(self, pos):
        """Точка экрана -> координаты PDF (без учёта поворота страницы)."""
        page = self.doc[self.page_no]
        return pymupdf.Point(pos.x() / self.scale, pos.y() / self.scale) * page.derotation_matrix

    def rect_to_pdf(self, r):
        return pymupdf.Rect(self.to_pdf(r.topLeft()), self.to_pdf(r.bottomRight())).normalize()

    # ---------- отмена ----------

    def snapshot(self):
        self.undo_stack.append((self.doc.tobytes(), self.page_no))
        del self.undo_stack[:-UNDO_LIMIT]
        self.redo_stack.clear()
        self.modified = True

    def _restore(self, src, dst):
        if not src:
            return
        dst.append((self.doc.tobytes(), self.page_no))
        data, self.page_no = src.pop()
        self.doc = pymupdf.open("pdf", data)
        self.modified = True
        self.refresh()

    def undo(self):
        self._restore(self.undo_stack, self.redo_stack)

    def redo(self):
        self._restore(self.redo_stack, self.undo_stack)

    # ---------- файлы ----------

    def maybe_save(self):
        if not self.doc or not self.modified:
            return True
        r = QMessageBox.question(self, APP, "Сохранить изменения в документе?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return self.save()
        return r == QMessageBox.Discard

    def last_dir(self):
        if self.path:
            return str(self.path.parent)
        return self.settings.value("last_dir", str(Path.home()))

    def open_dialog(self):
        f, _ = QFileDialog.getOpenFileName(self, "Открыть", self.last_dir(), OPEN_FILTER)
        if f:
            self.open_file(f)

    def open_file(self, f):
        if not self.maybe_save():
            return
        try:
            doc = open_as_pdf(f)
        except Exception as ex:
            QMessageBox.critical(self, APP, f"Не удалось открыть файл:\n{ex}")
            return
        if not self.unlock(doc, f):
            return
        is_pdf = Path(f).suffix.lower() == ".pdf"
        self.set_doc(doc, Path(f) if is_pdf else None, modified=not is_pdf)
        self.settings.setValue("last_dir", str(Path(f).parent))
        if is_pdf:
            recent = [Path(f).resolve().as_posix()] + [
                r for r in self.recent() if r != Path(f).resolve().as_posix()]
            self.settings.setValue("recent", recent[:10])

    def unlock(self, doc, name):
        while doc.needs_pass:
            pw, ok = QInputDialog.getText(self, APP, f"Пароль для «{Path(name).name}»:",
                                          QLineEdit.Password)
            if not ok:
                return False
            if doc.authenticate(pw):
                break
            QMessageBox.warning(self, APP, "Неверный пароль")
        return True

    def set_doc(self, doc, path, modified=False):
        self.doc = doc
        self.path = path
        self.page_no = 0
        self.modified = modified
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.render_thumbs()
        self.fit_width() if len(doc) else self.render_page()
        self.update_ui()

    def recent(self):
        r = self.settings.value("recent", [])
        return [r] if isinstance(r, str) else list(r or [])

    def fill_recent(self):
        self.recent_menu.clear()
        items = [r for r in self.recent() if os.path.exists(r)]
        for r in items:
            self.recent_menu.addAction(r, lambda r=r: self.open_file(r))
        if not items:
            self.recent_menu.addAction("(пусто)").setEnabled(False)

    def close_doc(self):
        if self.maybe_save():
            self.doc = None
            self.path = None
            self.modified = False
            self.undo_stack.clear()
            self.redo_stack.clear()
            self.thumbs.clear()
            self.view.set_pixmap(None)
            self.update_ui()

    def save(self):
        if not self.doc:
            return False
        if not self.path:
            return self.save_as()
        return self.write(self.path)

    def save_as(self):
        if not self.doc:
            return False
        start = str(self.path) if self.path else str(Path(self.last_dir()) / "документ.pdf")
        f, _ = QFileDialog.getSaveFileName(self, "Сохранить как", start, PDF_FILTER)
        if not f:
            return False
        if not f.lower().endswith(".pdf"):
            f += ".pdf"
        if self.write(Path(f)):
            self.path = Path(f)
            self.update_ui()
            return True
        return False

    def write(self, path, doc=None):
        doc = doc or self.doc
        tmp = path.with_name(path.name + ".tmp")
        try:
            # шрифты урезаем в копии, чтобы дальнейшая правка не теряла символы
            copy = pymupdf.open("pdf", doc.tobytes())
            try:
                copy.subset_fonts()
            except Exception:
                pass
            copy.save(tmp, garbage=3, deflate=True)
            copy.close()
            os.replace(tmp, path)
        except Exception as ex:
            tmp.unlink(missing_ok=True)
            QMessageBox.critical(self, APP, f"Не удалось сохранить:\n{ex}")
            return False
        if doc is self.doc:
            self.modified = False
            self.update_ui()
            self.statusBar().showMessage(f"Сохранено: {path}", 4000)
        return True

    def pick_sources(self, title):
        files, _ = QFileDialog.getOpenFileNames(self, title, self.last_dir(), OPEN_FILTER)
        docs = []
        for f in files:
            try:
                d = open_as_pdf(f)
            except Exception as ex:
                QMessageBox.warning(self, APP, f"Пропущен «{Path(f).name}»:\n{ex}")
                continue
            if self.unlock(d, f):
                docs.append(d)
        return docs

    def new_from_files(self):
        if not self.maybe_save():
            return
        docs = self.pick_sources("Выберите PDF и картинки (порядок можно поменять потом)")
        if not docs:
            return
        doc = pymupdf.open()
        for d in docs:
            doc.insert_pdf(d)
        self.set_doc(doc, None, modified=True)

    # ---------- страницы ----------

    def selected_pages(self):
        rows = sorted(self.thumbs.row(it) for it in self.thumbs.selectedItems())
        return rows or ([self.page_no] if self.doc else [])

    def insert_files(self):
        docs = self.pick_sources("Вставить PDF или картинки")
        if not docs:
            return
        if not self.doc:
            doc = pymupdf.open()
            for d in docs:
                doc.insert_pdf(d)
            self.set_doc(doc, None, modified=True)
            return
        where = ["В конец документа", "После текущей страницы", "Перед текущей страницей",
                 "В начало документа"]
        choice, ok = QInputDialog.getItem(self, "Куда вставить", "Позиция:", where, 0, False)
        if not ok:
            return
        pos = {0: len(self.doc), 1: self.page_no + 1, 2: self.page_no, 3: 0}[where.index(choice)]
        self.snapshot()
        first = pos
        for d in docs:
            n = len(d)
            self.doc.insert_pdf(d, start_at=pos)
            pos += n
        self.page_no = first
        self.refresh()
        self.statusBar().showMessage(f"Добавлено страниц: {pos - first}", 4000)

    def insert_blank(self):
        r = self.doc[self.page_no].rect if len(self.doc) else pymupdf.paper_rect("a4")
        self.snapshot()
        self.doc.new_page(self.page_no + 1, width=r.width, height=r.height)
        self.page_no += 1
        self.refresh()

    def rotate(self, deg):
        self.snapshot()
        for i in self.selected_pages():
            p = self.doc[i]
            p.set_rotation((p.rotation + deg) % 360)
        sel = self.selected_pages()
        self.refresh()
        self.reselect(sel)

    def reselect(self, rows):
        for r in rows:
            if 0 <= r < self.thumbs.count():
                self.thumbs.item(r).setSelected(True)

    def delete_pages(self):
        sel = self.selected_pages()
        if len(sel) == len(self.doc):
            QMessageBox.information(self, APP, "Нельзя удалить все страницы документа.")
            return
        if len(sel) > 1 and QMessageBox.question(
                self, APP, f"Удалить страниц: {len(sel)}?") != QMessageBox.Yes:
            return
        self.snapshot()
        self.doc.delete_pages(sel)
        self.page_no = min(sel[0], len(self.doc) - 1)
        self.refresh()

    def move_pages(self, d):
        sel = self.selected_pages()
        n = len(self.doc)
        if (d < 0 and sel[0] == 0) or (d > 0 and sel[-1] == n - 1):
            return
        order = list(range(n))
        for i in (sel if d < 0 else reversed(sel)):
            order[i], order[i + d] = order[i + d], order[i]
        self.apply_order(order)
        self.reselect([i + d for i in sel])

    def on_reordered(self):
        order = [self.thumbs.item(i).data(Qt.UserRole) for i in range(self.thumbs.count())]
        if order != list(range(len(order))):
            cur = self.thumbs.currentRow()
            self.apply_order(order, cur)

    def apply_order(self, order, cur=None):
        self.snapshot()
        cur_old = self.page_no
        self.doc.select(order)
        self.page_no = order.index(cur_old) if cur is None else max(cur, 0)
        self.refresh()

    def extract(self):
        sel = self.selected_pages()
        stem = self.path.stem if self.path else "документ"
        start = str(Path(self.last_dir()) / f"{stem}_стр_{fmt_pages(sel)}.pdf")
        f, _ = QFileDialog.getSaveFileName(self, "Сохранить выделенные страницы", start, PDF_FILTER)
        if not f:
            return
        out = pymupdf.open()
        for i in sel:
            out.insert_pdf(self.doc, from_page=i, to_page=i)
        if self.write(Path(f), out):
            self.statusBar().showMessage(f"Сохранено страниц: {len(sel)} → {f}", 5000)

    def split(self):
        n = len(self.doc)
        dlg = SplitDialog(self, n, self.last_dir())
        if not dlg.exec():
            return
        if dlg.r_each.isChecked():
            groups = [[i] for i in range(n)]
        elif dlg.r_n.isChecked():
            k = dlg.n.value()
            groups = [list(range(i, min(i + k, n))) for i in range(0, n, k)]
        else:
            try:
                groups = parse_ranges(dlg.rng.text(), n)
            except ValueError as ex:
                QMessageBox.warning(self, APP, str(ex))
                return
        folder = dlg.folder.path()
        folder.mkdir(parents=True, exist_ok=True)
        stem = self.path.stem if self.path else "документ"
        done = []
        for g in groups:
            out = pymupdf.open()
            for i in g:
                out.insert_pdf(self.doc, from_page=i, to_page=i)
            target = folder / f"{stem}_стр_{fmt_pages(g)}.pdf"
            if not self.write(target, out):
                return
            done.append(target)
        QMessageBox.information(self, APP, f"Создано файлов: {len(done)}\nПапка: {folder}")

    def export_images(self):
        n = len(self.doc)
        has_sel = len(self.thumbs.selectedItems()) > 0
        folder = self.settings.value("export_dir", self.last_dir())
        dlg = ExportDialog(self, n, has_sel, folder)
        if not dlg.exec():
            return
        if dlg.r_all.isChecked():
            pages = list(range(n))
        elif dlg.r_sel.isChecked():
            pages = self.selected_pages()
        else:
            try:
                pages = [i for g in parse_ranges(dlg.rng.text(), n) for i in g]
            except ValueError as ex:
                QMessageBox.warning(self, APP, str(ex))
                return
        folder = dlg.folder.path()
        folder.mkdir(parents=True, exist_ok=True)
        self.settings.setValue("export_dir", str(folder))
        ext = dlg.fmt.currentText().lower()
        dpi = dlg.dpi.value()
        q = dlg.quality.value()
        stem = self.path.stem if self.path else "страница"
        width = len(str(n))
        prog = QProgressDialog("Сохранение изображений…", "Отмена", 0, len(pages), self)
        prog.setWindowModality(Qt.WindowModal)
        prog.setMinimumDuration(300)
        saved = 0
        try:
            for k, i in enumerate(pages):
                prog.setValue(k)
                QApplication.processEvents()
                if prog.wasCanceled():
                    break
                pix = self.doc[i].get_pixmap(dpi=dpi, alpha=False)
                target = folder / f"{stem}_{i + 1:0{width}d}.{ext}"
                if ext == "jpg":
                    pix.save(str(target), jpg_quality=q)
                else:
                    pix.save(str(target))
                saved += 1
        except Exception as ex:
            QMessageBox.critical(self, APP, f"Ошибка экспорта:\n{ex}")
        prog.setValue(len(pages))
        if saved:
            QMessageBox.information(self, APP, f"Сохранено изображений: {saved}\nПапка: {folder}")

    # ---------- правка содержимого ----------

    def text_kwargs(self, size):
        kw = {"fontsize": size, "color": color_tuple(self.color)}
        if self.font_file:
            kw.update(fontname="UFont", fontfile=self.font_file)
        return kw

    def add_text(self, pos):
        text, ok = QInputDialog.getMultiLineText(self, "Добавить текст", "Текст:")
        if not ok or not text.strip():
            return
        size = self.font_size.value()
        page = self.doc[self.page_no]
        # точка щелчка — верх строки; PDF ждёт базовую линию
        base = QPointF(pos.x(), pos.y() + size * 0.85 * self.scale)
        self.snapshot()
        page.insert_text(self.to_pdf(base), text, rotate=page.rotation, **self.text_kwargs(size))
        self.refresh(full=False)

    def edit_text(self, pos):
        page = self.doc[self.page_no]
        pt = self.to_pdf(pos)
        line = None
        for b in page.get_text("dict")["blocks"]:
            for ln in b.get("lines", []):
                if pymupdf.Rect(ln["bbox"]).contains(pt) and ln["spans"]:
                    line = ln
        if not line:
            self.statusBar().showMessage("Здесь нет текста. Для отсканированных страниц используйте «Стереть» + «Текст».", 5000)
            return
        spans = line["spans"]
        old = "".join(s["text"] for s in spans)
        new, ok = QInputDialog.getText(self, "Править текст", "Текст строки:", text=old)
        if not ok or new == old:
            return
        first = spans[0]
        size = round(first["size"], 1)
        r = pymupdf.Rect(line["bbox"])
        h = r.height
        r.y0 += h * 0.15
        r.y1 -= h * 0.15
        self.snapshot()
        page.add_redact_annot(r, fill=False)
        kw = {"images": pymupdf.PDF_REDACT_IMAGE_NONE}
        if hasattr(pymupdf, "PDF_REDACT_LINE_ART_NONE"):
            kw["graphics"] = pymupdf.PDF_REDACT_LINE_ART_NONE
        page.apply_redactions(**kw)
        if new.strip():
            kwargs = self.text_kwargs(size)
            kwargs["color"] = pymupdf.sRGB_to_pdf(first["color"])
            # направление строки (повёрнутый текст), кратно 90°
            dx, dy = line.get("dir", (1, 0))
            angle = round(math.degrees(math.atan2(-dy, dx)) / 90) * 90 % 360
            page.insert_text(pymupdf.Point(first["origin"]), new, rotate=angle, **kwargs)
        self.refresh(full=False)

    def apply_rect(self, qr):
        page = self.doc[self.page_no]
        r = self.rect_to_pdf(qr)
        tool = self.tool
        if tool == "image":
            f, _ = QFileDialog.getOpenFileName(self, "Картинка", self.last_dir(), IMG_FILTER)
            if not f:
                return
            self.snapshot()
            try:
                page.insert_image(r, filename=f, rotate=page.rotation)
            except Exception as ex:
                self.undo_stack.pop()
                QMessageBox.warning(self, APP, f"Не удалось вставить картинку:\n{ex}")
                return
        elif tool == "erase":
            self.snapshot()
            page.add_redact_annot(r, fill=(1, 1, 1))
            img = getattr(pymupdf, "PDF_REDACT_IMAGE_PIXELS", pymupdf.PDF_REDACT_IMAGE_REMOVE)
            page.apply_redactions(images=img)
        elif tool == "highlight":
            self.snapshot()
            a = page.add_highlight_annot(r)
            a.set_colors(stroke=color_tuple(self.color) if self.color != QColor("#000000")
                         else (1, 0.9, 0))
            a.update()
        elif tool == "box":
            self.snapshot()
            a = page.add_rect_annot(r)
            a.set_colors(stroke=color_tuple(self.color))
            a.set_border(width=self.line_width)
            a.update()
        self.refresh(full=False)

    def add_ink(self, pts):
        page = self.doc[self.page_no]
        self.snapshot()
        a = page.add_ink_annot([[tuple(self.to_pdf(p)) for p in pts]])
        a.set_colors(stroke=color_tuple(self.color))
        a.set_border(width=self.line_width)
        a.update()
        self.refresh(full=False)

    # ---------- окно ----------

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        files = [f for f in files if Path(f).suffix.lower() in IMG_EXT | {".pdf"}]
        if not files:
            return
        if not self.doc and len(files) == 1:
            self.open_file(files[0])
            return
        if self.doc:
            box = QMessageBox(QMessageBox.Question, APP, "Что сделать с файлами?", parent=self)
            b_ins = box.addButton("Добавить в конец документа", QMessageBox.AcceptRole)
            b_open = box.addButton("Открыть" if len(files) == 1 else "Собрать новый PDF",
                                   QMessageBox.AcceptRole)
            box.addButton(QMessageBox.Cancel)
            box.exec()
            if box.clickedButton() is b_open:
                if len(files) == 1:
                    self.open_file(files[0])
                    return
                if not self.maybe_save():
                    return
                self.doc = None
            elif box.clickedButton() is not b_ins:
                return
        docs = []
        for f in files:
            try:
                d = open_as_pdf(f)
            except Exception as ex:
                QMessageBox.warning(self, APP, f"Пропущен «{Path(f).name}»:\n{ex}")
                continue
            if self.unlock(d, f):
                docs.append(d)
        if not docs:
            return
        if self.doc:
            self.snapshot()
            first = len(self.doc)
            for d in docs:
                self.doc.insert_pdf(d)
            self.page_no = first
            self.refresh()
        else:
            doc = pymupdf.open()
            for d in docs:
                doc.insert_pdf(d)
            self.set_doc(doc, None, modified=True)

    def closeEvent(self, e):
        if not self.maybe_save():
            e.ignore()
            return
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("font_size", self.font_size.value())
        e.accept()


def fmt_pages(pages):
    """[0,1,2,4] -> '1-3_5' для имён файлов."""
    parts = []
    i = 0
    while i < len(pages):
        j = i
        while j + 1 < len(pages) and pages[j + 1] == pages[j] + 1:
            j += 1
        parts.append(f"{pages[i] + 1}" if i == j else f"{pages[i] + 1}-{pages[j] + 1}")
        i = j + 1
    s = "_".join(parts)
    return s if len(s) <= 40 else s[:40] + "…"


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP)
    app.setStyle(QStyleFactory.create("Fusion"))
    w = MainWindow()
    w.show()
    if len(sys.argv) > 1:
        w.open_file(sys.argv[1])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
