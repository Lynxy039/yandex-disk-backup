import hashlib
import io
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import bootstrap


class BootstrapTests(unittest.TestCase):
    def test_dependencies_install_once_then_again_when_requirements_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'requirements.txt').write_text('textual==8.2.8\n')
            (root / '.venv').mkdir()
            with patch('subprocess.check_call') as install, patch('subprocess.run', return_value=subprocess.CompletedProcess([], 0)):
                bootstrap.ensure_environment(root)
                first = install.call_count
                self.assertGreaterEqual(first, 1)
                bootstrap.ensure_environment(root)
                # Заглушка venv не создаёт exe: проверяем именно повторный pip install.
                pip_before = sum('pip' in call.args[0] for call in install.call_args_list)
                self.assertEqual(pip_before, 1)
                (root / 'requirements.txt').write_text('textual==8.2.8\nhttpx>=0.28\n')
                bootstrap.ensure_environment(root)
                self.assertEqual(sum('pip' in call.args[0] for call in install.call_args_list), 2)

    def test_portable_rclone_is_checksum_verified_and_only_exe_is_extracted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr('rclone-v1.75.1-windows-amd64/rclone.exe', b'fixture-binary')
                archive.writestr('../../settings.json', b'must-not-extract')
            payload = data.getvalue()
            manifest = (hashlib.sha256(payload).hexdigest() + '  rclone-v1.75.1-windows-amd64.zip\n').encode()
            with patch('bootstrap.download', side_effect=[manifest, payload]):
                executable = bootstrap.install_rclone(root, 'windows', 'amd64')
            self.assertEqual(executable.read_bytes(), b'fixture-binary')
            self.assertFalse((root / 'settings.json').exists())

    def test_recent_system_rclone_is_reused_without_download(self):
        with tempfile.TemporaryDirectory() as tmp, patch('bootstrap.shutil.which', return_value='rclone'), \
             patch('bootstrap.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout='rclone v1.75.1\n')), \
             patch('bootstrap.install_rclone') as install:
            self.assertEqual(bootstrap.ensure_rclone(Path(tmp)), Path('rclone'))
            install.assert_not_called()

    def test_old_rclone_uses_portable_install(self):
        with tempfile.TemporaryDirectory() as tmp, patch('bootstrap.shutil.which', return_value='rclone'), \
             patch('bootstrap.subprocess.run', return_value=subprocess.CompletedProcess([], 0, stdout='rclone v1.60.0\n')), \
             patch('bootstrap.sys.platform', 'win32'), patch('bootstrap.platform.machine', return_value='AMD64'), \
             patch('bootstrap.install_rclone', return_value=Path('portable/rclone.exe')) as install:
            self.assertEqual(bootstrap.ensure_rclone(Path(tmp)), Path('portable/rclone.exe'))
            install.assert_called_once_with(Path(tmp), 'windows', 'amd64')

    def test_bad_checksum_does_not_install_binary(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            manifest = ('0' * 64 + '  rclone-v1.75.1-windows-amd64.zip\n').encode()
            with patch('bootstrap.download', side_effect=[manifest, b'not-valid']):
                with self.assertRaisesRegex(RuntimeError, 'SHA256'):
                    bootstrap.install_rclone(root, 'windows', 'amd64')
            self.assertFalse((root / '.tools/rclone/rclone.exe').exists())
