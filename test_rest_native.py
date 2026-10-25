"""Настоящий rclone/yandex против локального HTTPS-стенда, без запросов к Яндексу."""
import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import ssl
import tempfile
import threading
import unittest

import httpx
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

import download
import transfers


class NativeRestTests(unittest.TestCase):
    def test_native_backend_lists_downloads_and_resumes_without_refresh_or_mutation(self):
        requests = []
        downloaded = threading.Event()
        downloaded_before_page = []
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "localhost")])
            now = datetime.datetime.now(datetime.timezone.utc)
            certificate = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                           .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(days=1))
                           .not_valid_after(now + datetime.timedelta(days=1)).sign(key, hashes.SHA256()))
            cert_path, key_path = root / "cert.pem", root / "key.pem"
            cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
            key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                                   serialization.NoEncryption()))
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            context.load_cert_chain(cert_path, key_path)

            class API(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def reply(self, code, data, kind="application/json"):
                    body = json.dumps(data).encode() if kind == "application/json" else data
                    self.send_response(code)
                    self.send_header("Content-Type", kind)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                def do_GET(self):
                    requests.append((self.command, self.path, self.headers.get("Authorization")))
                    parsed = urlsplit(self.path)
                    query = parse_qs(parsed.query)
                    if parsed.path == "/fixture":
                        self.reply(200, b"abc", "application/octet-stream")
                        downloaded.set()
                    elif parsed.path == "/v1/disk/resources/download":
                        self.reply(200, {"href": "https://downloader.disk.yandex.ru/fixture", "method": "GET", "templated": False})
                    elif parsed.path == "/v1/disk/resources":
                        path = query.get("path", [""])[0].rstrip("/")
                        file = {"name": "fixture.jpg", "path": "disk:/Фото/fixture.jpg", "type": "file", "size": 3,
                                "modified": "2026-01-01T00:00:00Z", "md5": "900150983cd24fb0d6963f7d28e17f72"}
                        if path.endswith("fixture.jpg"):
                            self.reply(200, file)
                        else:
                            offset = int(query.get("offset", [0])[0])
                            if path.endswith("/Пустая"):
                                items, total = [], 0
                            elif offset:
                                downloaded_before_page.append(downloaded.wait(timeout=3))
                                items, total = [{"path": "disk:/Фото/Пустая", "type": "dir"}], 2
                            else:
                                items, total = [file], 2
                            self.reply(200, {"name": "Фото", "path": "disk:/Фото", "type": "dir",
                                             "_embedded": {"items": items, "total": total,
                                                           "limit": int(query.get("limit", [1000])[0]), "offset": offset}})
                    elif parsed.path.rstrip("/") == "/v1/disk":
                        self.reply(200, {"total_space": 1000, "used_space": 3})
                    else:
                        self.reply(404, {"error": "DiskNotFoundError"})

                def reject_mutation(self):
                    requests.append((self.command, self.path, None))
                    self.reply(405, {"error": "ReadOnlyFixture"})

                do_POST = do_PUT = do_DELETE = do_PATCH = reject_mutation

            class Proxy(BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def do_CONNECT(self):
                    self.send_response(200)
                    self.end_headers()
                    with context.wrap_socket(self.connection, server_side=True) as connection:
                        API(connection, self.client_address, self.server)
                    self.close_connection = True

            server = ThreadingHTTPServer(("127.0.0.1", 0), Proxy)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                config = {"backend": "rest", "login": "fixture-user", "oauth_token": "test-polygon-token", "retry_passes": 0}
                env = download.rclone_environment(config)
                for key in ("http_proxy", "https_proxy", "all_proxy", "no_proxy", "ALL_PROXY"):
                    env.pop(key, None)
                proxy = f"http://127.0.0.1:{server.server_port}"
                env.update(HTTPS_PROXY=proxy, HTTP_PROXY=proxy, NO_PROXY="")
                original_flags = transfers.flags
                def fixture_flags(settings):
                    # Только тестовый self-signed HTTPS-стенд. В рабочем коде проверка сертификатов включена.
                    return [*original_flags(settings), "--no-check-certificate"]
                job = {"source": "/Фото", "destination": str(root / "backup")}
                original_client = httpx.AsyncClient
                def fixture_client(**kwargs):
                    # И постраничный HTTPX-обход, и rclone идут только через локальный стенд.
                    return original_client(proxy=proxy, verify=False, trust_env=False, **kwargs)
                with patch("transfers.flags", side_effect=fixture_flags), patch("httpx.AsyncClient", side_effect=fixture_client):
                    first = download.run_job(job, root / "run1", config, env)
                    before = len([request for request in requests if request[1] == "/fixture"])
                    second = download.run_job(job, root / "run2", config, env)
                self.assertEqual(first["status"], "ok", requests)
                self.assertEqual(first["downloaded"], 1)
                self.assertEqual((root / "backup/fixture.jpg").read_bytes(), b"abc")
                self.assertTrue((root / "backup/Пустая").is_dir())
                self.assertTrue(downloaded_before_page and all(downloaded_before_page))
                self.assertEqual(second["status"], "ok")
                self.assertEqual(second["present"], 1)
                self.assertEqual(second["cached_files"], 1)
                self.assertEqual(len([request for request in requests if request[1] == "/fixture"]), before)
                self.assertTrue(requests)
                self.assertTrue(all(method == "GET" for method, _, _ in requests))
                self.assertTrue(any(auth == "OAuth test-polygon-token" for _, path, auth in requests if path.startswith("/v1/")))
                for report in root.glob("run*/*.json*"):
                    self.assertNotIn("test-polygon-token", report.read_text())
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)
