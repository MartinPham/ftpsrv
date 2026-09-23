#!/usr/bin/env python3
"""Local file-transfer regression checks; uses only the Python standard library."""

import ftplib
import io
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import unittest


class Transfers(unittest.TestCase):
    binary = None

    @classmethod
    def setUpClass(cls):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            cls.port = probe.getsockname()[1]
        cls.log = tempfile.TemporaryFile()
        cls.addClassCleanup(cls.log.close)
        cls.server = subprocess.Popen(
            [cls.binary, "-p", str(cls.port)], stdout=cls.log, stderr=cls.log
        )
        cls.addClassCleanup(cls.stop_server)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            if cls.server.poll() is not None:
                cls.log.seek(0)
                raise RuntimeError(cls.log.read().decode(errors="replace"))
            try:
                with cls.connect():
                    return
            except OSError:
                time.sleep(0.05)
        raise TimeoutError("Local FTP server did not start")

    @classmethod
    def stop_server(cls):
        if cls.server.poll() is None:
            cls.server.terminate()
            try:
                cls.server.wait(timeout=5)
            except subprocess.TimeoutExpired:
                cls.server.kill()
                cls.server.wait(timeout=5)

    @classmethod
    def connect(cls):
        ftp = ftplib.FTP(timeout=5)
        try:
            ftp.connect("127.0.0.1", cls.port)
            ftp.login()
            return ftp
        except BaseException:
            ftp.close()
            raise

    def setUp(self):
        temp = tempfile.TemporaryDirectory(prefix="ftpsrv-check-")
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.ftp = self.connect()
        self.addCleanup(self.ftp.close)
        self.ftp.cwd(str(self.root))

    def download(self, name, **kwargs):
        out = io.BytesIO()
        self.ftp.retrbinary("RETR " + name, out.write, **kwargs)
        return out.getvalue()

    def test_binary_roundtrip_and_buffer_reuse(self):
        # Listing allocates the small buffer, file transfer grows it, and
        # subsequent commands must safely reuse the resulting storage.
        self.assertEqual(set(self.ftp.nlst()) - {".", ".."}, set())
        payload = bytes(range(256)) * 9000
        for passive in (True, False):
            with self.subTest(passive=passive):
                self.ftp.set_pasv(passive)
                self.ftp.storbinary("STOR data.bin", io.BytesIO(payload))
                self.assertEqual(self.download("data.bin"), payload)
                self.assertIn("data.bin", self.ftp.nlst())
                rows = []
                self.ftp.retrlines("LIST", rows.append)
                self.assertTrue(any("data.bin" in row for row in rows))
                self.assertIn("data.bin", dict(self.ftp.mlsd()))

    def test_store_replaces_and_truncates(self):
        target = self.root / "data.bin"
        target.write_bytes(b"old contents" * 100)
        self.ftp.storbinary("STOR data.bin", io.BytesIO(b"new"))
        self.assertEqual(target.read_bytes(), b"new")
        self.ftp.storbinary("STOR data.bin", io.BytesIO())
        self.assertEqual(target.read_bytes(), b"")

    def test_resume_upload_and_download(self):
        target = self.root / "data.bin"
        target.write_bytes(b"0123456789")
        self.ftp.storbinary("STOR data.bin", io.BytesIO(b"abc"), rest=5)
        self.assertEqual(target.read_bytes(), b"01234abc")
        self.assertEqual(self.download("data.bin", rest=5), b"abc")

    def test_append_creates_and_preserves_contents(self):
        target = self.root / "append.bin"
        self.ftp.storbinary("APPE append.bin", io.BytesIO(b"first"))
        self.ftp.storbinary("APPE append.bin", io.BytesIO())
        self.ftp.storbinary("APPE append.bin", io.BytesIO(b"second"))
        self.assertEqual(target.read_bytes(), b"firstsecond")

    def test_append_ignores_restart_marker(self):
        target = self.root / "append.bin"
        target.write_bytes(b"original")
        self.ftp.storbinary("APPE append.bin", io.BytesIO(b"tail"), rest=2)
        self.assertEqual(target.read_bytes(), b"originaltail")
        self.assertEqual(self.download("append.bin"), b"originaltail")

    def test_overlapping_append_sessions(self):
        for initial in (b"", b"prefix"):
            with self.subTest(initial=initial):
                target = self.root / "shared.bin"
                target.write_bytes(initial)
                with self.connect() as other:
                    other.cwd(str(self.root))
                    self.ftp.voidcmd("TYPE I")
                    other.voidcmd("TYPE I")
                    # Both commands open the file before either sends data.
                    with self.ftp.transfercmd("APPE shared.bin") as first:
                        with other.transfercmd("APPE shared.bin") as second:
                            first.sendall(b"A" * 4096)
                            first.shutdown(socket.SHUT_WR)
                            self.ftp.voidresp()
                            second.sendall(b"B" * 4096)
                            second.shutdown(socket.SHUT_WR)
                            other.voidresp()
                self.assertEqual(target.read_bytes(), initial + b"A" * 4096 + b"B" * 4096)

    def test_failed_data_connection_preserves_file(self):
        target = self.root / "keep.bin"
        target.write_bytes(b"keep these contents")
        # Reserve an unlistening local port so connect fails immediately.
        with socket.socket() as unused:
            unused.bind(("127.0.0.1", 0))
            self.ftp.sendport("127.0.0.1", unused.getsockname()[1])
            self.ftp.putcmd("STOR keep.bin")
            self.assertTrue(self.ftp.getresp().startswith("150"))
            with self.assertRaises(ftplib.error_perm):
                self.ftp.getresp()
        self.assertEqual(target.read_bytes(), b"keep these contents")
        self.assertTrue(self.ftp.voidcmd("NOOP").startswith("200"))

    def test_symlink_upload_is_rejected(self):
        target = self.root / "keep.bin"
        target.write_bytes(b"keep")
        (self.root / "link.bin").symlink_to(target)
        for command in ("STOR", "APPE"):
            with self.subTest(command=command):
                with self.assertRaises(ftplib.error_perm):
                    self.ftp.storbinary(command + " link.bin", io.BytesIO(b"new"))
                self.assertEqual(target.read_bytes(), b"keep")


if __name__ == "__main__":
    Transfers.binary = str(Path(sys.argv.pop(1)).resolve())
    unittest.main(verbosity=2)
