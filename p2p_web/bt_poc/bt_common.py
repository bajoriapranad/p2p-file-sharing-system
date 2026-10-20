"""
bt_common.py — Shared Bluetooth RFCOMM protocol, metrics, and Winsock constants.
Part of the standalone Windows Bluetooth Classic RFCOMM Proof-of-Concept.
"""

import os
import sys
import time
import json
import struct
import socket
import hashlib
import winreg
import ctypes
from ctypes import wintypes

# ─── Winsock Bluetooth Constants ─────────────────────────────────────────────
# Address family AF_BTH is 32 on Windows (ws2bth.h)
AF_BTH = 32

# Protocol BTHPROTO_RFCOMM is 3 (ws2bth.h)
BTPROTO_RFCOMM = getattr(socket, "BTPROTO_RFCOMM", 3)

# Default RFCOMM Server Channel (1 to 30 valid; 4 is standard and widely available)
DEFAULT_RFCOMM_CHANNEL = 4

# Transfer chunk size (32 KB is optimal for Bluetooth baseband buffers)
CHUNK_SIZE = 32 * 1024

# Custom Service GUID for P2P File Sharing SDP registration:
# {e2f6a91d-4f18-4b96-829b-8e3649a202c1}
SERVICE_UUID_STR = "e2f6a91d-4f18-4b96-829b-8e3649a202c1"
SERVICE_NAME = "P2P Bluetooth Classic File Transfer"


# ─── Hardware MAC & Device Resolution ────────────────────────────────────────

def get_local_bt_mac():
    """
    Resolves the local machine's actual hardware Bluetooth adapter MAC address
    by binding an ephemeral RFCOMM socket to channel 0 and querying getsockname().
    Returns MAC string format: 'AA:BB:CC:DD:EE:FF' or None if Bluetooth is off.
    """
    try:
        s = socket.socket(AF_BTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
        s.bind(("00:00:00:00:00:00", 0))
        mac, port = s.getsockname()
        s.close()
        if mac and mac != "00:00:00:00:00:00":
            return mac.upper()
    except Exception as e:
        pass
    return None


def list_paired_bt_devices():
    """
    Queries the Windows Registry for paired and previously authenticated Bluetooth devices.
    Registry Key: HKLM\\SYSTEM\\CurrentControlSet\\Services\\BTHPORT\\Parameters\\Devices
    Requires zero administrative privileges and executes in < 5ms.
    Returns list of dicts: [{'name': 'LAPTOP-B', 'mac': '9C:67:D6:EC:2D:FD'}, ...]
    """
    devices = []
    key_path = r"SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices"
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, key_path) as key:
            i = 0
            while True:
                try:
                    sub = winreg.EnumKey(key, i)
                    # Convert 12-character hex string into standard MAC format
                    mac = ":".join(sub[j:j+2].upper() for j in range(0, 12, 2))
                    name = "Unknown Device"
                    try:
                        with winreg.OpenKey(key, sub) as subkey:
                            val, _ = winreg.QueryValueEx(subkey, "Name")
                            if isinstance(val, bytes):
                                name = val.decode("utf-8", errors="ignore").rstrip("\x00")
                            elif isinstance(val, str):
                                name = val.rstrip("\x00")
                    except Exception:
                        pass
                    devices.append({"name": name, "mac": mac})
                    i += 1
                except OSError:
                    break
    except Exception:
        pass
    return devices


def normalize_mac(mac_input):
    """
    Cleans up user-entered MAC addresses (strips spaces, dashes, colons).
    Accepts 'aabbccddeeff', 'aa-bb-cc-dd-ee-ff', or 'aa:bb:cc:dd:ee:ff'.
    Returns standardized format: 'AA:BB:CC:DD:EE:FF' or raises ValueError.
    """
    raw = mac_input.strip().replace(":", "").replace("-", "").replace(" ", "").upper()
    if len(raw) != 12 or not all(c in "0123456789ABCDEF" for c in raw):
        raise ValueError(f"Invalid Bluetooth MAC address format: '{mac_input}'. Expected 12 hex digits (e.g. 28:C5:D2:27:1A:18).")
    return ":".join(raw[i:i+2] for i in range(0, 12, 2))


# ─── Frame Framing & Socket Helpers ──────────────────────────────────────────

def send_exact(sock, data):
    """Sends all bytes reliably, handling partial socket writes."""
    total_sent = 0
    while total_sent < len(data):
        sent = sock.send(data[total_sent:])
        if sent == 0:
            raise ConnectionError("Socket connection closed while sending data.")
        total_sent += sent


def recv_exact(sock, num_bytes):
    """Blocks until exactly num_bytes are received, handling fragmentation."""
    buf = bytearray()
    while len(buf) < num_bytes:
        chunk = sock.recv(num_bytes - len(buf))
        if not chunk:
            raise ConnectionError("Socket closed prematurely while receiving data.")
        buf.extend(chunk)
    return bytes(buf)


# Maximum allowable size for a single framed payload (16 MB)
# Protects against memory exhaustion from malformed or malicious length prefixes
MAX_FRAME_SIZE = 16 * 1024 * 1024


def send_frame(sock, payload_bytes):
    """
    Sends a length-prefixed protocol frame.
    Format: [4 bytes Big-Endian uint32 length][payload]
    """
    if len(payload_bytes) > MAX_FRAME_SIZE:
        raise ValueError(f"Payload size ({len(payload_bytes)} bytes) exceeds maximum frame limit ({MAX_FRAME_SIZE} bytes).")
    hdr = struct.pack("!I", len(payload_bytes))
    send_exact(sock, hdr + payload_bytes)


def recv_frame(sock):
    """
    Receives a length-prefixed protocol frame.
    Returns: payload_bytes
    """
    hdr = recv_exact(sock, 4)
    length = struct.unpack("!I", hdr)[0]
    if length > MAX_FRAME_SIZE:
        raise ValueError(f"Received frame length ({length} bytes) exceeds maximum allowable frame size ({MAX_FRAME_SIZE} bytes).")
    return recv_exact(sock, length)



def send_json(sock, data_dict):
    """Encodes dict as UTF-8 JSON and transmits as a frame."""
    payload = json.dumps(data_dict).encode("utf-8")
    send_frame(sock, payload)


def recv_json(sock):
    """Receives a frame and parses it as UTF-8 JSON."""
    payload = recv_frame(sock)
    return json.loads(payload.decode("utf-8"))


# ─── File Hashing & Performance Formatting ───────────────────────────────────

def compute_file_sha256(filepath):
    """Calculates SHA-256 hash of a local file in 64 KB blocks."""
    h = hashlib.sha256()
    with open(filepath, "rb") as f:
        for block in iter(lambda: f.read(65536), b""):
            h.update(block)
    return h.hexdigest()


def calculate_transfer_metrics(bytes_transferred, elapsed_seconds):
    """
    Computes standard transfer performance statistics:
    - elapsed_seconds: Duration in seconds
    - bytes_transferred: Total bytes moved
    - kb_s: Kilobytes per second (1 KB = 1024 B)
    - mb_s: Megabytes per second (1 MB = 1024 * 1024 B)
    - mbps: Megabits per second (1 Mbps = 1,000,000 bits/s)
    Returns dict with raw numerical values and formatted strings.
    """
    elapsed = max(0.0001, float(elapsed_seconds))
    bps = bytes_transferred / elapsed
    kb_s = bps / 1024.0
    mb_s = bps / (1024.0 * 1024.0)
    mbps = (bps * 8.0) / 1_000_000.0

    if mb_s >= 1.0:
        speed_str = f"{mb_s:.2f} MB/s"
    else:
        speed_str = f"{kb_s:.1f} KB/s"
    mbps_str = f"{mbps:.2f} Mbps"

    return {
        "elapsed_seconds": elapsed,
        "bytes_transferred": bytes_transferred,
        "kb_s": kb_s,
        "mb_s": mb_s,
        "mbps": mbps,
        "speed_str": speed_str,
        "mbps_str": mbps_str,
    }


def format_bytes(num_bytes):
    """Formats bytes into human-readable B, KB, MB, or GB."""
    if num_bytes < 1024:
        return f"{num_bytes} B"
    elif num_bytes < 1024 * 1024:
        return f"{num_bytes / 1024:.2f} KB"
    elif num_bytes < 1024 * 1024 * 1024:
        return f"{num_bytes / (1024 * 1024):.2f} MB"
    else:
        return f"{num_bytes / (1024 * 1024 * 1024):.2f} GB"


def format_speed(bps):
    """
    Formats transfer throughput.
    bps = Bytes per second.
    Returns (speed_str, mbps_str)
    """
    metrics = calculate_transfer_metrics(bps, 1.0)
    return metrics["speed_str"], metrics["mbps_str"]


RESERVED_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    "COM1", "COM2", "COM3", "COM4", "COM5", "COM6", "COM7", "COM8", "COM9",
    "LPT1", "LPT2", "LPT3", "LPT4", "LPT5", "LPT6", "LPT7", "LPT8", "LPT9",
}


def sanitize_filename(output_dir, filename):
    """
    Sanitizes a received filename to prevent path traversal attacks,
    strips invalid characters, avoids Windows reserved device names,
    and resolves duplicate naming collisions.
    Returns safe absolute filepath within output_dir.
    """
    os.makedirs(output_dir, exist_ok=True)
    # Strip drive letters and normalize both forward and backslashes
    clean = str(filename).replace("\x00", "").replace("\\", "/")
    safe_name = os.path.basename(clean.rstrip("/")).strip()
    if ":" in safe_name:
        safe_name = safe_name.split(":")[-1].strip()

    # Remove potentially dangerous characters for Windows
    for char in '<>:"/\\|?*':
        safe_name = safe_name.replace(char, "_")

    safe_name = safe_name.strip(". ")
    if not safe_name:
        safe_name = f"bt_received_{int(time.time())}.bin"

    base, ext = os.path.splitext(safe_name)
    # Prefix Windows reserved DOS device names to prevent OS-level I/O collisions
    if base.upper() in RESERVED_DEVICE_NAMES:
        safe_name = f"bt_{safe_name}"
        base = f"bt_{base}"

    candidate = os.path.join(output_dir, safe_name)
    counter = 1
    while os.path.exists(candidate):
        candidate = os.path.join(output_dir, f"{base}_{counter}{ext}")
        counter += 1
    return candidate



def print_progress_bar(transferred, total, start_time, last_transferred, last_time, label=""):
    """Renders a single-line terminal progress bar with live speed."""
    now = time.time()
    elapsed = max(0.001, now - start_time)
    pct = (transferred / total) * 100 if total > 0 else 100.0

    # Calculate speed over recent window (or overall)
    dt = now - last_time
    if dt > 0.3:
        instant_bps = (transferred - last_transferred) / dt
    else:
        instant_bps = transferred / elapsed

    speed_str, mbps_str = format_speed(instant_bps)
    bar_width = 25
    filled = int(bar_width * (transferred / total)) if total > 0 else bar_width
    bar = "=" * filled + "-" * (bar_width - filled)

    tag = f" {label}" if label else ""
    sys.stdout.write(
        f"\r  [{bar}] {pct:5.1f}% | {format_bytes(transferred)} / {format_bytes(total)} | {speed_str} ({mbps_str}) | {elapsed:4.1f}s{tag}"
    )
    sys.stdout.flush()


# ─── Winsock SDP Service Registration (ws2bth.h) ─────────────────────────────
# Allows the RFCOMM listener to publish its custom GUID in the Windows SDP
# database so remote clients can discover the service dynamically.

class _GUID(ctypes.Structure):
    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_byte * 8),
    ]

def _str_to_guid(uuid_str):
    import uuid
    u = uuid.UUID(uuid_str)
    fields = u.fields
    g = _GUID()
    g.Data1 = fields[0]
    g.Data2 = fields[1]
    g.Data3 = fields[2]
    g.Data4 = (ctypes.c_byte * 8)(*u.bytes[8:])
    return g

class _SOCKADDR_BTH(ctypes.Structure):
    _fields_ = [
        ("addressFamily", ctypes.c_ushort),  # AF_BTH = 32
        ("btAddr", ctypes.c_ulonglong),      # BTH_ADDR
        ("serviceClassId", _GUID),           # GUID
        ("port", wintypes.ULONG),            # RFCOMM channel (1-30)
    ]

class _SOCKET_ADDRESS(ctypes.Structure):
    _fields_ = [
        ("lpSockaddr", ctypes.c_void_p),
        ("iSockaddrLength", ctypes.c_int),
    ]

class _CSADDR_INFO(ctypes.Structure):
    _fields_ = [
        ("LocalAddr", _SOCKET_ADDRESS),
        ("RemoteAddr", _SOCKET_ADDRESS),
        ("iSocketType", ctypes.c_int),       # SOCK_STREAM = 1
        ("iProtocol", ctypes.c_int),         # BTHPROTO_RFCOMM = 3
    ]

class _WSAQUERYSETW(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("lpszServiceInstanceName", wintypes.LPWSTR),
        ("lpServiceClassId", ctypes.POINTER(_GUID)),
        ("lpVersion", ctypes.c_void_p),
        ("lpszComment", wintypes.LPWSTR),
        ("dwNameSpace", wintypes.DWORD),      # NS_BTH = 16
        ("lpNSProviderId", ctypes.c_void_p),
        ("lpszContext", wintypes.LPWSTR),
        ("dwNumberOfProtocols", wintypes.DWORD),
        ("lpafpProtocols", ctypes.c_void_p),
        ("lpszQueryString", wintypes.LPWSTR),
        ("dwNumberOfCsAddrs", wintypes.DWORD),
        ("lpcsaBuffer", ctypes.POINTER(_CSADDR_INFO)),
        ("dwOutputFlags", wintypes.DWORD),
        ("lpBlob", ctypes.c_void_p),
    ]


def register_rfcomm_sdp_service(port, service_uuid_str=SERVICE_UUID_STR, service_name=SERVICE_NAME):
    """
    Registers an RFCOMM service with the Windows Bluetooth Service Discovery
    Protocol (SDP) using Winsock WSASetServiceW.
    Returns (success_bool, registration_context_dict, error_message).
    """
    try:
        ws2_32 = ctypes.windll.ws2_32
        WSASetServiceW = ws2_32.WSASetServiceW
        WSASetServiceW.argtypes = [ctypes.POINTER(_WSAQUERYSETW), ctypes.c_int, wintypes.DWORD]
        WSASetServiceW.restype = ctypes.c_int

        guid = _str_to_guid(service_uuid_str)
        sockaddr = _SOCKADDR_BTH()
        sockaddr.addressFamily = AF_BTH
        sockaddr.btAddr = 0
        sockaddr.serviceClassId = guid
        sockaddr.port = int(port)

        csaddr = _CSADDR_INFO()
        csaddr.LocalAddr.lpSockaddr = ctypes.cast(ctypes.pointer(sockaddr), ctypes.c_void_p)
        csaddr.LocalAddr.iSockaddrLength = ctypes.sizeof(sockaddr)
        csaddr.RemoteAddr.lpSockaddr = ctypes.cast(ctypes.pointer(sockaddr), ctypes.c_void_p)
        csaddr.RemoteAddr.iSockaddrLength = ctypes.sizeof(sockaddr)
        csaddr.iSocketType = socket.SOCK_STREAM
        csaddr.iProtocol = BTPROTO_RFCOMM

        qs = _WSAQUERYSETW()
        qs.dwSize = ctypes.sizeof(_WSAQUERYSETW)
        qs.lpszServiceInstanceName = str(service_name)
        qs.lpServiceClassId = ctypes.pointer(guid)
        qs.lpszComment = "P2P Bluetooth RFCOMM Transfer"
        qs.dwNameSpace = 16  # NS_BTH
        qs.dwNumberOfCsAddrs = 1
        qs.lpcsaBuffer = ctypes.pointer(csaddr)

        # RNRSERVICE_REGISTER = 0
        ret = WSASetServiceW(ctypes.pointer(qs), 0, 0)
        if ret == 0:
            context = {
                "guid": guid,
                "sockaddr": sockaddr,
                "csaddr": csaddr,
                "qs": qs,
                "port": port,
                "service_uuid_str": service_uuid_str,
            }
            return True, context, None
        else:
            err = ctypes.GetLastError()
            return False, None, f"WSASetServiceW failed with error code {err}"
    except Exception as exc:
        return False, None, str(exc)


def unregister_rfcomm_sdp_service(reg_context):
    """
    Deregisters a previously published SDP record using WSASetServiceW with RNRSERVICE_DELETE (2).
    """
    if not reg_context or "qs" not in reg_context:
        return False, "Invalid registration context"
    try:
        ws2_32 = ctypes.windll.ws2_32
        WSASetServiceW = ws2_32.WSASetServiceW
        WSASetServiceW.argtypes = [ctypes.POINTER(_WSAQUERYSETW), ctypes.c_int, wintypes.DWORD]
        WSASetServiceW.restype = ctypes.c_int

        # RNRSERVICE_DELETE = 2
        qs = reg_context["qs"]
        ret = WSASetServiceW(ctypes.pointer(qs), 2, 0)
        if ret == 0:
            return True, None
        return False, f"WSASetServiceW unregister returned error code {ctypes.GetLastError()}"
    except Exception as exc:
        return False, str(exc)


# ─── Mock / Loopback Transport Constants ─────────────────────────────────────
MOCK_LOOPBACK_HOST = "127.0.0.1"
MOCK_DEFAULT_PORT = 14004

MOCK_DISCLAIMER_BANNER = """
******************************************************************************
* [SIMULATED LOCAL LOOPBACK - NOT ACTUAL BLUETOOTH RFCOMM METRICS]           *
* Notice: Running in local TCP/RAM loopback mode on a single machine.        *
* Throughput reflects memory/IPC bus speeds, NOT actual 2.4 GHz Bluetooth RF *
* air speed (~100 - 250 KB/s). Real over-the-air performance requires two   *
* physical devices with Bluetooth hardware transceivers.                     *
******************************************************************************
"""

