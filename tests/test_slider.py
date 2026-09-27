from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from t6_viewer.main_window import ClickSeekSlider


def test_clicking_timeline_groove_seeks_to_clicked_time(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    slider = ClickSeekSlider(Qt.Horizontal)
    slider.setRange(0, 60_000)
    slider.resize(400, 30)
    slider.show()
    clicked = []
    slider.clicked_value.connect(clicked.append)
    QTest.mouseClick(slider, Qt.LeftButton, pos=QPoint(300, 15))
    assert 40_000 < slider.value() < 50_000
    assert clicked == [slider.value()]
    slider.close()
    assert app is not None
