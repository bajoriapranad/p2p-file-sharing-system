"""
bt_sender.py — Windows Bluetooth Classic RFCOMM Sender / Client.
Part of the standalone Windows Bluetooth Classic RFCOMM Proof-of-Concept.

Usage:
    python bt_sender.py [--target MAC_ADDRESS] [--file FILEPATH] [--channel 4]
"""

import os
import sys
import time
import socket
import argparse
from bt_common import (
    AF_BTH,
    BTPROTO_RFCOMM,
    DEFAULT_RFCOMM_CHANNEL,
    CHUNK_SIZE,
    get_local_bt_mac,
    list_paired_bt_devices,
    normalize_mac,
    compute_file_sha256,
    format_bytes,
    format_speed,
    calculate_transfer_metrics,
    print_progress_bar,
    send_exact,
    send_json,
    recv_json,
    MOCK_LOOPBACK_HOST,
    MOCK_DEFAULT_PORT,
    MOCK_DISCLAIMER_BANNER,
)


def select_target_device(cli_target=None):
    """
    Resolves the target Bluetooth MAC address:
    Uses CLI argument if provided, otherwise presents an interactive menu
    of paired Windows Bluetooth devices with manual entry option.
    """
    if cli_target:
        return normalize_mac(cli_target)

    print("\n--- Bluetooth Device Selection ---")
    paired = list_paired_bt_devices()
    if paired:
        print("Paired Windows Bluetooth Devices:")
        for idx, dev in enumerate(paired, 1):
            print(f"  [{idx}] {dev['name']} ({dev['mac']})")
        print("  [M] Enter MAC Address Manually")

        choice = input("\nSelect target device number or 'M': ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(paired):
            selected = paired[int(choice) - 1]["mac"]
            print(f"Selected: {paired[int(choice) - 1]['name']} ({selected})")
            return selected

    while True:
        raw = input("Enter target receiver's Bluetooth MAC address (e.g. 28:C5:D2:27:1A:18): ").strip()
        try:
            return normalize_mac(raw)
        except ValueError as err:
            print(f"  {err}")


def select_or_create_file(cli_file=None, auto_create_if_missing=False):
    """Resolves the file to send. If none provided, prompts user or creates a quick test payload."""
    if cli_file and os.path.isfile(cli_file):
        return os.path.abspath(cli_file)

    if cli_file and not os.path.isfile(cli_file):
        print(f"[WARNING] Specified file '{cli_file}' not found.")

    if auto_create_if_missing:
        test_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bt_test_1mb.bin")
        print(f"  Generating default 1 MB test payload: '{test_path}'...")
        with open(test_path, "wb") as f:
            for _ in range(16):
                f.write(os.urandom(65536))
        print("  [OK] Test file created.")
        return test_path

    while True:
        print("\n--- File to Transfer ---")
        print("  [1] Select an existing file from disk")
        print("  [2] Generate a quick 1 MB test file (bt_test_1mb.bin)")
        print("  [3] Generate a quick 10 MB test file (bt_test_10mb.bin)")
        choice = input("Choose option [1/2/3]: ").strip()

        if choice == "1":
            path = input("Enter full path to file: ").strip().strip('"').strip("'")
            if os.path.isfile(path):
                return os.path.abspath(path)
            print(f"  File not found: '{path}'")
        elif choice in ("2", "3"):
            size_mb = 1 if choice == "2" else 10
            test_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"bt_test_{size_mb}mb.bin")
            print(f"  Generating {size_mb} MB test payload: '{test_path}'...")
            with open(test_path, "wb") as f:
                for _ in range(size_mb * 16):
                    f.write(os.urandom(65536))
            print("  [OK] Test file created.")
            return test_path


def run_sender(target_mac=None, filepath=None, channel=DEFAULT_RFCOMM_CHANNEL, mock=False, mock_port=MOCK_DEFAULT_PORT):
    hostname = socket.gethostname()
    if filepath is None:
        filepath = select_or_create_file(None, auto_create_if_missing=mock)

    filename = os.path.basename(filepath)
    filesize = os.path.getsize(filepath)

    if mock:
        print(MOCK_DISCLAIMER_BANNER)
        print("=" * 70)
        print("  BLUETOOTH CLASSIC SENDER (SIMULATED LOCAL LOOPBACK MODE)")
        print("=" * 70)
        print(f"  Mode            : SIMULATED / LOCAL TEST (Single Laptop)")
        print(f"  Target Server   : {MOCK_LOOPBACK_HOST}:{mock_port}")
        print(f"  File to Send    : {filename} ({format_bytes(filesize)})")
        print("=" * 70)
    else:
        local_mac = get_local_bt_mac() or "UNKNOWN_MAC"
        print("=" * 70)
        print("  BLUETOOTH CLASSIC RFCOMM SENDER (CLIENT)")
        print("=" * 70)
        print(f"  Mode            : PHYSICAL BLUETOOTH RFCOMM (Two Laptops)")
        print(f"  Local Hostname  : {hostname}")
        print(f"  Local BT MAC    : {local_mac}")
        print(f"  Target BT MAC   : {target_mac}")
        print(f"  Target Channel  : {channel}")
        print(f"  File to Send    : {filename} ({format_bytes(filesize)})")
        print("=" * 70)

    # ─── Pre-Transfer Hash Calculation ───────────────────────────────────────
    print("\n[PREPARATION] Computing SHA-256 checksum of local file...")
    expected_sha256 = compute_file_sha256(filepath)
    print(f"  SHA-256 Checksum : {expected_sha256}")

    # ─── Establish Connection ────────────────────────────────────────────────
    if mock:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.settimeout(10.0)
        print(f"\n[CONNECTING] Connecting to local loopback server {MOCK_LOOPBACK_HOST}:{mock_port}...")
        try:
            sock.connect((MOCK_LOOPBACK_HOST, mock_port))
            print(f"[CONNECTED] Local loopback stream established.")
        except Exception as e:
            sock.close()
            raise ConnectionError(
                f"Failed to connect to loopback receiver at {MOCK_LOOPBACK_HOST}:{mock_port}: {e}\n"
                f"Ensure the receiver is running with '--mock' flag."
            )
    else:
        sock = socket.socket(AF_BTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
        sock.settimeout(20.0)
        print(f"\n[CONNECTING] Connecting to {target_mac} on RFCOMM channel {channel} over 2.4 GHz RF...")
        try:
            sock.connect((target_mac, channel))
            print(f"[CONNECTED] RFCOMM data link established with {target_mac}.")
        except Exception as e:
            sock.close()
            raise ConnectionError(
                f"Failed to connect to {target_mac} on channel {channel}: {e}\n"
                f"Troubleshooting checklist:\n"
                f"  1. Ensure receiver laptop has started 'python bt_receiver.py'.\n"
                f"  2. Verify both laptops have been paired in Windows Bluetooth settings.\n"
                f"  3. Check receiver's Bluetooth radio is turned ON and Discoverable."
            )

    try:
        # ─── Step 1: Short Test Message Handshake ────────────────────────────
        print("\n--- Phase 1: Test Message Handshake ---")
        ping_payload = {
            "type": "PING",
            "msg": "HELLO_BLUETOOTH",
            "sender_name": hostname,
            "mock": mock,
        }
        print("  Sending test frame: 'HELLO_BLUETOOTH'...")
        send_json(sock, ping_payload)

        pong_in = recv_json(sock)
        if pong_in.get("type") != "PONG":
            raise ValueError(f"Expected PONG response, received: {pong_in}")
        print(f"  Received handshake response: '{pong_in.get('msg')}' from receiver '{pong_in.get('receiver_name', 'Unknown')}'")
        print("  [PASS] Test message exchange succeeded.")

        # ─── Step 2: File Transfer Negotiation ───────────────────────────────
        print("\n--- Phase 2: File Transfer Negotiation ---")
        offer = {
            "type": "FILE_OFFER",
            "filename": filename,
            "filesize": filesize,
            "sha256": expected_sha256,
            "chunk_size": CHUNK_SIZE,
            "sender_name": hostname,
            "mock": mock,
        }
        print(f"  Offering file '{filename}' ({format_bytes(filesize)}) to receiver...")
        send_json(sock, offer)

        print("  [WAITING] Awaiting receiver's approval...")
        sock.settimeout(60.0)
        resp = recv_json(sock)

        if not resp.get("accepted"):
            reason = resp.get("reason", "Receiver declined the transfer.")
            print(f"  [ABORTED] Transfer rejected by receiver: {reason}")
            return False


        print("  [APPROVED] Receiver accepted transfer! Initiating stream...")

        # ─── Step 3: Bounded Chunk Streaming ─────────────────────────────────
        print("\n--- Phase 3: Binary Chunk Streaming ---")
        sock.settimeout(30.0)
        bytes_sent = 0
        start_time = time.time()
        last_time = start_time
        last_bytes = 0
        prog_label = "[MOCK]" if mock else "[BT]"

        with open(filepath, "rb") as f_in:
            while bytes_sent < filesize:
                chunk = f_in.read(CHUNK_SIZE)
                if not chunk:
                    break
                send_exact(sock, chunk)
                bytes_sent += len(chunk)

                if time.time() - last_time >= 0.25 or bytes_sent == filesize:
                    print_progress_bar(bytes_sent, filesize, start_time, last_bytes, last_time, label=prog_label)
                    last_bytes = bytes_sent
                    last_time = time.time()

        elapsed = max(0.0001, time.time() - start_time)
        print("\n  [OK] All chunks transmitted.")

        # ─── Step 4: Verification from Receiver ──────────────────────────────
        print("\n--- Phase 4: Remote Integrity Verification ---")
        print("  Waiting for receiver's SHA-256 calculation...")
        sock.settimeout(30.0)
        verify_resp = recv_json(sock)

        match = verify_resp.get("match", False)
        receiver_sha = verify_resp.get("receiver_sha256", "UNKNOWN")

        print(f"  Sender SHA-256   : {expected_sha256}")
        print(f"  Receiver SHA-256 : {receiver_sha}")

        if match:
            print("  Result           : [VERIFIED] Receiver confirmed SHA-256 match -- Zero corruption.")
        else:
            print("  Result           : [FAILED] Receiver reported SHA-256 mismatch -- File was corrupted.")

        # ─── Step 5: Performance Summary Report ──────────────────────────────
        metrics = calculate_transfer_metrics(bytes_sent, elapsed)

        print("\n" + "=" * 70)
        if mock:
            print("  SENDER PERFORMANCE METRICS (SIMULATED LOCAL LOOPBACK)")
            print("  * NOTICE: These are local RAM/loopback numbers, NOT Bluetooth speeds.")
        else:
            print("  SENDER PERFORMANCE METRICS (PHYSICAL BLUETOOTH RFCOMM)")
        print("=" * 70)
        mode_str = "[SIMULATED LOCAL LOOPBACK - NOT ACTUAL BLUETOOTH]" if mock else "PHYSICAL BLUETOOTH RFCOMM"
        target_str = f"{MOCK_LOOPBACK_HOST}:{mock_port} [Loopback]" if mock else str(target_mac)
        print(f"  Transfer Mode     : {mode_str}")
        print(f"  Status            : {'SUCCESS' if match else 'INTEGRITY_MISMATCH'}")
        print(f"  Target Endpoint   : {target_str}")
        print(f"  File Transferred  : {filename}")
        print(f"  Bytes Transmitted : {metrics['bytes_transferred']:,} bytes ({format_bytes(metrics['bytes_transferred'])})")
        print(f"  Elapsed Time      : {metrics['elapsed_seconds']:.2f} seconds")
        print(f"  Transfer Speed    : {metrics['kb_s']:.2f} KB/s  |  {metrics['mb_s']:.2f} MB/s")
        print(f"  Network Throughput: {metrics['mbps']:.2f} Mbps")
        if mock:
            print("  Disclaimer        : Simulated results on single PC. Actual Bluetooth is ~100-250 KB/s.")
        print("=" * 70 + "\n")
        return match

    finally:
        sock.close()


def main():
    parser = argparse.ArgumentParser(description="Bluetooth Classic RFCOMM Sender / Client (Windows)")
    parser.add_argument("--target", "-t", type=str, default=None, help="Target Bluetooth MAC address (e.g. 28:C5:D2:27:1A:18)")
    parser.add_argument("--file", "-f", type=str, default=None, help="Path to file to transmit")
    parser.add_argument("--channel", "-c", type=int, default=DEFAULT_RFCOMM_CHANNEL, help="RFCOMM Channel (default: 4)")
    parser.add_argument("--mock", "-m", action="store_true", help="Run in local loopback mock mode (single-laptop validation)")
    parser.add_argument("--mock-port", type=int, default=MOCK_DEFAULT_PORT, help="Port to connect for local loopback mode")
    args = parser.parse_args()

    if args.mock:
        filepath = select_or_create_file(args.file, auto_create_if_missing=True)
        success = run_sender(target_mac="LOOPBACK", filepath=filepath, mock=True, mock_port=args.mock_port)
    else:
        target_mac = select_target_device(args.target)
        filepath = select_or_create_file(args.file)
        success = run_sender(target_mac=target_mac, filepath=filepath, channel=args.channel, mock=False)

    sys.exit(0 if success else 1)



if __name__ == "__main__":
    main()

