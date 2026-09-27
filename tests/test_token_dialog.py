from pathlib import Path

from PySide6.QtWidgets import QApplication

from tesla_viewer.main_window import TokenDialog


def test_failure_log_scrolls_without_expanding_dialog(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    app = QApplication.instance() or QApplication([])
    dialog = TokenDialog(Path("TeslaCam_Decrypted"))
    dialog.show()
    original_size = dialog.size()
    for _ in range(150):
        dialog.append_log("event.json: Tesla가 키를 반환하지 않았습니다.")
    dialog.set_finished("완료: 0개 복호화, 99개 실패", 0, 99)
    app.processEvents()
    assert dialog.size() == original_size
    assert dialog.log.verticalScrollBar().maximum() > 0
    assert dialog.status_label.text() == "완료: 0개 복호화, 99개 실패"
    dialog.close()
