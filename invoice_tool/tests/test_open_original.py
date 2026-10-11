import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from invoice_tool.core import InvoiceRecord
from invoice_tool.gui import InvoiceApp


class OpenOriginalTests(unittest.TestCase):
    def setUp(self):
        self.app = InvoiceApp.__new__(InvoiceApp)
        self.app.root = Mock()
        self.app.selected = 0
        self.app.rows = [InvoiceRecord(source='粘贴文字')]

    def test_supported_files_are_passed_literally_to_default_viewer(self):
        with tempfile.TemporaryDirectory() as folder:
            for extension in ('.PDF', '.jpg', '.png', '.tiff', '.txt'):
                with self.subTest(extension=extension):
                    path = Path(folder)/('发票 原件 & (1)'+extension)
                    path.write_bytes(b'synthetic fixture')
                    self.app.rows[0].source = str(path)
                    before = self.app.rows[0].__dict__.copy()
                    self.app.dirty = True
                    self.app.amount = Mock()
                    with patch('invoice_tool.gui.os.startfile') as launch:
                        self.app.open_original()
                    launch.assert_called_once_with(str(path))
                    self.assertEqual(self.app.rows[0].__dict__, before)
                    self.assertTrue(self.app.dirty)
                    self.app.amount.assert_not_called()
                    self.assertEqual(self.app.amount.mock_calls, [])

    def test_no_selection_does_not_launch(self):
        self.app.selected = None
        with patch('invoice_tool.gui.os.startfile') as launch:
            self.app.open_original()
        launch.assert_not_called()

    def test_pasted_text_has_no_source_file(self):
        with patch('invoice_tool.gui.os.startfile') as launch, \
                patch('invoice_tool.gui.messagebox.showinfo') as notice:
            self.app.open_original()
        launch.assert_not_called()
        notice.assert_called_once()

    def test_missing_source_reports_problem_without_launching(self):
        with tempfile.TemporaryDirectory() as folder:
            self.app.rows[0].source = str(Path(folder)/'已移动.pdf')
            with patch('invoice_tool.gui.os.startfile') as launch, \
                    patch('invoice_tool.gui.messagebox.showerror') as notice:
                self.app.open_original()
            launch.assert_not_called()
            notice.assert_called_once()

    def test_unsupported_existing_file_is_not_opened(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'not-an-invoice.exe'
            path.write_bytes(b'synthetic fixture')
            self.app.rows[0].source = str(path)
            with patch('invoice_tool.gui.os.startfile') as launch, \
                    patch('invoice_tool.gui.messagebox.showinfo') as notice:
                self.app.open_original()
            launch.assert_not_called()
            notice.assert_called_once()

    def test_viewer_failure_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'原件.pdf'
            path.write_bytes(b'synthetic fixture')
            self.app.rows[0].source = str(path)
            with patch('invoice_tool.gui.os.startfile', side_effect=OSError('no association')), \
                    patch('invoice_tool.gui.messagebox.showerror') as notice:
                self.app.open_original()
            notice.assert_called_once()
            self.assertIn('no association', notice.call_args.args[1])
