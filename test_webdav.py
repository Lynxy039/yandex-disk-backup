"""Интеграция с настоящим rclone и локальным WebDAV: файл доступен, подпапка зависает."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from xml.sax.saxutils import escape

import download


def response(path, directory=False):
    kind = "<d:collection/>" if directory else ""
    return (f"<d:response><d:href>{escape(path)}</d:href><d:propstat><d:prop>"
            f"<d:resourcetype>{kind}</d:resourcetype><d:getcontentlength>{0 if directory else 3}</d:getcontentlength>"
            "<d:getlastmodified>Sat, 24 Oct 2026 12:00:00 GMT</d:getlastmodified>"
            "<d:getetag>\"test\"</d:getetag></d:prop>"
            "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")


class WebDAVTests(unittest.TestCase):
    def test_real_rclone_downloads_file_while_subdirectory_listing_waits(self):
        downloaded = threading.Event()
        downloaded_before_listing_failure = []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, body=b"", kind="application/xml"):
                self.send_response(code)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            def do_PROPFIND(self):
                self.rfile.read(int(self.headers.get("Content-Length", 0)))
                path = self.path.rstrip("/")
                if path == "/slow":
                    downloaded_before_listing_failure.append(downloaded.wait(timeout=3))
                    self.reply(503)
                    return
                records = response("/first.jpg") if path == "/first.jpg" else (
                    response("/", True) + response("/first.jpg") + response("/slow/", True))
                xml = ('<?xml version="1.0"?><d:multistatus xmlns:d="DAV:">' + records + '</d:multistatus>')
                self.reply(207, xml.encode())

            def do_GET(self):
                if self.path.rstrip("/") == "/first.jpg":
                    self.reply(200, b"abc", "application/octet-stream")
                    downloaded.set()
                else:
                    self.reply(404)

            def do_HEAD(self):
                self.reply(200, b"abc", "application/octet-stream")

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                env = {key: value for key, value in os.environ.items() if not key.startswith("RCLONE_")}
                env.update(RCLONE_WEBDAV_URL=f"http://127.0.0.1:{server.server_port}/", RCLONE_WEBDAV_VENDOR="other")
                with patch.object(download, "remote_source", return_value=":webdav:"):
                    result = download.run_job({"source": "/", "destination": str(root / "backup")},
                                              root / "run", {"listing_timeout": 5, "retry_passes": 0}, env)
                self.assertEqual((root / "backup/first.jpg").read_bytes(), b"abc")
                self.assertTrue(downloaded_before_listing_failure)
                self.assertTrue(all(downloaded_before_listing_failure))
                self.assertFalse(result["manifest_complete"])
                self.assertTrue(result["known_files_verified"])
                self.assertEqual(result["status"], "failed")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
