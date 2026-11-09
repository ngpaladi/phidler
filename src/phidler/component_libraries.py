"""Component library folders and the export dialog (see phidler.component_files).

Library folders are kept per user in QSettings, like the recent-projects list.
Every ``*.pcomp.json`` in them shows in the palette under "Library — <folder>"
at start-up.
"""
from __future__ import annotations

import os

import numpy as np
from PySide6.QtCore import QSettings
from PySide6.QtWidgets import (
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QSpinBox,
    QVBoxLayout,
)

from phidler import component_files as cf

_ORG, _APP = "phidler", "phidler"


def _settings(settings: QSettings | None) -> QSettings:
    return settings if settings is not None else QSettings(_ORG, _APP)


def library_dirs(settings: QSettings | None = None, existing_only: bool = True) -> list[str]:
    stored = _settings(settings).value(cf.SETTINGS_KEY, [])
    if isinstance(stored, str):  # QSettings collapses a 1-element list to a bare string
        stored = [stored]
    dirs = [str(d) for d in (stored or [])]
    return [d for d in dirs if os.path.isdir(d)] if existing_only else dirs


def set_library_dirs(dirs: list[str], settings: QSettings | None = None) -> None:
    _settings(settings).setValue(cf.SETTINGS_KEY, list(dict.fromkeys(dirs)))


class LibrariesDialog(QDialog):
    """Add or remove component library folders."""

    def __init__(self, parent=None, settings: QSettings | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Component Libraries")
        self._settings = settings
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.addItems(library_dirs(settings, existing_only=False))
        layout.addWidget(self.list)
        row = QHBoxLayout()
        add, remove = QPushButton("Add Folder…"), QPushButton("Remove")
        add.clicked.connect(self._add)
        remove.clicked.connect(self._remove)
        row.addWidget(add)
        row.addWidget(remove)
        row.addStretch(1)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _add(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Component library folder")
        if folder:
            self.list.addItem(folder)

    def _remove(self) -> None:
        for item in self.list.selectedItems():
            self.list.takeItem(self.list.row(item))

    def folders(self) -> list[str]:
        return [self.list.item(i).text() for i in range(self.list.count())]

    def accept(self) -> None:
        set_library_dirs(self.folders(), self._settings)
        super().accept()


class ExportComponentDialog(QDialog):
    """Name and describe the part, optionally add an S-parameter table, and
    write the selection as a component file."""

    def __init__(self, parent, document, inst_ids: list[int]) -> None:
        super().__init__(parent)
        self.setWindowTitle("Export Selection as Component")
        self.document, self.inst_ids = document, list(inst_ids)
        form = QFormLayout(self)
        first = document.instances[self.inst_ids[0]]
        default = first.component_spec if len(self.inst_ids) == 1 else "subcircuit"
        self.name_edit = QLineEdit(default)
        self.desc_edit = QLineEdit("")
        self.author_edit = QLineEdit(os.environ.get("USER", ""))
        self.library_edit = QLineEdit("")
        form.addRow("Name", self.name_edit)
        form.addRow("Description", self.desc_edit)
        form.addRow("Author", self.author_edit)
        form.addRow("Library", self.library_edit)
        self.sparams_check = QCheckBox("Add an S-parameter table (sampled at 0 V)")
        form.addRow(self.sparams_check)
        self.wl_from, self.wl_to = QDoubleSpinBox(), QDoubleSpinBox()
        for w, v in ((self.wl_from, 1.50), (self.wl_to, 1.60)):
            w.setRange(0.4, 5.0)
            w.setDecimals(4)
            w.setValue(v)
            w.setSuffix(" um")
        self.wl_points = QSpinBox()
        self.wl_points.setRange(2, 2001)
        self.wl_points.setValue(51)
        form.addRow("Wavelength from", self.wl_from)
        form.addRow("Wavelength to", self.wl_to)
        form.addRow("Points", self.wl_points)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._save)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def export_to(self, path: str) -> dict:
        wls = None
        if self.sparams_check.isChecked():
            wls = np.linspace(self.wl_from.value(), self.wl_to.value(), self.wl_points.value())
        return cf.export_selection(self.document, self.inst_ids, path, name=self.name_edit.text(),
                                   description=self.desc_edit.text(), author=self.author_edit.text(),
                                   library=self.library_edit.text(), sparams_wavelengths=wls)

    def _save(self) -> None:
        path, _ = QFileDialog.getSaveFileName(self, "Save component", self.name_edit.text() + cf.SUFFIX,
                                              "Component files (*.pcomp.json)")
        if not path:
            return
        try:
            self.export_to(path)
        except Exception as exc:
            QMessageBox.critical(self, "Export failed", f"{type(exc).__name__}: {exc}")
            return
        self.accept()
