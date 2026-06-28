"""Startup window shown at launch: pick a recent project, or start/open another.

After exec(), ``choice`` is one of:
  ("recent", path)  — open this recent project
  ("new",)          — start a new project (the project-settings dialog)
  ("open",)         — open some other project file (file dialog)
  None              — the window was closed without choosing
"""

from __future__ import annotations

import os

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
)


class StartupDialog(QDialog):
    def __init__(self, recent_paths: list[str], parent=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Phidler — Open a project")
        self.setMinimumWidth(460)
        self.choice: tuple | None = None

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>Recent projects</b>"))

        self.list = QListWidget()
        for path in recent_paths:
            item = QListWidgetItem(f"{os.path.basename(path)}\n{path}")
            item.setData(Qt.UserRole, path)
            self.list.addItem(item)
        if not recent_paths:
            placeholder = QListWidgetItem("No recent projects yet — start a new one or open a file.")
            placeholder.setFlags(Qt.NoItemFlags)
            self.list.addItem(placeholder)
        self.list.itemDoubleClicked.connect(self._open_item)
        self.list.itemSelectionChanged.connect(self._update_open_enabled)
        layout.addWidget(self.list)

        buttons = QHBoxLayout()
        self.open_button = QPushButton("Open Selected")
        self.open_button.setEnabled(False)
        self.open_button.clicked.connect(self._open_selected)
        new_button = QPushButton("New Project…")
        new_button.clicked.connect(lambda: self._finish(("new",)))
        open_other_button = QPushButton("Open Other…")
        open_other_button.clicked.connect(lambda: self._finish(("open",)))
        buttons.addWidget(self.open_button)
        buttons.addStretch(1)
        buttons.addWidget(open_other_button)
        buttons.addWidget(new_button)
        layout.addLayout(buttons)

    def _selected_path(self) -> str | None:
        item = self.list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def _update_open_enabled(self) -> None:
        self.open_button.setEnabled(self._selected_path() is not None)

    def _open_item(self, item: QListWidgetItem) -> None:
        path = item.data(Qt.UserRole)
        if path:
            self._finish(("recent", path))

    def _open_selected(self) -> None:
        path = self._selected_path()
        if path:
            self._finish(("recent", path))

    def _finish(self, choice: tuple) -> None:
        self.choice = choice
        self.accept()
