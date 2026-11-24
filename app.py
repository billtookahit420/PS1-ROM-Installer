#!/usr/bin/env python3
"""Recomp ROM Installer - GUI (PySide6).

Point it at a folder of extracted recomp packages and at one or more
ROM source folders (or .zip sets). It lists every package that is
waiting on a player ROM, then fills in only exact hash matches -
never a filename guess. See installer.py for the matching rules and
README.md for the full story.

Worker discipline (family rule, learned the hard way): every QThread
is held in a set until it finishes - a worker reference is never
overwritten while running, and closing the window asks the core to
cancel, then drains the workers before exiting. A failed worker
always re-enables the button that started it (v1.0.0 left the Scan
button greyed forever after a failed scan).
"""

import sys
import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QFileDialog,
                               QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QMainWindow, QPlainTextEdit,
                               QProgressBar, QPushButton, QTableWidget,
                               QTableWidgetItem, QVBoxLayout, QWidget)

import installer

DARK = """
QWidget { background: #1d1f26; color: #e8e9ec; font-size: 13px; }
QLineEdit, QListWidget, QTableWidget, QPlainTextEdit {
    background: #262933; border: 1px solid #3a3f4d; border-radius: 4px; }
QPushButton { background: #343947; border: 1px solid #4a5064;
    border-radius: 4px; padding: 5px 12px; }
QPushButton:hover { background: #41465c; }
QPushButton:disabled { color: #7a7f8c; }
QHeaderView::section { background: #262933; color: #e8e9ec;
    border: none; padding: 4px; }
QProgressBar { background: #262933; border: 1px solid #3a3f4d;
    border-radius: 4px; text-align: center; }
QProgressBar::chunk { background: #7c5cd6; }
"""


class FuncWorker(QThread):
    done = Signal(object)
    failed = Signal(str)

    def __init__(self, fn):
        super().__init__()
        self._fn = fn

    def run(self):
        try:
            self.done.emit(self._fn())
        except Exception as e:  # noqa: BLE001
            self.failed.emit(str(e))


class Bridge(QObject):
    log = Signal(str)
    progress = Signal(str)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Recomp ROM Installer")
        self.setStyleSheet(DARK)
        self.resize(1120, 740)
        self._workers = set()
        self._cancel = threading.Event()
        self.units = []
        self.ignored = {}
        self.scan_notes = []
        self._scanned_root = None    # set when a scan completes
        self.index = None
        self.bridge = Bridge()
        self.bridge.log.connect(self._log)
        self.bridge.progress.connect(self._on_progress)

        pkg_row = QHBoxLayout()
        self.pkg_edit = QLineEdit()
        self.pkg_edit.setPlaceholderText("Folder of extracted recomp packages…")
        browse_pkg = QPushButton("Browse…")
        browse_pkg.clicked.connect(self._browse_pkg)
        self.scan_btn = QPushButton("Scan packages")
        self.scan_btn.clicked.connect(self._scan)
        pkg_row.addWidget(QLabel("Packages:"))
        pkg_row.addWidget(self.pkg_edit)
        pkg_row.addWidget(browse_pkg)
        pkg_row.addWidget(self.scan_btn)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(
            ["Package", "System", "Expects", "Status", "Replace"])
        self.table.setWordWrap(False)
        header = self.table.horizontalHeader()
        header.setStretchLastSection(False)
        mode = header.ResizeMode
        header.setSectionResizeMode(0, mode.Interactive)
        header.setSectionResizeMode(1, mode.ResizeToContents)
        header.setSectionResizeMode(2, mode.Interactive)
        header.setSectionResizeMode(3, mode.Stretch)
        header.setSectionResizeMode(4, mode.ResizeToContents)
        self.table.setColumnWidth(0, 240)
        self.table.setColumnWidth(2, 190)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)

        src_row = QHBoxLayout()
        self.src_list = QListWidget()
        add_src = QPushButton("Add source folder…")
        add_src.clicked.connect(self._add_src_folder)
        add_zip = QPushButton("Add source .zip…")
        add_zip.clicked.connect(self._add_src_zip)
        rm_src = QPushButton("Remove selected")
        rm_src.clicked.connect(
            lambda: [self.src_list.takeItem(i.row())
                     for i in self.src_list.selectedIndexes()])
        src_btns = QVBoxLayout()
        src_btns.addWidget(add_src)
        src_btns.addWidget(add_zip)
        src_btns.addWidget(rm_src)
        src_btns.addStretch()
        src_row.addWidget(self.src_list)
        src_row.addLayout(src_btns)

        action_row = QHBoxLayout()
        self.fill_btn = QPushButton("Find ROMs + install")
        self.fill_btn.clicked.connect(self._fill)
        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(lambda: self._cancel.set())
        self.export_btn = QPushButton("Export report…")
        self.export_btn.clicked.connect(self._export)
        action_row.addWidget(self.fill_btn)
        action_row.addWidget(self.cancel_btn)
        action_row.addStretch()
        action_row.addWidget(self.export_btn)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 0)
        self.progress_bar.setFormat("idle")
        self.log_view = QPlainTextEdit()
        self.log_view.setReadOnly(True)

        lay = QVBoxLayout()
        lay.addLayout(pkg_row)
        lay.addWidget(self.table)
        lay.addWidget(QLabel("ROM sources (folders and/or .zip sets):"))
        lay.addLayout(src_row)
        lay.addLayout(action_row)
        lay.addWidget(self.progress_bar)
        lay.addWidget(QLabel("Log"))
        lay.addWidget(self.log_view)
        w = QWidget()
        w.setLayout(lay)
        self.setCentralWidget(w)

    # ------------------------------------------------------------ util
    def _log(self, msg):
        self.log_view.appendPlainText(msg)

    def _on_progress(self, msg):
        self.progress_bar.setFormat(msg[:90])

    def _run_worker(self, fn, handler, on_fail=None):
        worker = FuncWorker(fn)
        self._workers.add(worker)
        worker.done.connect(handler)

        def _failed(msg):
            self._log(f"error: {msg}")
            if on_fail is not None:
                on_fail()

        worker.failed.connect(_failed)
        worker.finished.connect(lambda w=worker: self._workers.discard(w))
        worker.start()
        return worker

    def _cancelled(self):
        return self._cancel.is_set()

    # ------------------------------------------------------------ scan
    def _browse_pkg(self):
        d = QFileDialog.getExistingDirectory(self, "Choose packages folder")
        if d:
            self.pkg_edit.setText(d)
            self._scan()

    def _scan(self):
        root = self.pkg_edit.text().strip()
        if not root:
            self._log("Pick a packages folder first.")
            return
        self._cancel.clear()
        self.scan_btn.setEnabled(False)

        def work():
            return installer.scan_packages(
                root, cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m))

        def done(result):
            self.scan_btn.setEnabled(True)
            self._scanned_root = root
            self.units, self.ignored, self.scan_notes = result
            self._fill_table()
            for n in self.scan_notes:
                self._log(f"note: {n}")
            self._log(f"Scanned {root}: {len(self.units)} ROM-waiting "
                      f"package(s); ignored (not ROM-waiting): "
                      f"{self.ignored or 'none'}.")

        def fail():
            self.scan_btn.setEnabled(True)

        self._run_worker(work, done, on_fail=fail)

    def _fill_table(self):
        self.table.setRowCount(0)
        for u in self.units:
            r = self.table.rowCount()
            self.table.insertRow(r)
            title_item = QTableWidgetItem(u.title)
            title_item.setToolTip(str(u.folder))
            self.table.setItem(r, 0, title_item)
            self.table.setItem(r, 1, QTableWidgetItem(
                installer.SYSTEMS[u.system][0]))
            self.table.setItem(r, 2, QTableWidgetItem(u.expects_text()))
            status_item = QTableWidgetItem(f"{u.status}: {u.detail}")
            status_item.setToolTip(f"{u.status}: {u.detail}")
            self.table.setItem(r, 3, status_item)
            box = QCheckBox()
            box.setEnabled(u.status == "conflict")
            self.table.setCellWidget(r, 4, box)
        self.table.resizeRowsToContents()

    def _row_status(self, row, unit):
        item = self.table.item(row, 3)
        if item:
            item.setText(f"{unit.status}: {unit.detail}")
        box = self.table.cellWidget(row, 4)
        if box is not None:
            box.setEnabled(unit.status == "conflict")

    # ----------------------------------------------------------- fill
    def _add_src_folder(self):
        d = QFileDialog.getExistingDirectory(self, "Choose ROM source folder")
        if d:
            self.src_list.addItem(d)

    def _add_src_zip(self):
        f, _ = QFileDialog.getOpenFileName(
            self, "Choose ROM set .zip", "", "Zip archives (*.zip)")
        if f:
            self.src_list.addItem(f)

    def _empty_scan_hint(self):
        """When a scan found nothing, name the two causes that most
        often explain it: packages still zipped, or a folder of
        converter *-src projects (which are not packages)."""
        root = Path(self.pkg_edit.text().strip())
        try:
            names = [p.name for p in root.iterdir()]
        except OSError:
            return ""
        hints = []
        if any(n.lower().endswith(".zip") for n in names):
            hints.append("This folder holds .zip files - zipped "
                         "packages must be extracted first.")
        if any(n.endswith("-src") for n in names):
            hints.append("The *-src folders are converter project "
                         "folders, not installable packages.")
        return (" " + " ".join(hints)) if hints else ""

    def _fill(self):
        if not self.units:
            if self._scanned_root is not None:
                self._log("The scan found no ROM-waiting packages in "
                          f"{self._scanned_root} - nothing to fill."
                          + self._empty_scan_hint())
            else:
                self._log("Scan a packages folder first.")
            return
        sources = [self.src_list.item(i).text()
                   for i in range(self.src_list.count())]
        if not sources:
            self._log("Add at least one ROM source folder or .zip.")
            return
        replace_keys = set()
        for r, u in enumerate(self.units):
            box = self.table.cellWidget(r, 4)
            if box is not None and box.isChecked():
                replace_keys.add(u.key)
        self._cancel.clear()
        self.fill_btn.setEnabled(False)
        units = self.units

        def work():
            index = installer.run_install(
                units, sources, replace_keys=replace_keys,
                cancel=self._cancelled,
                progress=lambda m: self.bridge.progress.emit(m))
            return index

        def done(index):
            self.fill_btn.setEnabled(True)
            self.index = index
            for r, u in enumerate(units):
                self._row_status(r, u)
                self.bridge.log.emit(
                    f"{u.title} [{u.system}]: {u.status} - {u.detail}")
            for n in index.notes:
                self._log(f"source note: {n}")
            self._log("Done.")

        def fail():
            self.fill_btn.setEnabled(True)

        self._run_worker(work, done, on_fail=fail)

    # ---------------------------------------------------------- export
    def _export(self):
        if not self.units:
            self._log("Nothing to export yet - scan first.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Export report", "rom-installer-report.md",
            "Markdown (*.md);;Text (*.txt)")
        if not path:
            return
        notes = list(self.scan_notes)
        count = None
        if self.index is not None:
            notes += self.index.notes
            count = len(self.index.entries)
        Path(path).write_text(installer.make_report(
            self.units, self.ignored, notes, count))
        self._log(f"Report written to {path}")

    # ----------------------------------------------------------- close
    def closeEvent(self, event):
        self._cancel.set()
        for w in list(self._workers):
            w.wait(15000)
        event.accept()


def main():
    app = QApplication(sys.argv)
    app.setStyleSheet(DARK)
    win = MainWindow()
    win.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
