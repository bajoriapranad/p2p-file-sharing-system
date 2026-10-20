import os
import socket
import threading
import time
import hashlib
import json
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from werkzeug.utils import secure_filename
from flask import Flask, request, jsonify, render_template, send_from_directory

app = Flask(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
MANIFEST_DIR = os.path.join(UPLOAD_DIR, ".manifests")
CHAT_IMAGE_DIR = os.path.join(UPLOAD_DIR, "chat_images")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(MANIFEST_DIR, exist_ok=True)
os.makedirs(CHAT_IMAGE_DIR, exist_ok=True)

DEVICE_NAME = socket.gethostname()
DISCOVERY_PORT = 9999
WEB_PORT = 5000
PEER_TIMEOUT = 8
CHUNK_SIZE = 64 * 1024   # 64 KB per chunk (optimal for Wi-Fi LAN)
PARALLEL_WORKERS = 4
APPROVAL_TIMEOUT = 60  # seconds the sender waits for accept/reject before giving up
ORPHAN_PART_TIMEOUT = 30 * 60  # 30 minutes without chunk updates
STALE_MANIFEST_TIMEOUT = 60 * 60  # 1 hour after completion
CLEANUP_INTERVAL = 300  # run cleanup every 5 minutes
MAX_CHAT_IMAGE_SIZE = 5 * 1024 * 1024  # 5 MB max image size in chat
ALLOWED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
APP_START_TIME = time.time()

peers = {}                     # ip -> {name, port, last_seen}
manifests_lock = threading.Lock()
transfer_manifests = {}        # transfer_id -> manifest dict (receiver side)
send_progress = {}             # transfer_id -> progress dict (sender side)
chat_messages = []             # list of {from, message, time}
chat_lock = threading.Lock()

peer_stats = {}            # ip -> {name, bytes_sent, bytes_received, transfers_sent, transfers_received, speeds}
peer_stats_lock = threading.Lock()

HISTORY_FILE = os.path.join(UPLOAD_DIR, ".transfer_history.json")
history_lock = threading.Lock()

requests_lock = threading.Lock()
pending_requests = {}          # transfer_id -> {filename, total_size, sender_name, status, requested_at}
# status is one of: "pending", "accepted", "rejected"
AUTO_ACCEPT_TRANSFERS = False


def _file_category(filename):
    """Return (category, extension) tuple for a given filename."""
    ext = os.path.splitext(filename)[1].lower()
    if ext in {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.svg', '.ico', '.tiff'}:
        return 'Image', ext
    if ext in {'.mp4', '.mkv', '.avi', '.mov', '.wmv', '.flv', '.webm', '.m4v'}:
        return 'Video', ext
    if ext in {'.mp3', '.wav', '.flac', '.aac', '.ogg', '.m4a', '.wma'}:
        return 'Audio', ext
    if ext in {'.pdf', '.doc', '.docx', '.xls', '.xlsx', '.ppt', '.pptx', '.txt', '.odt', '.rtf', '.csv'}:
        return 'Document', ext
    if ext in {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2', '.xz', '.iso'}:
        return 'Archive', ext
    if ext in {'.py', '.js', '.ts', '.html', '.css', '.java', '.cpp', '.c', '.h', '.go', '.rs',
               '.php', '.rb', '.sh', '.bat', '.json', '.xml', '.yaml', '.yml', '.md', '.sql'}:
        return 'Code', ext
    return 'Other', ext if ext else '(none)'


def _update_peer_stats(peer_ip, peer_name, file_size, direction, duration_seconds):
    """Thread-safe update of per-peer cumulative transfer statistics."""
    if not peer_ip:
        return
    speed_bps = file_size / max(0.1, duration_seconds)
    with peer_stats_lock:
        if peer_ip not in peer_stats:
            peer_stats[peer_ip] = {
                "name": peer_name or peer_ip,
                "bytes_sent": 0,
                "bytes_received": 0,
                "transfers_sent": 0,
                "transfers_received": 0,
                "speeds": [],
            }
        ps = peer_stats[peer_ip]
        if peer_name:
            ps["name"] = peer_name
        if direction == "sent":
            ps["bytes_sent"] += file_size
            ps["transfers_sent"] += 1
        else:
            ps["bytes_received"] += file_size
            ps["transfers_received"] += 1
        ps["speeds"].append(round(speed_bps))
        ps["speeds"] = ps["speeds"][-20:]  # keep last 20 speed samples


def validate_peer_ip(endpoint_name):
    """Soft non-blocking peer IP validation.
    Checks client IP against discovered peers. Allows localhost and local IP,
    and grants a grace period so newly started peers are not prematurely blocked.
    Logs warnings for unverified IPs without breaking live classroom demos."""
    client_ip = request.remote_addr
    if client_ip in ("127.0.0.1", "::1", LOCAL_IP) or client_ip in peers:
        return True

    # Grace period during startup or network discovery propagation
    if time.time() - APP_START_TIME < 10:
        return True

    deadline = time.time() + 0.5
    while time.time() < deadline:
        if client_ip in peers:
            return True
        time.sleep(0.05)

    print(f"[Security Warning] {endpoint_name} accessed by unverified peer IP: {client_ip}")
    return True


def record_transfer_history(transfer_id, filename, file_size, direction, peer_name, peer_ip, duration_seconds, sha256_match):
    """Safely appends a completed transfer record to uploads/.transfer_history.json under history_lock,
    keeping a maximum of 100 entries (most recent first)."""
    file_category, file_ext = _file_category(filename)
    with history_lock:
        history = []
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f:
                    history = json.load(f)
                if not isinstance(history, list):
                    history = []
            except Exception:
                history = []
        entry = {
            "transfer_id": transfer_id,
            "filename": filename,
            "file_size": file_size,
            "direction": direction,
            "peer_name": peer_name,
            "peer_ip": peer_ip,
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "duration_seconds": round(max(0.1, duration_seconds), 1),
            "sha256_match": sha256_match,
            "file_ext": file_ext if file_ext else "(none)",
            "file_category": file_category,
        }
        history.insert(0, entry)
        history = history[:100]

        try:
            with open(HISTORY_FILE, "w") as f:
                json.dump(history, f, indent=2)
        except Exception as e:
            print(f"[History] Error saving transfer history: {e}")

    # Update per-peer stats outside history_lock to avoid lock nesting
    _update_peer_stats(peer_ip, peer_name, file_size, direction, duration_seconds)


def add_chat_message(from_name, message):
    """Helper to safely record chat messages, including System event notices."""
    with chat_lock:
        chat_messages.append({
            "from": from_name,
            "message": message,
            "time": time.strftime("%H:%M:%S"),
        })


def handle_incoming_bt_request(transfer_id, filename, total_size, sender_name, mode="bluetooth", timeout=60.0):
    """Called by bt_manager when an incoming Bluetooth file offer arrives.
    Registers the pending request in pending_requests and waits for human Accept/Reject."""
    decision_event = threading.Event()

    with requests_lock:
        pending_requests[transfer_id] = {
            "transfer_id": transfer_id,
            "filename": filename,
            "total_size": total_size,
            "sender_name": sender_name,
            "status": "pending",
            "requested_at": time.time(),
            "mode": mode,
            "_event": decision_event,
        }

    size_kb = math.ceil(int(total_size) / 1024) if total_size else 0
    add_chat_message("System", f"📁 Incoming Bluetooth transfer request from {sender_name}: '{filename}' ({size_kb} KB)")

    # Wait for the user to click Accept or Reject in the web UI
    answered = decision_event.wait(timeout=timeout)
    with requests_lock:
        req = pending_requests.get(transfer_id)
        if not answered:
            if req:
                req["status"] = "timed_out"
            add_chat_message("System", f"⚠️ Bluetooth transfer request for '{filename}' timed out waiting for approval")
            return False

        return bool(req is not None and req.get("status") == "accepted")


from bt_manager import BluetoothManager

bt_mgr = BluetoothManager(
    upload_dir=UPLOAD_DIR,
    history_recorder=record_transfer_history,
    chat_notifier=add_chat_message,
    request_handler=handle_incoming_bt_request,
    auto_accept_checker=lambda: AUTO_ACCEPT_TRANSFERS,
)


def get_local_ip():

    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        ip = s.getsockname()[0]
    except Exception:
        try:
            import psutil
            addrs = psutil.net_if_addrs()
            for name, addr_list in addrs.items():
                if any(k in name.lower() for k in ("wi-fi", "wifi", "wlan", "wireless", "ethernet")):
                    for a in addr_list:
                        if getattr(a, "family", None) == socket.AF_INET and not a.address.startswith(("127.", "169.254.")):
                            return a.address
        except Exception:
            pass
        ip = "127.0.0.1"
    finally:
        s.close()
    return ip


LOCAL_IP = get_local_ip()


def get_network_info():
    """Detect the network interface type (Wi-Fi / Ethernet / Bluetooth) for LOCAL_IP."""
    try:
        import psutil
        for iface, addrs in psutil.net_if_addrs().items():
            for addr in addrs:
                if getattr(addr, 'address', None) == LOCAL_IP:
                    name = iface.lower()
                    if 'bluetooth' in name:
                        ntype = 'Bluetooth (PAN)'
                    elif any(k in name for k in ('wi-fi', 'wifi', 'wlan', 'wireless', 'airport', 'wlp', 'wlo')):
                        ntype = 'Wi-Fi'
                    elif any(k in name for k in ('eth', 'ethernet', 'local area connection', 'enp', 'ens', 'eno')):
                        ntype = 'Ethernet'
                    else:
                        ntype = f'Other ({iface})'
                    return {'type': ntype, 'interface': iface, 'ip': LOCAL_IP, 'detected': True}
    except Exception:
        pass
    return {'type': 'Unknown', 'interface': 'N/A — run: pip install psutil', 'ip': LOCAL_IP, 'detected': False}


def _rebuild_peer_stats_from_history():
    """Reconstruct in-memory peer_stats from persisted history on startup."""
    if not os.path.exists(HISTORY_FILE):
        return
    try:
        with open(HISTORY_FILE, "r") as f:
            history = json.load(f)
        if not isinstance(history, list):
            return
        for entry in reversed(history):   # oldest first so most recent speed is captured last
            _update_peer_stats(
                peer_ip=entry.get("peer_ip", ""),
                peer_name=entry.get("peer_name", ""),
                file_size=entry.get("file_size", 0),
                direction=entry.get("direction", ""),
                duration_seconds=entry.get("duration_seconds", 1.0),
            )
        print(f"[Stats] Rebuilt peer stats from {len(history)} history records.")
    except Exception as e:
        print(f"[Stats] Could not rebuild peer stats: {e}")


def compute_sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            h.update(chunk)
    return h.hexdigest()


def transfer_id_for(filename, total_size):
    """Deterministic ID so re-sending the same file resumes the same transfer."""
    return hashlib.sha256(f"{filename}:{total_size}".encode()).hexdigest()[:16]


# ---------------- Discovery (unchanged) ----------------

def broadcast_presence():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    message = json.dumps({"name": DEVICE_NAME, "ip": LOCAL_IP, "port": WEB_PORT}).encode()
    while True:
        try:
            sock.sendto(message, ("<broadcast>", DISCOVERY_PORT))
        except OSError:
            pass
        time.sleep(3)


def listen_for_peers():
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind(("", DISCOVERY_PORT))
    while True:
        data, addr = sock.recvfrom(1024)
        try:
            info = json.loads(data.decode())
        except (ValueError, UnicodeDecodeError):
            continue
        physical_ip = addr[0] if addr and addr[0] else info.get("ip")
        if physical_ip == LOCAL_IP or info.get("ip") == LOCAL_IP:
            continue
        peer_ip = physical_ip if (physical_ip and not physical_ip.startswith("127.")) else info.get("ip")
        peers[peer_ip] = {
            "name": info.get("name", peer_ip),
            "port": info.get("port", WEB_PORT),
            "last_seen": time.time(),
        }


def cleanup_peers():
    while True:
        now = time.time()
        for ip in [ip for ip, p in peers.items() if now - p["last_seen"] > PEER_TIMEOUT]:
            del peers[ip]
        time.sleep(2)


def cleanup_transfers():
    """Periodically cleans up orphaned .part files and stale completed manifests.
    Runs in a background thread every CLEANUP_INTERVAL seconds."""
    while True:
        time.sleep(CLEANUP_INTERVAL)
        now = time.time()
        try:
            # 1. Clean up orphaned .part files that have had no chunk activity for ORPHAN_PART_TIMEOUT
            if os.path.exists(UPLOAD_DIR):
                for f in os.listdir(UPLOAD_DIR):
                    if not f.endswith(".part"):
                        continue
                    transfer_id = f[:-5]
                    part_path = os.path.join(UPLOAD_DIR, f)
                    mpath = manifest_file_path(transfer_id)

                    ref_time = None
                    if os.path.exists(mpath):
                        try:
                            ref_time = os.path.getmtime(mpath)
                        except OSError:
                            pass
                    elif os.path.exists(part_path):
                        try:
                            ref_time = os.path.getmtime(part_path)
                        except OSError:
                            pass

                    if ref_time and (now - ref_time > ORPHAN_PART_TIMEOUT):
                        with manifests_lock:
                            manifest = transfer_manifests.get(transfer_id)
                            if manifest and manifest.get("complete"):
                                continue
                            if os.path.exists(part_path):
                                try:
                                    os.remove(part_path)
                                except OSError:
                                    pass
                            if os.path.exists(mpath):
                                try:
                                    os.remove(mpath)
                                except OSError:
                                    pass
                            if transfer_id in transfer_manifests:
                                del transfer_manifests[transfer_id]
                        print(f"[Cleanup] Removed orphaned partial transfer: {f} (inactive > {ORPHAN_PART_TIMEOUT // 60}m)")

            # 2. Clean up stale completed manifests older than STALE_MANIFEST_TIMEOUT
            if os.path.exists(MANIFEST_DIR):
                for mf in os.listdir(MANIFEST_DIR):
                    if not mf.endswith(".json"):
                        continue
                    transfer_id = mf[:-5]
                    mpath = os.path.join(MANIFEST_DIR, mf)
                    try:
                        mtime = os.path.getmtime(mpath)
                    except OSError:
                        continue

                    if now - mtime > STALE_MANIFEST_TIMEOUT:
                        with manifests_lock:
                            try:
                                with open(mpath, "r") as fp:
                                    data = json.load(fp)
                                if data.get("complete"):
                                    os.remove(mpath)
                                    if transfer_id in transfer_manifests:
                                        del transfer_manifests[transfer_id]
                                    print(f"[Cleanup] Removed stale completed manifest: {mf} (age > {STALE_MANIFEST_TIMEOUT // 60}m)")
                            except Exception:
                                pass
        except Exception as e:
            print(f"[Cleanup] Error during maintenance scan: {e}")


# ---------------- Manifest helpers (receiver side chunk bookkeeping) ----------------

def manifest_file_path(transfer_id):
    return os.path.join(MANIFEST_DIR, transfer_id + ".json")


def get_or_create_manifest(transfer_id, filename, total_size, total_chunks, expected_sha256):
    with manifests_lock:
        if transfer_id in transfer_manifests:
            return transfer_manifests[transfer_id]
        path = manifest_file_path(transfer_id)
        if os.path.exists(path):
            with open(path) as f:
                saved = json.load(f)
            saved["received"] = set(saved["received"])
            if "created_at" not in saved:
                saved["created_at"] = os.path.getmtime(path)
        else:
            saved = {
                "filename": filename,
                "total_size": total_size,
                "total_chunks": total_chunks,
                "expected_sha256": expected_sha256,
                "received": set(),
                "complete": False,
                "created_at": time.time(),
            }
        saved["lock"] = threading.Lock()
        transfer_manifests[transfer_id] = saved
        return saved


def save_manifest_to_disk(transfer_id):
    data = transfer_manifests[transfer_id]
    to_save = {k: v for k, v in data.items() if k != "lock"}
    to_save["received"] = sorted(to_save["received"])
    with open(manifest_file_path(transfer_id), "w") as f:
        json.dump(to_save, f)


# ---------------- Web routes (control plane) ----------------

@app.route("/")
def index():
    bt_info = bt_mgr.get_adapter_info()
    return render_template("index.html", device_name=DEVICE_NAME, local_ip=LOCAL_IP, bt_info=bt_info)



@app.route("/peers")
def get_peers():
    return jsonify(peers)


@app.route("/files")
def list_files():
    files = [
        f for f in os.listdir(UPLOAD_DIR)
        if not f.startswith(".")
        and not f.endswith(".part")
        and os.path.isfile(os.path.join(UPLOAD_DIR, f))
    ]
    return jsonify(sorted(files))


@app.route("/download/<path:filename>")
def download(filename):
    return send_from_directory(UPLOAD_DIR, filename, as_attachment=True)


@app.route("/local-add", methods=["POST"])
@app.route("/add-file", methods=["POST"])
def local_add():
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "no file provided"}), 400
    safe_name = secure_filename(f.filename) or f.filename
    f.save(os.path.join(UPLOAD_DIR, safe_name))
    return jsonify({"ok": True, "filename": safe_name})


@app.route("/delete-file", methods=["POST"])
def delete_file():
    data = request.get_json(silent=True) or request.form
    filename = data.get("filename")
    if not filename:
        return jsonify({"error": "no filename provided"}), 400

    safe_name = os.path.basename(filename)
    filepath = os.path.join(UPLOAD_DIR, safe_name)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return jsonify({"error": "file not found"}), 404

    try:
        os.remove(filepath)
        return jsonify({"ok": True, "filename": safe_name})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ---------------- Accept / reject handshake ----------------
# A transfer must be explicitly accepted here before any chunk is written.
# If a manifest for this transfer_id already exists on disk (a previous,
# already-approved transfer that got interrupted), we auto-accept so that
# resume-after-restart still works without re-prompting the user.

@app.route("/transfer-request", methods=["POST"])
def transfer_request():
    validate_peer_ip("/transfer-request")
    data = request.get_json(force=True)
    transfer_id = data["transfer_id"]

    with requests_lock:
        # Check if we already have a manifest on disk (resumed or completed transfer)
        mpath = manifest_file_path(transfer_id)
        already_complete = False
        if os.path.exists(mpath):
            try:
                with open(mpath) as _f:
                    _saved = json.load(_f)
                already_complete = bool(_saved.get("complete"))
            except Exception:
                pass

        # If a prior entry exists and is already accepted, return it immediately
        # (allows the sender to poll for status without re-prompting).
        # But if it was rejected or is a stale pending from a crashed session,
        # reset it so the receiver gets a fresh prompt.
        if transfer_id in pending_requests:
            existing = pending_requests[transfer_id]
            if existing["status"] == "accepted":
                return jsonify({"status": "accepted", "already_complete": already_complete})
            # Rejected or hung-pending — fall through and create a new prompt below
            del pending_requests[transfer_id]

        already_in_progress = os.path.exists(mpath) and not already_complete
        filename = data.get("filename")
        total_size = data.get("total_size", 0)
        sender_name = data.get("sender_name", "unknown device")
        status = "accepted" if (already_in_progress or AUTO_ACCEPT_TRANSFERS) else "pending"

        pending_requests[transfer_id] = {
            "transfer_id": transfer_id,
            "filename": filename,
            "total_size": total_size,
            "sender_name": sender_name,
            "status": status,
            "requested_at": time.time(),
            "mode": "lan",
        }

        if status == "pending":
            size_kb = math.ceil(int(total_size) / 1024) if total_size else 0
            add_chat_message("System", f"📁 Incoming LAN transfer request from {sender_name}: '{filename}' ({size_kb} KB)")
        elif status == "accepted" and not already_in_progress and not already_complete:
            add_chat_message("System", f"⚡ Auto-accepted incoming LAN transfer of '{filename}' from {sender_name}")
        elif already_complete:
            # Auto-accept but inform sender it's already fully received
            add_chat_message("System", f"ℹ️ '{filename}' was already fully received — sender notified")

        return jsonify({"status": pending_requests[transfer_id]["status"], "already_complete": already_complete})


@app.route("/auto-accept-status")
def get_auto_accept_status():
    return jsonify({"auto_accept": AUTO_ACCEPT_TRANSFERS})


@app.route("/toggle-auto-accept", methods=["POST"])
def toggle_auto_accept():
    global AUTO_ACCEPT_TRANSFERS
    data = request.get_json(silent=True) or request.form or {}
    if "auto_accept" in data:
        AUTO_ACCEPT_TRANSFERS = bool(data["auto_accept"])
    else:
        AUTO_ACCEPT_TRANSFERS = not AUTO_ACCEPT_TRANSFERS
    return jsonify({"auto_accept": AUTO_ACCEPT_TRANSFERS})


@app.route("/transfer-requests")
def list_transfer_requests():
    """Polled by THIS machine's own browser to show incoming requests that
    still need a human decision."""
    with requests_lock:
        pending = [
            {
                "transfer_id": tid,
                "filename": info.get("filename"),
                "total_size": info.get("total_size", 0),
                "sender_name": info.get("sender_name", "unknown device"),
                "status": info.get("status"),
                "mode": info.get("mode", "lan"),
            }
            for tid, info in pending_requests.items()
            if info.get("status") == "pending"
        ]
    return jsonify(pending)


@app.route("/transfer-request/<transfer_id>/respond", methods=["POST"])
def respond_transfer_request(transfer_id):
    """Called by THIS machine's own browser when the user clicks accept/reject."""
    data = request.get_json(force=True)
    accept = bool(data.get("accept"))
    with requests_lock:
        if transfer_id not in pending_requests:
            return jsonify({"error": "unknown transfer_id"}), 404
        info = pending_requests[transfer_id]
        info["status"] = "accepted" if accept else "rejected"
        status = info["status"]
        filename = info.get("filename", "file")
        sender_name = info.get("sender_name", "peer")
        mode = info.get("mode", "lan")
        evt = info.get("_event")

    # If this request had a waiting thread (e.g. Bluetooth), unblock it immediately!
    if evt is not None:
        evt.set()

    proto_name = "Bluetooth" if mode == "bluetooth" else "LAN"
    if accept:
        add_chat_message("System", f"✅ You accepted incoming {proto_name} transfer of '{filename}' from {sender_name}")
    else:
        add_chat_message("System", f"❌ You rejected incoming {proto_name} transfer of '{filename}' from {sender_name}")

    return jsonify({"status": status})


@app.route("/transfer-request-status/<transfer_id>")
def transfer_request_status(transfer_id):
    """Polled by the SENDER (on the peer machine) to find out whether the
    human on the other end has accepted or rejected yet."""
    with requests_lock:
        info = pending_requests.get(transfer_id)
    return jsonify({"status": info["status"] if info else "unknown"})


def _is_accepted(transfer_id):
    with requests_lock:
        info = pending_requests.get(transfer_id)
    return bool(info and info["status"] == "accepted")


# ---------------- Chunked transfer: receiver side ----------------

@app.route("/transfer-status/<transfer_id>")
def transfer_status(transfer_id):
    """The sender calls this to find out which chunks the receiver already
    has for this transfer_id, so it only (re)sends what's missing. Requires
    the transfer to have been accepted first."""
    if not _is_accepted(transfer_id):
        return jsonify({"error": "transfer not accepted"}), 403

    filename = request.args.get("filename")
    total_size = int(request.args.get("total_size", 0))
    total_chunks = int(request.args.get("total_chunks", 0))
    expected_sha256 = request.args.get("sha256")

    manifest = get_or_create_manifest(transfer_id, filename, total_size, total_chunks, expected_sha256)
    return jsonify({
        "received": sorted(manifest["received"]),
        "total_chunks": manifest["total_chunks"],
        "complete": manifest["complete"],
        "match": manifest.get("match"),
        "already_complete": manifest["complete"],
    })


@app.route("/upload-chunk", methods=["POST"])
def upload_chunk():
    validate_peer_ip("/upload-chunk")
    transfer_id = request.form["transfer_id"]
    if not _is_accepted(transfer_id):
        return jsonify({"error": "transfer not accepted"}), 403

    filename = request.form["filename"]
    chunk_index = int(request.form["chunk_index"])
    total_chunks = int(request.form["total_chunks"])
    total_size = int(request.form["total_size"])
    expected_sha256 = request.form.get("sha256")
    chunk_data = request.files["chunk"].read()

    manifest = get_or_create_manifest(transfer_id, filename, total_size, total_chunks, expected_sha256)
    part_path = os.path.join(UPLOAD_DIR, transfer_id + ".part")

    with manifest["lock"]:
        if not os.path.exists(part_path):
            with open(part_path, "wb") as f:
                f.truncate(total_size)  # preallocate so out-of-order chunks can seek freely
        with open(part_path, "r+b") as f:
            f.seek(chunk_index * CHUNK_SIZE)
            f.write(chunk_data)

        manifest["received"].add(chunk_index)
        complete = len(manifest["received"]) >= total_chunks

        if complete and not manifest["complete"]:
            final_path = os.path.join(UPLOAD_DIR, filename)
            os.replace(part_path, final_path)
            actual_hash = compute_sha256(final_path)
            manifest["complete"] = True
            is_match = (actual_hash == expected_sha256) if expected_sha256 else None
            manifest["match"] = is_match
            manifest["actual_sha256"] = actual_hash

            if is_match is True:
                add_chat_message("System", f"✅ '{filename}' transfer complete — SHA-256 verified")
            elif is_match is False:
                add_chat_message("System", f"❌ '{filename}' transfer complete — SHA-256 MISMATCH detected")
            else:
                add_chat_message("System", f"'{filename}' transfer complete")

            # Phase 2b: record completed transfer in persistent history (receiver side)
            req_info = pending_requests.get(transfer_id, {})
            sender_peer_name = req_info.get("sender_name", "Peer")
            peer_ip = request.remote_addr
            transfer_duration = time.time() - manifest.get("created_at", time.time())
            record_transfer_history(
                transfer_id=transfer_id,
                filename=filename,
                file_size=total_size,
                direction="received",
                peer_name=sender_peer_name,
                peer_ip=peer_ip,
                duration_seconds=transfer_duration,
                sha256_match=is_match,
            )

        save_manifest_to_disk(transfer_id)

    return jsonify({
        "chunk_index": chunk_index,
        "received_count": len(manifest["received"]),
        "total_chunks": total_chunks,
        "complete": manifest["complete"],
        "match": manifest.get("match"),
    })


# ---------------- Chunked transfer: sender side ----------------

def _send_worker(transfer_id, peer_ip, peer_port, filepath, filename, chunk_index, total_chunks, total_size, sha256_hash):
    with open(filepath, "rb") as f:
        f.seek(chunk_index * CHUNK_SIZE)
        data = f.read(CHUNK_SIZE)
    resp = requests.post(
        f"http://{peer_ip}:{peer_port}/upload-chunk",
        data={
            "transfer_id": transfer_id,
            "filename": filename,
            "chunk_index": chunk_index,
            "total_chunks": total_chunks,
            "total_size": total_size,
            "sha256": sha256_hash,
        },
        files={"chunk": ("chunk", data)},
        timeout=60,
    )
    resp.raise_for_status()
    send_progress[transfer_id]["sent"].add(chunk_index)


def _run_transfer(transfer_id, peer_ip, peer_port, filepath, filename, total_size, sha256_hash):
    total_chunks = max(1, math.ceil(total_size / CHUNK_SIZE))
    # Always reset send_progress cleanly so a re-send of the same file starts fresh
    send_progress[transfer_id] = {
        "sent": set(), "total": total_chunks, "filename": filename,
        "done": False, "error": None, "waiting_for_approval": True,
        "match": None, "start_time": None, "initial_sent": 0,
        "already_complete": False,
    }

    # Step 1: ask the receiver's human to accept or reject before sending anything.
    try:
        req_resp = requests.post(
            f"http://{peer_ip}:{peer_port}/transfer-request",
            json={
                "transfer_id": transfer_id, "filename": filename, "total_size": total_size,
                "total_chunks": total_chunks, "sha256": sha256_hash, "sender_name": DEVICE_NAME,
            },
            timeout=10,
        )
        req_data = req_resp.json()
    except requests.RequestException as e:
        err_msg = f"Could not reach {peer_ip}:{peer_port}. Make sure friend's laptop has 'python app.py' running and Windows Firewall allows Port {peer_port}."
        send_progress[transfer_id].update({"done": True, "error": err_msg, "waiting_for_approval": False})
        add_chat_message("System", f"⚠️ {err_msg}")
        return

    # Fast-path: receiver reports the file is already fully received
    if req_data.get("already_complete"):
        send_progress[transfer_id].update({
            "waiting_for_approval": False,
            "sent": set(range(total_chunks)),
            "initial_sent": total_chunks,
            "start_time": time.time(),
            "already_complete": True,
        })
        # Fetch the stored integrity result from the receiver
        match_result = None
        try:
            fs = requests.get(
                f"http://{peer_ip}:{peer_port}/transfer-status/{transfer_id}",
                params={"filename": filename, "total_size": total_size,
                        "total_chunks": total_chunks, "sha256": sha256_hash},
                timeout=10,
            ).json()
            match_result = fs.get("match")
        except Exception:
            pass
        send_progress[transfer_id]["match"] = match_result
        send_progress[transfer_id]["done"] = True
        if match_result is True:
            add_chat_message("System", f"✅ '{filename}' was already fully received by peer — SHA-256 verified")
        else:
            add_chat_message("System", f"✅ '{filename}' was already fully received by peer (transfer was complete)")
        return

    deadline = time.time() + APPROVAL_TIMEOUT
    decision = None
    while time.time() < deadline:
        try:
            status = requests.get(
                f"http://{peer_ip}:{peer_port}/transfer-request-status/{transfer_id}", timeout=5
            ).json().get("status")
        except requests.RequestException:
            status = "unknown"
        if status in ("accepted", "rejected"):
            decision = status
            break
        time.sleep(1)

    send_progress[transfer_id]["waiting_for_approval"] = False

    if decision != "accepted":
        reason = "Receiver rejected the transfer" if decision == "rejected" else "Receiver did not respond in time"
        send_progress[transfer_id].update({"done": True, "error": reason})
        if decision == "rejected":
            add_chat_message("System", f"❌ Transfer of '{filename}' was rejected by receiver")
        else:
            add_chat_message("System", f"⚠️ Transfer of '{filename}' timed out waiting for approval")
        return

    # Step 2: accepted — check which chunks the receiver already has and send only missing ones.
    add_chat_message("System", f"✅ Transfer request for '{filename}' accepted by peer")

    status_data = requests.get(
        f"http://{peer_ip}:{peer_port}/transfer-status/{transfer_id}",
        params={"filename": filename, "total_size": total_size, "total_chunks": total_chunks, "sha256": sha256_hash},
        timeout=10,
    ).json()

    # Double-check: receiver may have completed the file between our request and now
    if status_data.get("already_complete") or status_data.get("complete"):
        already_have = set(range(total_chunks))
        missing = []
        add_chat_message("System", f"✅ '{filename}' is already complete on receiver — no chunks to send")
    else:
        already_have = set(status_data.get("received", []))
        missing = [i for i in range(total_chunks) if i not in already_have]
        skipped = len(already_have)
        remaining = len(missing)
        if skipped > 0:
            add_chat_message("System", f"⬆️ Resuming '{filename}': {skipped} chunks already received, sending {remaining} remaining...")
        else:
            add_chat_message("System", f"⬆️ Sending '{filename}' to {peer_ip}:{peer_port}...")

    send_progress[transfer_id]["sent"] = set(already_have)
    send_progress[transfer_id]["initial_sent"] = len(already_have)
    send_progress[transfer_id]["start_time"] = time.time()

    if missing:
        with ThreadPoolExecutor(max_workers=PARALLEL_WORKERS) as pool:
            futures = [
                pool.submit(_send_worker, transfer_id, peer_ip, peer_port, filepath, filename,
                            i, total_chunks, total_size, sha256_hash)
                for i in missing
            ]
            for fut in as_completed(futures):
                try:
                    fut.result()
                except Exception as e:
                    send_progress[transfer_id]["error"] = str(e)
                    add_chat_message("System", f"⚠️ Transfer of '{filename}' interrupted: {e} — click send to resume")

    # Step 3: check receiver's final status to retrieve SHA-256 integrity match result
    match_result = None
    try:
        final_status = requests.get(
            f"http://{peer_ip}:{peer_port}/transfer-status/{transfer_id}",
            params={"filename": filename, "total_size": total_size, "total_chunks": total_chunks, "sha256": sha256_hash},
            timeout=10,
        ).json()
        match_result = final_status.get("match")
    except Exception:
        pass

    send_progress[transfer_id]["match"] = match_result
    send_progress[transfer_id]["done"] = True

    if not send_progress[transfer_id].get("error"):
        duration = time.time() - send_progress[transfer_id].get("start_time", time.time())
        target_peer_name = peers.get(peer_ip, {}).get("name", peer_ip)

        # Phase 2b: record completed transfer in persistent history (sender side)
        record_transfer_history(
            transfer_id=transfer_id,
            filename=filename,
            file_size=total_size,
            direction="sent",
            peer_name=target_peer_name,
            peer_ip=peer_ip,
            duration_seconds=duration,
            sha256_match=match_result,
        )

        if match_result is True:
            add_chat_message("System", f"✅ Finished sending '{filename}' — SHA-256 verified by peer")
        elif match_result is False:
            add_chat_message("System", f"❌ Finished sending '{filename}' — SHA-256 MISMATCH reported by peer")
        else:
            add_chat_message("System", f"Finished sending '{filename}' ({total_chunks} chunks)")


@app.route("/send", methods=["POST"])
def send_file():
    peer_ip = request.form.get("peer_ip")
    peer_port = request.form.get("peer_port")
    filename = request.form.get("filename")

    filepath = os.path.join(UPLOAD_DIR, filename)
    if not os.path.exists(filepath):
        return jsonify({"error": "file not found on this machine"}), 404

    total_size = os.path.getsize(filepath)
    sha256_hash = compute_sha256(filepath)
    transfer_id = transfer_id_for(filename, total_size)

    # Pre-seed progress so the first JS poll never gets a 404 and bails out too early
    total_chunks = max(1, math.ceil(total_size / CHUNK_SIZE))
    send_progress[transfer_id] = {
        "sent": set(), "total": total_chunks, "filename": filename,
        "done": False, "error": None, "waiting_for_approval": True,
        "match": None, "start_time": None, "initial_sent": 0,
        "already_complete": False,
    }

    threading.Thread(
        target=_run_transfer,
        args=(transfer_id, peer_ip, peer_port, filepath, filename, total_size, sha256_hash),
        daemon=True,
    ).start()

    return jsonify({"transfer_id": transfer_id})


@app.route("/send-progress/<transfer_id>")
def get_send_progress(transfer_id):
    p = send_progress.get(transfer_id)
    if not p:
        return jsonify({"error": "unknown transfer"}), 404
    start_time = p.get("start_time")
    elapsed = time.time() - start_time if start_time else 0.0
    sent_in_session = len(p["sent"]) - p.get("initial_sent", 0)
    return jsonify({
        "sent": len(p["sent"]), "total": p["total"], "filename": p["filename"],
        "done": p["done"], "error": p["error"],
        "waiting_for_approval": p.get("waiting_for_approval", False),
        "match": p.get("match"),
        "elapsed": round(elapsed, 1),
        "sent_in_session": max(0, sent_in_session),
        "chunk_size": CHUNK_SIZE,
        "initial_sent": p.get("initial_sent", 0),
        "already_complete": p.get("already_complete", False),
    })


@app.route("/transfer-history")
def get_transfer_history():
    """Returns persistent transfer history (most recent first, capped at 100)."""
    with history_lock:
        if os.path.exists(HISTORY_FILE):
            try:
                with open(HISTORY_FILE, "r") as f:
                    return jsonify(json.load(f))
            except Exception:
                pass
        return jsonify([])


# ---------------- LAN chat ----------------

@app.route("/chat/receive", methods=["POST"])
def chat_receive():
    validate_peer_ip("/chat/receive")
    data = request.get_json(force=True)
    add_chat_message(data.get("from", "unknown"), data.get("message", ""))
    return jsonify({"ok": True})


@app.route("/chat/send", methods=["POST"])
def chat_send():
    data = request.get_json(force=True)
    peer_ip = data.get("peer_ip")
    peer_port = data.get("peer_port")
    message = data.get("message", "")
    add_chat_message("You", message)
    try:
        requests.post(
            f"http://{peer_ip}:{peer_port}/chat/receive",
            json={"from": DEVICE_NAME, "message": message},
            timeout=5,
        )
    except requests.RequestException as e:
        return jsonify({"error": str(e)}), 502
    return jsonify({"ok": True})


@app.route("/chat/send-image", methods=["POST"])
def chat_send_image():
    """Browser sends image to its own backend, which saves it and pushes to peer."""
    peer_ip = request.form.get("peer_ip")
    peer_port = request.form.get("peer_port")
    file = request.files.get("image")

    if not peer_ip or not peer_port:
        return jsonify({"error": "No peer selected"}), 400

    if not file or not file.filename:
        return jsonify({"error": "No image file provided"}), 400

    orig_filename = secure_filename(file.filename)
    if not orig_filename:
        orig_filename = f"image_{int(time.time())}.png"

    ext = os.path.splitext(orig_filename)[1].lower()
    if ext not in ALLOWED_IMAGE_EXTS:
        return jsonify({"error": f"Unsupported image format. Allowed: {', '.join(sorted(ALLOWED_IMAGE_EXTS))}"}), 400

    file_data = file.read()
    if len(file_data) > MAX_CHAT_IMAGE_SIZE:
        return jsonify({"error": f"Image exceeds 5 MB limit ({round(len(file_data) / (1024 * 1024), 2)} MB)"}), 400

    unique_name = f"img_{int(time.time() * 1000)}_{orig_filename}"
    local_path = os.path.join(CHAT_IMAGE_DIR, unique_name)
    with open(local_path, "wb") as f:
        f.write(file_data)

    with chat_lock:
        chat_messages.append({
            "from": "You",
            "message": "",
            "type": "image",
            "image_url": f"/chat-image/{unique_name}",
            "time": time.strftime("%H:%M:%S"),
        })

    # Forward to peer backend
    try:
        resp = requests.post(
            f"http://{peer_ip}:{peer_port}/chat/receive-image",
            data={"sender_name": DEVICE_NAME, "filename": unique_name},
            files={"image": (unique_name, file_data, file.content_type or "application/octet-stream")},
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return jsonify({"error": f"Could not deliver image to peer: {e}"}), 502

    return jsonify({"ok": True, "image_url": f"/chat-image/{unique_name}"})


@app.route("/chat/receive-image", methods=["POST"])
def chat_receive_image():
    """Receives image multipart push from peer backend."""
    validate_peer_ip("/chat/receive-image")
    file = request.files.get("image")
    if not file or not file.filename:
        return jsonify({"error": "No image file provided"}), 400

    file_data = file.read()
    if len(file_data) > MAX_CHAT_IMAGE_SIZE:
        return jsonify({"error": "Image exceeds 5 MB limit"}), 400

    raw_filename = request.form.get("filename") or file.filename
    safe_name = secure_filename(os.path.basename(raw_filename))
    if not safe_name:
        safe_name = f"img_{int(time.time() * 1000)}.png"

    local_path = os.path.join(CHAT_IMAGE_DIR, safe_name)
    with open(local_path, "wb") as f:
        f.write(file_data)

    sender_name = request.form.get("sender_name", "Peer")
    with chat_lock:
        chat_messages.append({
            "from": sender_name,
            "message": "",
            "type": "image",
            "image_url": f"/chat-image/{safe_name}",
            "time": time.strftime("%H:%M:%S"),
        })

    return jsonify({"ok": True})


@app.route("/chat-image/<path:filename>")
def get_chat_image(filename):
    """Safely serves inline chat images from uploads/chat_images/."""
    if ".." in filename or filename.startswith("/") or "\\" in filename:
        return jsonify({"error": "Invalid file path"}), 400
    safe_name = secure_filename(os.path.basename(filename))
    if not safe_name:
        return jsonify({"error": "Invalid file name"}), 400
    return send_from_directory(CHAT_IMAGE_DIR, safe_name)


@app.route("/chat/messages")
def chat_get_messages():
    with chat_lock:
        return jsonify(chat_messages[-100:])


@app.route("/stats")
def get_stats():
    """Returns per-peer cumulative transfer stats and network info for the Analytics Dashboard."""
    with peer_stats_lock:
        snapshot = {}
        for ip, ps in peer_stats.items():
            speeds = ps.get("speeds", [])
            avg_speed = sum(speeds) / len(speeds) if speeds else 0
            snapshot[ip] = {
                "name": ps["name"],
                "bytes_sent": ps["bytes_sent"],
                "bytes_received": ps["bytes_received"],
                "transfers_sent": ps["transfers_sent"],
                "transfers_received": ps["transfers_received"],
                "avg_speed_bps": round(avg_speed),
                "net_diff_bytes": ps["bytes_sent"] - ps["bytes_received"],
            }

    # Merge in currently live peers that have no transfers yet
    for ip, peer in peers.items():
        if ip not in snapshot:
            snapshot[ip] = {
                "name": peer["name"],
                "bytes_sent": 0,
                "bytes_received": 0,
                "transfers_sent": 0,
                "transfers_received": 0,
                "avg_speed_bps": 0,
                "net_diff_bytes": 0,
            }

    return jsonify({
        "peer_stats": snapshot,
        "network_info": get_network_info(),
        "chunk_size": CHUNK_SIZE,
        "chunk_size_kb": CHUNK_SIZE // 1024,
        "parallel_workers": PARALLEL_WORKERS,
        "live_peer_count": len(peers),
    })


# ---------------- Bluetooth RFCOMM Routes ----------------

@app.route("/api/bt/status")
def api_bt_status():
    """Returns Bluetooth adapter, listener, and active transfer state."""
    return jsonify(bt_mgr.get_status_snapshot())


@app.route("/api/bt/devices")
def api_bt_devices():
    """Returns paired Windows Bluetooth devices discovered via registry."""
    return jsonify({"devices": bt_mgr.get_paired_devices()})


@app.route("/api/bt/receiver/start", methods=["POST"])
def api_bt_receiver_start():
    data = request.get_json(silent=True) or request.form or {}
    channel = int(data.get("channel", 4))
    mock = bool(data.get("mock", False))
    mock_port = int(data.get("mock_port", 14004))
    ok, msg = bt_mgr.start_receiver(channel=channel, mock=mock, mock_port=mock_port)
    return jsonify({"ok": ok, "message": msg, "snapshot": bt_mgr.get_status_snapshot()}), (200 if ok else 400)


@app.route("/api/bt/receiver/stop", methods=["POST"])
def api_bt_receiver_stop():
    ok, msg = bt_mgr.stop_receiver()
    return jsonify({"ok": ok, "message": msg, "snapshot": bt_mgr.get_status_snapshot()})


@app.route("/api/bt/send", methods=["POST"])
def api_bt_send():
    """Initiates a background send transfer."""
    data = request.get_json(silent=True) or {}
    target_mac = request.form.get("target_mac") or data.get("target_mac")
    channel = int(request.form.get("channel") or data.get("channel", 4))
    mock_val = request.form.get("mock") or data.get("mock", False)
    mock = mock_val in (True, "true", "1", 1)
    mock_port = int(request.form.get("mock_port") or data.get("mock_port", 14004))

    # Case 1: File uploaded directly via multipart form
    if "file" in request.files and request.files["file"].filename:
        uploaded = request.files["file"]
        safe_name = secure_filename(uploaded.filename) or f"bt_upload_{int(time.time())}.bin"
        local_path = os.path.join(UPLOAD_DIR, safe_name)
        uploaded.save(local_path)
    # Case 2: Existing file in UPLOAD_DIR selected
    elif request.form.get("filename") or data.get("filename"):
        fname = request.form.get("filename") or data.get("filename")
        safe_name = os.path.basename(fname)
        local_path = os.path.join(UPLOAD_DIR, safe_name)
    else:
        return jsonify({"error": "No file specified or uploaded."}), 400

    if not os.path.isfile(local_path):
        return jsonify({"error": f"File '{safe_name}' not found in shared storage."}), 404

    if not mock and not target_mac:
        return jsonify({"error": "Target Bluetooth MAC address is required for physical transfer."}), 400

    ok, msg = bt_mgr.start_send_file(
        filepath=local_path,
        target_mac=target_mac,
        channel=channel,
        mock=mock,
        mock_port=mock_port,
    )
    return jsonify({"ok": ok, "message": msg}), (200 if ok else 400)


@app.route("/api/bt/cancel", methods=["POST"])
def api_bt_cancel():
    ok, msg = bt_mgr.cancel_active_transfer()
    return jsonify({"ok": ok, "message": msg})



if __name__ == "__main__":

    _rebuild_peer_stats_from_history()
    threading.Thread(target=broadcast_presence, daemon=True).start()
    threading.Thread(target=listen_for_peers, daemon=True).start()
    threading.Thread(target=cleanup_peers, daemon=True).start()
    threading.Thread(target=cleanup_transfers, daemon=True).start()

    # Auto-start Bluetooth receiver on Channel 4 if Bluetooth radio is detected
    try:
        adapter_info = bt_mgr.get_adapter_info()
        if adapter_info.get("available"):
            ok, msg = bt_mgr.start_receiver(channel=4)
            if ok:
                print(f"[Bluetooth] RFCOMM receiver auto-started on Channel 4 (MAC: {adapter_info.get('mac')})")
    except Exception as e:
        print(f"[Bluetooth] Note: Receiver not auto-started: {e}")

    print(f"'{DEVICE_NAME}' running at http://{LOCAL_IP}:{WEB_PORT}")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, debug=False)
