"""
bt_manager.py — Bluetooth Classic RFCOMM Transfer Manager for Flask Web Application.

Provides background-threaded coordination for:
- Detecting Windows Bluetooth radio & querying paired devices.
- Starting and stopping the RFCOMM receiver with SDP registration.
- Background non-blocking file sending over physical RFCOMM or mock loopback.
- Real-time thread-safe transfer metrics (progress, speed in KB/s & MB/s, Mbps).
- Cryptographic SHA-256 verification and filename sanitization.
"""

import os
import sys
import time
import socket
import threading
import traceback

# Add current directory and parent to path so bt_poc imports resolve cleanly
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bt_poc.bt_common import (
    AF_BTH,
    BTPROTO_RFCOMM,
    DEFAULT_RFCOMM_CHANNEL,
    CHUNK_SIZE,
    get_local_bt_mac,
    list_paired_bt_devices,
    normalize_mac,
    compute_file_sha256,
    calculate_transfer_metrics,
    sanitize_filename,
    register_rfcomm_sdp_service,
    unregister_rfcomm_sdp_service,
    send_exact,
    recv_exact,
    send_frame,
    recv_frame,
    send_json,
    recv_json,
    MOCK_LOOPBACK_HOST,
    MOCK_DEFAULT_PORT,
    MOCK_DISCLAIMER_BANNER,
    SERVICE_UUID_STR,
    SERVICE_NAME,
)

RFCOMM_UUID = SERVICE_UUID_STR


class BluetoothManager:
    """Coordinates Bluetooth Classic RFCOMM listener, sender, and transfer state."""

    def __init__(self, upload_dir=None, history_recorder=None, chat_notifier=None, request_handler=None, auto_accept_checker=None):
        self.upload_dir = upload_dir or os.path.join(_HERE, "uploads")
        self.history_recorder = history_recorder
        self.chat_notifier = chat_notifier
        self.request_handler = request_handler
        self.auto_accept_checker = auto_accept_checker
        os.makedirs(self.upload_dir, exist_ok=True)

        self._lock = threading.Lock()

        # Listener State
        self.listener_running = False
        self.listener_channel = None
        self.listener_mac = None
        self.listener_is_mock = False
        self.sdp_registered = False
        self.listener_error = None

        self._listener_socket = None
        self._sdp_context = None
        self._listener_thread = None

        # Active Transfer State (Receiver or Sender)
        self.transfer_active = False
        self.transfer_direction = None  # "sending" or "receiving"
        self.transfer_filename = None
        self.transfer_filesize = 0
        self.transfer_bytes = 0
        self.transfer_percent = 0.0
        self.transfer_status = "idle"  # idle, connecting, negotiating, streaming, verifying, completed, error
        self.transfer_speed_str = "0.0 KB/s"
        self.transfer_mbps_str = "0.00 Mbps"
        self.transfer_kb_s = 0.0
        self.transfer_mb_s = 0.0
        self.transfer_mbps = 0.0
        self.transfer_elapsed = 0.0
        self.transfer_sha256_match = None
        self.transfer_error = None
        self.transfer_is_mock = False
        self.transfer_peer = None

        self._active_conn_sock = None
        self._sender_thread = None
        self._cancel_flag = False

        # Received files cache
        self.received_files = []

    # ─── System & Device Information ─────────────────────────────────────────

    def get_adapter_info(self):
        """Returns local Bluetooth hardware status and paired devices."""
        is_windows = sys.platform == "win32"
        local_mac = None
        available = False

        if is_windows:
            try:
                local_mac = get_local_bt_mac()
                available = bool(local_mac)
            except Exception:
                available = False

        return {
            "available": available,
            "mac": local_mac or "Unavailable / Radio Off",
            "mac_address": local_mac,
            "hostname": socket.gethostname(),
            "platform": sys.platform,
            "service_uuid": RFCOMM_UUID,
            "channel_default": DEFAULT_RFCOMM_CHANNEL,
            "default_channel": DEFAULT_RFCOMM_CHANNEL,
        }

    def get_paired_devices(self):
        """Returns list of paired Bluetooth devices from Windows registry."""
        if sys.platform != "win32":
            return []
        try:
            raw = list_paired_bt_devices()
            devices = []
            for d in raw:
                mac = d.get("mac") or d.get("address", "")
                name = d.get("name") or "Unknown Device"
                devices.append({
                    "name": name,
                    "mac": mac,
                    "address": mac,
                })
            return devices
        except Exception:
            return []

    # ─── Status Snapshot for Polling ─────────────────────────────────────────

    def get_status_snapshot(self):
        """Returns thread-safe snapshot of Bluetooth listener and transfer state."""
        with self._lock:
            adapter = self.get_adapter_info()
            listener_info = {
                "running": self.listener_running,
                "channel": self.listener_channel,
                "mac": self.listener_mac,
                "is_mock": self.listener_is_mock,
                "mock": self.listener_is_mock,
                "sdp_registered": self.sdp_registered,
                "error": self.listener_error,
            }
            transfer_info = {
                "active": self.transfer_active,
                "direction": self.transfer_direction,
                "filename": self.transfer_filename,
                "filesize": self.transfer_filesize,
                "total_bytes": self.transfer_filesize,
                "bytes_transferred": self.transfer_bytes,
                "transferred_bytes": self.transfer_bytes,
                "percent": round(self.transfer_percent, 1),
                "status": self.transfer_status,
                "state": self.transfer_status,
                "speed_str": self.transfer_speed_str,
                "mbps_str": self.transfer_mbps_str,
                "kb_s": round(self.transfer_kb_s, 2),
                "speed_kbps": round(self.transfer_kb_s, 2),
                "mb_s": round(self.transfer_mb_s, 2),
                "speed_mbps": round(self.transfer_mb_s, 2),
                "mbps": round(self.transfer_mbps, 2),
                "elapsed_seconds": round(self.transfer_elapsed, 2),
                "sha256_match": self.transfer_sha256_match,
                "sha256_hash": getattr(self, "transfer_hash", None),
                "error": self.transfer_error,
                "is_mock": self.transfer_is_mock,
                "mock": self.transfer_is_mock,
                "peer": self.transfer_peer,
                "peer_mac": self.transfer_peer,
            }
            return {
                "adapter": adapter,
                "listener": listener_info,
                "receiver": listener_info,
                "transfer": transfer_info,
            }

    # ─── Receiver Controls ───────────────────────────────────────────────────

    def start_receiver(self, channel=DEFAULT_RFCOMM_CHANNEL, mock=False, mock_port=MOCK_DEFAULT_PORT):
        """Starts the RFCOMM background receiver thread."""
        with self._lock:
            if self.listener_running:
                return True, "Receiver is already active."

            self.listener_error = None
            self.listener_is_mock = mock

            try:
                if mock:
                    server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                    server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    server_sock.bind((MOCK_LOOPBACK_HOST, mock_port))
                    server_sock.listen(1)
                    bound_mac = f"{MOCK_LOOPBACK_HOST}:{mock_port} [Loopback]"
                    bound_channel = mock_port
                    sdp_ok = False
                else:
                    # Probe and bind RFCOMM channel
                    channels_to_try = [channel] + [c for c in range(2, 16) if c != channel]
                    server_sock = None
                    last_err = None
                    for ch in channels_to_try:
                        s = socket.socket(AF_BTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
                        try:
                            s.bind(("00:00:00:00:00:00", ch))
                            s.listen(1)
                            actual_mac, actual_port = s.getsockname()
                            server_sock = s
                            bound_mac = actual_mac
                            bound_channel = actual_port
                            break
                        except Exception as e:
                            last_err = e
                            s.close()

                    if not server_sock:
                        raise RuntimeError(f"Could not bind RFCOMM channel (last error: {last_err}). Ensure Bluetooth is enabled.")

                    # Register with Windows SDP database
                    sdp_ok, self._sdp_context, sdp_err = register_rfcomm_sdp_service(bound_channel)

                self._listener_socket = server_sock
                self.listener_running = True
                self.listener_channel = bound_channel
                self.listener_mac = bound_mac
                self.sdp_registered = sdp_ok

            except Exception as e:
                self.listener_running = False
                self.listener_error = str(e)
                return False, f"Failed to start receiver: {e}"

        # Spawn listener loop thread
        self._listener_thread = threading.Thread(target=self._receiver_worker_loop, daemon=True)
        self._listener_thread.start()

        if self.chat_notifier:
            mode_tag = " [Mock Loopback]" if mock else ""
            self.chat_notifier("System", f"📶 Bluetooth receiver started on Channel {bound_channel}{mode_tag}")

        return True, f"Listening on {bound_mac} (Channel {bound_channel})"

    def stop_receiver(self):
        """Stops the RFCOMM receiver and cleans up sockets and SDP records."""
        with self._lock:
            if not self.listener_running:
                return True, "Receiver is not running."

            self.listener_running = False
            sock = self._listener_socket
            sdp_ctx = self._sdp_context
            self._listener_socket = None
            self._sdp_context = None

        if sock:
            try:
                sock.close()
            except Exception:
                pass

        if sdp_ctx:
            try:
                unregister_rfcomm_sdp_service(sdp_ctx)
            except Exception:
                pass

        if self.chat_notifier:
            self.chat_notifier("System", "📶 Bluetooth receiver stopped")

        return True, "Receiver stopped successfully."

    def _receiver_worker_loop(self):
        """Background thread awaiting connections and receiving files."""
        while self.listener_running:
            server_sock = self._listener_socket
            if not server_sock:
                break

            client_sock = None
            try:
                client_sock, client_addr = server_sock.accept()
                self._active_conn_sock = client_sock
                client_sock.settimeout(65.0)

                peer_name = str(client_addr[0] if isinstance(client_addr, tuple) else client_addr)
                is_loopback = (self.transfer_active and self.transfer_direction == "sending")

                if not is_loopback:
                    with self._lock:
                        self.transfer_active = True
                        self.transfer_direction = "receiving"
                        self.transfer_status = "negotiating"
                        self.transfer_peer = peer_name
                        self.transfer_is_mock = self.listener_is_mock
                        self.transfer_error = None
                        self.transfer_sha256_match = None
                        self._cancel_flag = False

                # Phase 1: Test Handshake
                msg_in = recv_json(client_sock)
                if msg_in.get("type") != "PING":
                    raise ValueError(f"Expected PING frame, got: {msg_in}")

                send_json(client_sock, {
                    "type": "PONG",
                    "msg": "PONG_BLUETOOTH",
                    "receiver_name": socket.gethostname(),
                    "mock": self.listener_is_mock,
                })

                # Phase 2: Metadata Negotiation
                offer = recv_json(client_sock)
                if offer.get("type") != "FILE_OFFER":
                    raise ValueError(f"Expected FILE_OFFER frame, got: {offer}")

                filename = offer.get("filename", f"bt_file_{int(time.time())}.bin")
                filesize = int(offer.get("filesize", 0))
                expected_sha256 = str(offer.get("sha256", "")).lower()
                chunk_size = int(offer.get("chunk_size", CHUNK_SIZE))
                sender_device_name = offer.get("sender_name", peer_name)

                # Check acceptance
                accepted = False
                auto_accept = False
                if self.auto_accept_checker:
                    try:
                        auto_accept = bool(self.auto_accept_checker())
                    except Exception:
                        auto_accept = False

                if auto_accept:
                    accepted = True
                elif self.request_handler:
                    transfer_id = f"bt_{int(time.time() * 1000)}"
                    if not is_loopback:
                        with self._lock:
                            self.transfer_filename = filename
                            self.transfer_filesize = filesize
                            self.transfer_status = "waiting_for_approval"
                            self.transfer_peer = peer_name
                    accepted = self.request_handler(
                        transfer_id=transfer_id,
                        filename=filename,
                        total_size=filesize,
                        sender_name=f"{sender_device_name} ({peer_name})",
                        mode="bluetooth",
                        timeout=60.0,
                    )
                else:
                    accepted = True

                if not accepted:
                    send_json(client_sock, {
                        "type": "FILE_RESPONSE",
                        "accepted": False,
                        "reason": "Transfer rejected by receiver.",
                    })
                    if self.chat_notifier and not is_loopback:
                        self.chat_notifier("System", f"❌ Bluetooth transfer of '{filename}' was rejected by receiver")
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                    if not is_loopback:
                        with self._lock:
                            self.transfer_active = False
                            self.transfer_status = "idle"
                            self.transfer_error = "Incoming transfer was rejected."
                    continue

                dest_path = sanitize_filename(self.upload_dir, filename)
                part_path = dest_path + ".part"

                if not is_loopback:
                    with self._lock:
                        self.transfer_filename = os.path.basename(dest_path)
                        self.transfer_filesize = filesize
                        self.transfer_bytes = 0
                        self.transfer_percent = 0.0
                        self.transfer_status = "streaming"

                # Send approval
                send_json(client_sock, {"type": "FILE_RESPONSE", "accepted": True})
                if self.chat_notifier and not is_loopback:
                    self.chat_notifier("System", f"📥 Receiving '{os.path.basename(dest_path)}' via Bluetooth from {peer_name}")

                # Phase 3: Binary Streaming
                start_time = time.time()
                last_time = start_time
                received_bytes = 0

                try:
                    with open(part_path, "wb") as f_out:
                        while received_bytes < filesize:
                            if self._cancel_flag:
                                raise ConnectionAbortedError("Transfer cancelled by user.")

                            remaining = filesize - received_bytes
                            read_target = min(chunk_size, remaining)
                            chunk = client_sock.recv(read_target)
                            if not chunk:
                                raise ConnectionError(f"Connection dropped at byte {received_bytes:,} of {filesize:,}.")

                            f_out.write(chunk)
                            received_bytes += len(chunk)

                            if not is_loopback:
                                now = time.time()
                                if now - last_time >= 0.2 or received_bytes == filesize:
                                    elapsed = max(0.0001, now - start_time)
                                    metrics = calculate_transfer_metrics(received_bytes, elapsed)
                                    with self._lock:
                                        self.transfer_bytes = received_bytes
                                        self.transfer_percent = (received_bytes / filesize) * 100 if filesize > 0 else 100.0
                                        self.transfer_kb_s = metrics["kb_s"]
                                        self.transfer_mb_s = metrics["mb_s"]
                                        self.transfer_mbps = metrics["mbps"]
                                        self.transfer_speed_str = metrics["speed_str"]
                                        self.transfer_mbps_str = metrics["mbps_str"]
                                        self.transfer_elapsed = elapsed
                                    last_time = now

                except Exception as e:
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                    raise e

                # Phase 4: SHA-256 Integrity Verification
                if not is_loopback:
                    with self._lock:
                        self.transfer_status = "verifying"

                actual_sha256 = compute_file_sha256(part_path)
                sha256_match = (actual_sha256.lower() == expected_sha256.lower())

                if sha256_match:
                    if os.path.exists(dest_path):
                        try:
                            os.remove(dest_path)
                        except Exception:
                            pass
                    os.replace(part_path, dest_path)
                    saved_path = dest_path
                else:
                    corrupt_path = dest_path + ".corrupt"
                    if os.path.exists(corrupt_path):
                        try:
                            os.remove(corrupt_path)
                        except Exception:
                            pass
                    os.replace(part_path, corrupt_path)
                    saved_path = corrupt_path

                # Send completion frame
                send_json(client_sock, {
                    "type": "TRANSFER_COMPLETE",
                    "match": sha256_match,
                    "receiver_sha256": actual_sha256,
                })

                total_elapsed = max(0.0001, time.time() - start_time)
                metrics = calculate_transfer_metrics(filesize, total_elapsed)

                if not is_loopback:
                    with self._lock:
                        self.transfer_percent = 100.0
                        self.transfer_status = "completed" if sha256_match else "error"
                        self.transfer_sha256_match = sha256_match
                        self.transfer_hash = actual_sha256
                        self.transfer_kb_s = metrics["kb_s"]
                        self.transfer_mb_s = metrics["mb_s"]
                        self.transfer_mbps = metrics["mbps"]
                        self.transfer_speed_str = metrics["speed_str"]
                        self.transfer_mbps_str = metrics["mbps_str"]
                        self.transfer_elapsed = total_elapsed
                        if not sha256_match:
                            self.transfer_error = "SHA-256 integrity mismatch. File quarantined."

                if sha256_match:
                    self.received_files.append({
                        "filename": os.path.basename(dest_path),
                        "size": filesize,
                        "sha256": actual_sha256,
                        "time": time.strftime("%H:%M:%S"),
                        "path": dest_path,
                    })

                # Record in transfer history if standalone receiver
                if not is_loopback and self.history_recorder:
                    tid = f"bt_rec_{int(time.time() * 1000)}"
                    self.history_recorder(
                        transfer_id=tid,
                        filename=os.path.basename(dest_path),
                        file_size=filesize,
                        direction="received",
                        peer_name=f"{peer_name} [Bluetooth]",
                        peer_ip="Bluetooth",
                        duration_seconds=total_elapsed,
                        sha256_match=sha256_match,
                    )

                if self.chat_notifier:
                    verdict = "✅ SHA-256 verified" if sha256_match else "❌ SHA-256 mismatch"
                    self.chat_notifier("System", f"📥 Finished Bluetooth receive of '{os.path.basename(dest_path)}' ({metrics['speed_str']}) — {verdict}")

            except Exception as exc:
                if self.listener_running:
                    with self._lock:
                        self.transfer_status = "error"
                        self.transfer_error = str(exc)
            finally:
                if client_sock:
                    try:
                        client_sock.close()
                    except Exception:
                        pass
                self._active_conn_sock = None
                time.sleep(0.5)
                with self._lock:
                    if self.transfer_status in ("completed", "error"):
                        # Keep completed/error state visible until next action or reset
                        pass

    # ─── Sender Controls ─────────────────────────────────────────────────────

    def start_send_file(self, filepath, target_mac=None, channel=DEFAULT_RFCOMM_CHANNEL, mock=False, mock_port=MOCK_DEFAULT_PORT):
        """Initiates a background send transfer without blocking Flask."""
        if not os.path.isfile(filepath):
            return False, f"File not found on disk: {filepath}"

        with self._lock:
            if self.transfer_active and self.transfer_status in ("connecting", "negotiating", "streaming"):
                return False, "Another transfer is currently in progress."

            self.transfer_active = True
            self.transfer_direction = "sending"
            self.transfer_filename = os.path.basename(filepath)
            self.transfer_filesize = os.path.getsize(filepath)
            self.transfer_bytes = 0
            self.transfer_percent = 0.0
            self.transfer_status = "connecting"
            self.transfer_speed_str = "0.0 KB/s"
            self.transfer_mbps_str = "0.00 Mbps"
            self.transfer_kb_s = 0.0
            self.transfer_mb_s = 0.0
            self.transfer_mbps = 0.0
            self.transfer_elapsed = 0.0
            self.transfer_sha256_match = None
            self.transfer_error = None
            self.transfer_is_mock = mock
            self.transfer_peer = f"127.0.0.1:{mock_port} [Loopback]" if mock else str(target_mac)
            self._cancel_flag = False

        self._sender_thread = threading.Thread(
            target=self._sender_worker,
            args=(filepath, target_mac, channel, mock, mock_port),
            daemon=True,
        )
        self._sender_thread.start()

        if self.chat_notifier:
            dest_tag = "Loopback" if mock else str(target_mac)
            self.chat_notifier("System", f"📤 Starting Bluetooth transfer of '{os.path.basename(filepath)}' to {dest_tag}...")

        return True, f"Initiated transfer of {os.path.basename(filepath)}"

    def _sender_worker(self, filepath, target_mac, channel, mock, mock_port):
        """Worker thread executing the 4-phase RFCOMM sender sequence."""
        filename = os.path.basename(filepath)
        filesize = os.path.getsize(filepath)
        sock = None

        try:
            # Hash calculation
            with self._lock:
                self.transfer_status = "negotiating"
            expected_sha256 = compute_file_sha256(filepath)

            # Establish socket
            if mock:
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(10.0)
                sock.connect((MOCK_LOOPBACK_HOST, mock_port))
            else:
                sock = socket.socket(AF_BTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
                sock.settimeout(20.0)
                norm_mac = normalize_mac(target_mac)
                sock.connect((norm_mac, channel))

            self._active_conn_sock = sock

            # Phase 1: Test Handshake
            send_json(sock, {
                "type": "PING",
                "msg": "HELLO_BLUETOOTH",
                "sender_name": socket.gethostname(),
                "mock": mock,
            })
            pong = recv_json(sock)
            if pong.get("type") != "PONG":
                raise ValueError(f"Handshake failed: expected PONG, received {pong}")

            # Phase 2: Metadata Negotiation
            send_json(sock, {
                "type": "FILE_OFFER",
                "filename": filename,
                "filesize": filesize,
                "sha256": expected_sha256,
                "chunk_size": CHUNK_SIZE,
                "sender_name": socket.gethostname(),
                "mock": mock,
            })

            with self._lock:
                self.transfer_status = "waiting_for_approval"

            sock.settimeout(65.0)
            resp = recv_json(sock)
            if not resp.get("accepted"):
                reason = resp.get("reason", "Receiver declined transfer.")
                raise PermissionError(f"Transfer rejected: {reason}")

            # Phase 3: Binary Chunk Streaming
            with self._lock:
                self.transfer_status = "streaming"

            sock.settimeout(30.0)
            bytes_sent = 0
            start_time = time.time()
            last_time = start_time

            with open(filepath, "rb") as f_in:
                while bytes_sent < filesize:
                    if self._cancel_flag:
                        raise ConnectionAbortedError("Transfer cancelled by user.")

                    chunk = f_in.read(min(CHUNK_SIZE, filesize - bytes_sent))
                    if not chunk:
                        break
                    send_exact(sock, chunk)
                    bytes_sent += len(chunk)

                    now = time.time()
                    if now - last_time >= 0.2 or bytes_sent == filesize:
                        elapsed = max(0.0001, now - start_time)
                        metrics = calculate_transfer_metrics(bytes_sent, elapsed)
                        with self._lock:
                            self.transfer_bytes = bytes_sent
                            self.transfer_percent = (bytes_sent / filesize) * 100 if filesize > 0 else 100.0
                            self.transfer_kb_s = metrics["kb_s"]
                            self.transfer_mb_s = metrics["mb_s"]
                            self.transfer_mbps = metrics["mbps"]
                            self.transfer_speed_str = metrics["speed_str"]
                            self.transfer_mbps_str = metrics["mbps_str"]
                            self.transfer_elapsed = elapsed
                        last_time = now

            # Phase 4: Remote Verification Report
            with self._lock:
                self.transfer_status = "verifying"

            sock.settimeout(30.0)
            verify_resp = recv_json(sock)
            match = bool(verify_resp.get("match", False))

            total_elapsed = max(0.0001, time.time() - start_time)
            metrics = calculate_transfer_metrics(filesize, total_elapsed)

            with self._lock:
                self.transfer_percent = 100.0
                self.transfer_status = "completed" if match else "error"
                self.transfer_sha256_match = match
                self.transfer_hash = expected_sha256
                self.transfer_kb_s = metrics["kb_s"]
                self.transfer_mb_s = metrics["mb_s"]
                self.transfer_mbps = metrics["mbps"]
                self.transfer_speed_str = metrics["speed_str"]
                self.transfer_mbps_str = metrics["mbps_str"]
                self.transfer_elapsed = total_elapsed
                if not match:
                    self.transfer_error = "Receiver reported SHA-256 checksum mismatch."

            if self.history_recorder:
                tid = f"bt_snd_{int(time.time() * 1000)}"
                peer_desc = "Loopback" if mock else str(target_mac)
                self.history_recorder(
                    transfer_id=tid,
                    filename=filename,
                    file_size=filesize,
                    direction="sent",
                    peer_name=f"{peer_desc} [Bluetooth]",
                    peer_ip="Bluetooth",
                    duration_seconds=total_elapsed,
                    sha256_match=match,
                )

            if self.chat_notifier:
                verdict = "✅ SHA-256 verified" if match else "❌ Remote SHA-256 mismatch"
                self.chat_notifier("System", f"📤 Finished Bluetooth send of '{filename}' ({metrics['speed_str']}) — {verdict}")

        except Exception as exc:
            err_str = str(exc)
            if "10061" in err_str or "actively refused" in err_str.lower():
                friendly = f"Connection refused by {target_mac} on Channel {channel}. Make sure the receiver laptop has started the Bluetooth receiver."
            elif "10051" in err_str or "unreachable" in err_str.lower():
                friendly = f"Device {target_mac} unreachable. Ensure both laptops have Bluetooth turned ON and are paired in Windows Settings."
            elif "10060" in err_str or "timed out" in err_str.lower():
                friendly = f"Connection to {target_mac} timed out. Ensure both laptops are within Bluetooth range."
            else:
                friendly = err_str

            with self._lock:
                self.transfer_status = "error"
                self.transfer_error = friendly
            if self.chat_notifier:
                self.chat_notifier("System", f"❌ Bluetooth transfer failed: {friendly}")
        finally:
            if sock:
                try:
                    sock.close()
                except Exception:
                    pass
            self._active_conn_sock = None

    # ─── Cancellation ────────────────────────────────────────────────────────

    def cancel_active_transfer(self):
        """Cancels any ongoing sending or receiving stream."""
        self._cancel_flag = True
        sock = self._active_conn_sock
        if sock:
            try:
                sock.close()
            except Exception:
                pass
        with self._lock:
            self.transfer_status = "error"
            self.transfer_error = "Transfer aborted by user."
        return True, "Transfer cancellation signal sent."
