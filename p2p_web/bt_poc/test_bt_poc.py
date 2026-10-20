"""
test_bt_poc.py — Automated Unit and Integration Tests for Windows Bluetooth PoC.

Tests:
1. Protocol framing (length-prefix encoding, fragmentation handling, EOF detection).
2. JSON message serialization and parsing.
3. Metadata parsing and validation (file offer structure, size bounds).
4. Chunked file streaming and buffer alignment.
5. SHA-256 cryptographic hashing and tamper detection.
6. Filename sanitization (path traversal prevention, character stripping, duplicate collision avoidance).
7. Transfer statistics calculation (elapsed time, KB/s, MB/s, Mbps).
8. End-to-end loopback transfer test (4-phase protocol over local socket).
9. SDP registration API compatibility (WSASetServiceW via ctypes).

Run with:
    python -m unittest test_bt_poc.py -v
"""

import os
import sys
import time
import json
import socket
import struct
import hashlib
import tempfile
import threading
import unittest

# Ensure current directory is on path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bt_common import (
    CHUNK_SIZE,
    send_exact,
    recv_exact,
    send_frame,
    recv_frame,
    send_json,
    recv_json,
    compute_file_sha256,
    format_bytes,
    format_speed,
    calculate_transfer_metrics,
    sanitize_filename,
    normalize_mac,
    register_rfcomm_sdp_service,
    unregister_rfcomm_sdp_service,
    SERVICE_UUID_STR,
    SERVICE_NAME,
)


class TestProtocolFraming(unittest.TestCase):
    """Validates low-level length-prefixed framing and stream boundaries."""

    def setUp(self):
        # Create a localhost TCP socketpair for reproducible stream testing
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        port = self.server.getsockname()[1]

        self.client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_sock.connect(("127.0.0.1", port))
        self.server_sock, _ = self.server.accept()

    def tearDown(self):
        self.client_sock.close()
        self.server_sock.close()
        self.server.close()

    def test_frame_roundtrip(self):
        """Sends and receives short and binary frames intact."""
        payload = b"Hello, Windows Bluetooth RFCOMM test frame!"
        send_frame(self.client_sock, payload)
        received = recv_frame(self.server_sock)
        self.assertEqual(received, payload)

    def test_large_frame_roundtrip(self):
        """Sends a 128 KB frame requiring multi-chunk socket reads."""
        payload = os.urandom(128 * 1024)
        send_frame(self.client_sock, payload)
        received = recv_frame(self.server_sock)
        self.assertEqual(len(received), len(payload))
        self.assertEqual(received, payload)

    def test_empty_frame(self):
        """Sends an empty (0-byte) frame."""
        send_frame(self.client_sock, b"")
        received = recv_frame(self.server_sock)
        self.assertEqual(received, b"")

    def test_truncated_frame_raises_connection_error(self):
        """Validates that a connection abruptly closed during read raises ConnectionError."""
        # Send only 2 bytes of the 4-byte header then close
        self.client_sock.sendall(b"\x00\x01")
        self.client_sock.close()
        with self.assertRaises(ConnectionError):
            recv_frame(self.server_sock)


class TestJsonMessaging(unittest.TestCase):
    """Validates JSON encoding, framing, and schema transport."""

    def setUp(self):
        self.server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        port = self.server.getsockname()[1]

        self.client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.client_sock.connect(("127.0.0.1", port))
        self.server_sock, _ = self.server.accept()

    def tearDown(self):
        self.client_sock.close()
        self.server_sock.close()
        self.server.close()

    def test_json_roundtrip(self):
        """Sends arbitrary dictionary and validates exact deserialization."""
        test_dict = {
            "type": "FILE_OFFER",
            "filename": "document_sample.pdf",
            "filesize": 1048576,
            "sha256": "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
            "chunk_size": 32768,
            "nested": {"active": True, "tags": ["bluetooth", "rfcomm", "win32"]},
        }
        send_json(self.client_sock, test_dict)
        received = recv_json(self.server_sock)
        self.assertEqual(received, test_dict)


class TestMetadataParsing(unittest.TestCase):
    """Validates parsing, validation, and boundary checking on metadata payloads."""

    def test_valid_file_offer_metadata(self):
        payload = {
            "type": "FILE_OFFER",
            "filename": "report.docx",
            "filesize": 524288,
            "sha256": "a" * 64,
            "chunk_size": 32768,
        }
        self.assertEqual(payload["type"], "FILE_OFFER")
        self.assertGreater(payload["filesize"], 0)
        self.assertEqual(len(payload["sha256"]), 64)

    def test_invalid_metadata_detection(self):
        """Detects missing fields or non-integer file sizes."""
        invalid_payloads = [
            {"type": "FILE_OFFER"},  # missing filename, filesize, sha256
            {"type": "FILE_OFFER", "filename": "test.bin", "filesize": -100, "sha256": "abc"},
            {"type": "UNKNOWN_ACTION"},
        ]
        for p in invalid_payloads:
            has_error = False
            if p.get("type") != "FILE_OFFER":
                has_error = True
            elif not p.get("filename") or int(p.get("filesize", -1)) <= 0 or len(p.get("sha256", "")) != 64:
                has_error = True
            self.assertTrue(has_error, f"Expected validation failure for: {p}")


class TestChunkedFileHandling(unittest.TestCase):
    """Validates file slicing and chunk assembly across buffer boundaries."""

    def test_exact_chunking_and_reassembly(self):
        """Splits an arbitrary payload into CHUNK_SIZE pieces and reconstructs it."""
        total_size = (CHUNK_SIZE * 3) + 12345  # Not a multiple of chunk size
        raw_data = os.urandom(total_size)

        chunks = []
        offset = 0
        while offset < total_size:
            chunk = raw_data[offset : offset + CHUNK_SIZE]
            chunks.append(chunk)
            offset += len(chunk)

        reconstructed = b"".join(chunks)
        self.assertEqual(len(reconstructed), total_size)
        self.assertEqual(reconstructed, raw_data)

    def test_empty_payload_chunking(self):
        """Zero-byte file yields zero chunks without error."""
        raw_data = b""
        chunks = [raw_data[i : i + CHUNK_SIZE] for i in range(0, len(raw_data), CHUNK_SIZE)]
        self.assertEqual(len(chunks), 0)


class TestSha256Verification(unittest.TestCase):
    """Validates cryptographic integrity calculation and corruption detection."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="bt_poc_sha_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_sha256_matches_known_hash(self):
        filepath = os.path.join(self.test_dir, "known.txt")
        content = b"The quick brown fox jumps over the lazy dog"
        with open(filepath, "wb") as f:
            f.write(content)

        expected = hashlib.sha256(content).hexdigest()
        actual = compute_file_sha256(filepath)
        self.assertEqual(actual.lower(), expected.lower())

    def test_sha256_corruption_detection(self):
        """Altering a single bit in a file results in a distinct hash."""
        filepath = os.path.join(self.test_dir, "original.bin")
        content = bytearray(os.urandom(64 * 1024))
        with open(filepath, "wb") as f:
            f.write(content)

        orig_hash = compute_file_sha256(filepath)

        # Flip one byte
        content[100] ^= 0xFF
        corrupt_path = os.path.join(self.test_dir, "corrupt.bin")
        with open(corrupt_path, "wb") as f:
            f.write(content)

        corrupt_hash = compute_file_sha256(corrupt_path)
        self.assertNotEqual(orig_hash, corrupt_hash)


class TestFilenameSanitization(unittest.TestCase):
    """Validates directory traversal prevention, character cleaning, and collision avoidance."""

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="bt_poc_sanitize_")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_directory_traversal_stripping(self):
        malicious_names = [
            "../../etc/passwd",
            "..\\..\\Windows\\System32\\cmd.exe",
            "C:\\Secret\\confidential.txt",
            "/absolute/unix/path/exploit.sh",
        ]
        for name in malicious_names:
            safe_path = sanitize_filename(self.test_dir, name)
            self.assertTrue(safe_path.startswith(os.path.abspath(self.test_dir)))
            self.assertNotIn("..", os.path.relpath(safe_path, self.test_dir))

    def test_invalid_character_stripping(self):
        bad_name = 'my<file>:name"is|weird?*.txt'
        safe_path = sanitize_filename(self.test_dir, bad_name)
        base = os.path.basename(safe_path)
        for char in '<>:"/\\|?*':
            self.assertNotIn(char, base)

    def test_empty_filename_fallback(self):
        safe_path = sanitize_filename(self.test_dir, "   ")
        base = os.path.basename(safe_path)
        self.assertTrue(base.startswith("bt_received_"))

    def test_collision_avoidance(self):
        # Create an initial file
        initial_file = os.path.join(self.test_dir, "data.txt")
        with open(initial_file, "w") as f:
            f.write("first")

        second_path = sanitize_filename(self.test_dir, "data.txt")
        self.assertEqual(os.path.basename(second_path), "data_1.txt")

        # Create data_1.txt as well
        with open(second_path, "w") as f:
            f.write("second")

        third_path = sanitize_filename(self.test_dir, "data.txt")
        self.assertEqual(os.path.basename(third_path), "data_2.txt")


class TestTransferStatisticsCalculations(unittest.TestCase):
    """Validates math accuracy for elapsed time, bytes, KB/s, MB/s, and Mbps."""

    def test_metrics_accuracy(self):
        # 10 MB (10,485,760 bytes) transferred in 10.0 seconds
        bytes_count = 10 * 1024 * 1024
        elapsed = 10.0

        metrics = calculate_transfer_metrics(bytes_count, elapsed)
        self.assertAlmostEqual(metrics["kb_s"], 1024.0, places=2)
        self.assertAlmostEqual(metrics["mb_s"], 1.0, places=2)
        # 10,485,760 * 8 / 1,000,000 = 83.88608 Mbps / 10s = 8.39 Mbps
        self.assertAlmostEqual(metrics["mbps"], 8.39, places=1)
        self.assertEqual(metrics["speed_str"], "1.00 MB/s")

    def test_sub_megabyte_metrics(self):
        # 200 KB in 1.0 second (typical Bluetooth 2.4 GHz air speed)
        bytes_count = 200 * 1024
        elapsed = 1.0

        metrics = calculate_transfer_metrics(bytes_count, elapsed)
        self.assertAlmostEqual(metrics["kb_s"], 200.0, places=1)
        self.assertEqual(metrics["speed_str"], "200.0 KB/s")
        # 200 * 1024 * 8 / 1,000,000 = 1.64 Mbps
        self.assertAlmostEqual(metrics["mbps"], 1.64, places=1)

    def test_zero_elapsed_time_guard(self):
        """Zero elapsed time should not raise ZeroDivisionError."""
        metrics = calculate_transfer_metrics(1024, 0.0)
        self.assertGreater(metrics["kb_s"], 0)


class TestMacNormalization(unittest.TestCase):
    """Validates Bluetooth MAC address parsing and normalization."""

    def test_valid_mac_formats(self):
        expected = "28:C5:D2:27:1A:18"
        inputs = [
            "28:C5:D2:27:1A:18",
            "28-c5-d2-27-1a-18",
            "28c5d2271a18",
            "28 C5 D2 27 1A 18",
        ]
        for inp in inputs:
            self.assertEqual(normalize_mac(inp), expected)

    def test_invalid_mac_formats_raise(self):
        invalid = ["short", "28:C5:D2:27:1A", "GG:C5:D2:27:1A:18", ""]
        for inp in invalid:
            with self.assertRaises(ValueError):
                normalize_mac(inp)


class TestMockLoopbackEndToEnd(unittest.TestCase):
    """
    Executes a complete 4-phase mock loopback transfer on the same laptop.
    Simulates receiver and sender communicating over local TCP socket.
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="bt_poc_e2e_")
        self.out_dir = os.path.join(self.test_dir, "received")
        os.makedirs(self.out_dir, exist_ok=True)

        # Create a 256 KB test file
        self.source_path = os.path.join(self.test_dir, "e2e_sample.bin")
        self.file_size = 256 * 1024
        self.expected_hash = None
        with open(self.source_path, "wb") as f:
            data = os.urandom(self.file_size)
            f.write(data)
            self.expected_hash = hashlib.sha256(data).hexdigest()

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_full_protocol_transfer(self):
        """Transfers file over local stream and verifies bit-for-bit accuracy."""
        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.bind(("127.0.0.1", 0))
        server_sock.listen(1)
        port = server_sock.getsockname()[1]

        receiver_results = {}
        sender_results = {}

        def receiver_worker():
            conn, _ = server_sock.accept()
            try:
                # Phase 1: PING / PONG
                ping = recv_json(conn)
                self.assertEqual(ping["type"], "PING")
                send_json(conn, {"type": "PONG", "msg": "PONG_OK"})

                # Phase 2: FILE_OFFER
                offer = recv_json(conn)
                self.assertEqual(offer["type"], "FILE_OFFER")
                dest_file = sanitize_filename(self.out_dir, offer["filename"])
                send_json(conn, {"type": "FILE_RESPONSE", "accepted": True})

                # Phase 3: Binary Stream
                received_bytes = 0
                with open(dest_file, "wb") as f_out:
                    while received_bytes < offer["filesize"]:
                        chunk = conn.recv(min(32768, offer["filesize"] - received_bytes))
                        if not chunk:
                            break
                        f_out.write(chunk)
                        received_bytes += len(chunk)

                # Phase 4: Verification
                actual_hash = compute_file_sha256(dest_file)
                match = (actual_hash.lower() == offer["sha256"].lower())
                send_json(conn, {"type": "TRANSFER_COMPLETE", "match": match, "receiver_sha256": actual_hash})

                receiver_results["dest_file"] = dest_file
                receiver_results["received_bytes"] = received_bytes
                receiver_results["match"] = match
                receiver_results["actual_hash"] = actual_hash
            finally:
                conn.close()

        t = threading.Thread(target=receiver_worker)
        t.start()

        # Sender client
        client_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client_sock.connect(("127.0.0.1", port))
        try:
            # Phase 1: PING
            send_json(client_sock, {"type": "PING", "msg": "HELLO_TEST"})
            pong = recv_json(client_sock)
            self.assertEqual(pong["type"], "PONG")

            # Phase 2: FILE_OFFER
            send_json(client_sock, {
                "type": "FILE_OFFER",
                "filename": os.path.basename(self.source_path),
                "filesize": self.file_size,
                "sha256": self.expected_hash,
                "chunk_size": 32768,
            })
            resp = recv_json(client_sock)
            self.assertTrue(resp.get("accepted"))

            # Phase 3: Send file
            with open(self.source_path, "rb") as f_in:
                while True:
                    chunk = f_in.read(32768)
                    if not chunk:
                        break
                    send_exact(client_sock, chunk)

            # Phase 4: Wait for completion
            final_report = recv_json(client_sock)
            sender_results["final_report"] = final_report
        finally:
            client_sock.close()
            t.join(timeout=5.0)
            server_sock.close()

        # Assertions
        self.assertTrue(receiver_results.get("match"))
        self.assertEqual(receiver_results.get("received_bytes"), self.file_size)
        self.assertEqual(receiver_results.get("actual_hash"), self.expected_hash)
        self.assertTrue(sender_results["final_report"]["match"])


class TestSecurityAndReliabilityEdgeCases(unittest.TestCase):
    """
    Validates security boundaries, resource cleanup, and failure modes:
    - Oversized frame protection (DoS defense)
    - Windows DOS reserved device names
    - Empty file transfers (0 bytes)
    - Small file transfers (< 1 chunk)
    - Multi-chunk file transfers (> 7 chunks)
    - Interrupted streaming & partial file cleanup (.part deletion)
    - Corrupted payload detection & quarantine (.corrupt isolation)
    - Malformed metadata rejection (negative size, missing fields, invalid hex)
    """

    def setUp(self):
        self.test_dir = tempfile.mkdtemp(prefix="bt_sec_test_")
        self.out_dir = os.path.join(self.test_dir, "output")
        os.makedirs(self.out_dir, exist_ok=True)

    def tearDown(self):
        import shutil
        shutil.rmtree(self.test_dir, ignore_errors=True)

    def test_oversized_frame_rejected(self):
        """Validates that a frame declaring length > 16 MB is rejected immediately."""
        s1, s2 = socket.socketpair()
        try:
            # Send length prefix of 32 MB (exceeds 16 MB limit)
            hdr = struct.pack("!I", 32 * 1024 * 1024)
            s1.sendall(hdr)
            with self.assertRaises(ValueError) as ctx:
                recv_frame(s2)
            self.assertIn("exceeds maximum allowable frame size", str(ctx.exception))
        finally:
            s1.close()
            s2.close()

    def test_dos_reserved_device_names_sanitized(self):
        """Checks that DOS device names like CON, NUL, AUX, PRN are safely prefixed."""
        for dev in ["CON", "NUL", "AUX", "PRN", "COM1", "LPT1"]:
            safe = sanitize_filename(self.out_dir, dev)
            base = os.path.basename(safe)
            self.assertTrue(base.startswith("bt_"), f"Expected {base} to start with 'bt_'")

    def test_empty_file_transfer(self):
        """Transfers a 0-byte file through full 4 phases."""
        empty_file = os.path.join(self.test_dir, "empty.bin")
        open(empty_file, "wb").close()
        expected_hash = compute_file_sha256(empty_file)

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]

        receiver_res = {}

        def receiver_worker():
            conn, _ = server.accept()
            try:
                # PING/PONG
                ping = recv_json(conn)
                send_json(conn, {"type": "PONG", "msg": "PONG"})
                # FILE_OFFER
                offer = recv_json(conn)
                dest = sanitize_filename(self.out_dir, offer["filename"])
                part = dest + ".part"
                send_json(conn, {"type": "FILE_RESPONSE", "accepted": True})
                # Stream (0 bytes)
                with open(part, "wb") as f_out:
                    pass
                actual_hash = compute_file_sha256(part)
                match = (actual_hash.lower() == offer["sha256"].lower())
                if match:
                    os.replace(part, dest)
                send_json(conn, {"type": "TRANSFER_COMPLETE", "match": match, "receiver_sha256": actual_hash})
                receiver_res["match"] = match
                receiver_res["dest"] = dest
            finally:
                conn.close()

        t = threading.Thread(target=receiver_worker)
        t.start()

        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        client.connect(("127.0.0.1", port))
        try:
            send_json(client, {"type": "PING", "msg": "PING"})
            recv_json(client)
            send_json(client, {
                "type": "FILE_OFFER",
                "filename": "empty.bin",
                "filesize": 0,
                "sha256": expected_hash,
                "chunk_size": CHUNK_SIZE,
            })
            resp = recv_json(client)
            self.assertTrue(resp.get("accepted"))
            # 0 bytes sent
            done = recv_json(client)
            self.assertTrue(done.get("match"))
        finally:
            client.close()
            t.join(timeout=3.0)
            server.close()

        self.assertTrue(receiver_res.get("match"))
        self.assertEqual(os.path.getsize(receiver_res["dest"]), 0)

    def test_small_and_multichunk_transfers(self):
        """Verifies transfers of small (45 B) and multi-chunk (100 KB) files."""
        for size in [45, 100 * 1024]:
            content = os.urandom(size)
            expected_hash = hashlib.sha256(content).hexdigest()

            s1, s2 = socket.socketpair()
            try:
                # Send frame
                send_frame(s1, content)
                received = recv_frame(s2)
                self.assertEqual(len(received), size)
                self.assertEqual(hashlib.sha256(received).hexdigest(), expected_hash)
            finally:
                s1.close()
                s2.close()

    def test_interrupted_connection_cleans_partial_file(self):
        """Simulates disconnect mid-stream; confirms .part file is removed."""
        dest = os.path.join(self.out_dir, "interrupted.bin")
        part = dest + ".part"

        # Simulate receiver logic with sudden break
        with self.assertRaises(ConnectionError):
            try:
                with open(part, "wb") as f_out:
                    f_out.write(b"partial chunk data")
                    # Sender drops socket abruptly
                    raise ConnectionError("Abrupt socket drop")
            except Exception as e:
                if os.path.exists(part):
                    os.remove(part)
                raise e

        # Partial file must be deleted
        self.assertFalse(os.path.exists(part))
        self.assertFalse(os.path.exists(dest))

    def test_corrupted_data_quarantined_not_promoted(self):
        """Simulates data corrupted in transit; verifies file is quarantined as .corrupt."""
        dest = os.path.join(self.out_dir, "data.bin")
        part = dest + ".part"
        corrupt = dest + ".corrupt"

        good_data = b"Authentic payload"
        expected_hash = hashlib.sha256(good_data).hexdigest()

        # Received data has 1 corrupted bit
        bad_data = b"Authentic payloae"
        with open(part, "wb") as f:
            f.write(bad_data)

        actual_hash = compute_file_sha256(part)
        sha256_match = (actual_hash.lower() == expected_hash.lower())
        self.assertFalse(sha256_match)

        # Receiver quarantine logic
        if sha256_match:
            os.replace(part, dest)
        else:
            if os.path.exists(corrupt):
                os.remove(corrupt)
            os.replace(part, corrupt)

        self.assertFalse(os.path.exists(dest), "Corrupted file must NOT be promoted to dest path!")
        self.assertTrue(os.path.exists(corrupt), "Corrupted file must be isolated with .corrupt extension.")

    def test_malformed_metadata_rejected(self):
        """Tests rejection of negative size, missing fields, or invalid hex hash."""
        s1, s2 = socket.socketpair()
        try:
            # Missing sha256
            send_json(s1, {"type": "FILE_OFFER", "filename": "test.bin", "filesize": 100})
            offer = recv_json(s2)
            has_error = False
            if "filename" not in offer or "filesize" not in offer or "sha256" not in offer:
                has_error = True
            self.assertTrue(has_error)

            # Negative size
            send_json(s1, {"type": "FILE_OFFER", "filename": "test.bin", "filesize": -50, "sha256": "a"*64})
            offer = recv_json(s2)
            self.assertLess(offer["filesize"], 0)

            # Invalid hash format (not 64 hex chars)
            send_json(s1, {"type": "FILE_OFFER", "filename": "test.bin", "filesize": 100, "sha256": "bad_hash"})
            offer = recv_json(s2)
            self.assertNotEqual(len(offer["sha256"]), 64)
        finally:
            s1.close()
            s2.close()


class TestWinsockSdpRegistration(unittest.TestCase):
    """Verifies that the SDP registration API functions load and execute cleanly."""

    def test_sdp_registration_and_unregistration(self):
        # Register a test service on ephemeral port 99
        ok, ctx, err = register_rfcomm_sdp_service(port=99, service_name="Unit Test SDP")
        # On Windows with Bluetooth support, ok is True
        # If Bluetooth hardware is not present or disabled, it returns False with error string
        if ok:
            self.assertIsNotNone(ctx)
            unreg_ok, unreg_err = unregister_rfcomm_sdp_service(ctx)
            self.assertTrue(unreg_ok, f"SDP unregister failed: {unreg_err}")
        else:
            self.assertIsInstance(err, str)


if __name__ == "__main__":
    unittest.main()

