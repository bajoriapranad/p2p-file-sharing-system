"""
bt_receiver.py — Windows Bluetooth Classic RFCOMM Receiver / Server.
Part of the standalone Windows Bluetooth Classic RFCOMM Proof-of-Concept.

Usage:
    python bt_receiver.py [--channel 4] [--output ./received_files] [--yes]
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
    compute_file_sha256,
    format_bytes,
    format_speed,
    calculate_transfer_metrics,
    sanitize_filename,
    print_progress_bar,
    register_rfcomm_sdp_service,
    unregister_rfcomm_sdp_service,
    send_json,
    recv_json,
    MOCK_LOOPBACK_HOST,
    MOCK_DEFAULT_PORT,
    MOCK_DISCLAIMER_BANNER,
)


def bind_rfcomm_server(preferred_channel=DEFAULT_RFCOMM_CHANNEL):
    """
    Binds an RFCOMM listening socket to the local Bluetooth adapter.
    Tries preferred_channel first; if unavailable (WinError 10013 / WSAEACCES),
    dynamically probes alternative channels (2, 3, 5..15).
    """
    channels_to_try = [preferred_channel] + [c for c in range(2, 16) if c != preferred_channel]
    last_err = None
    for ch in channels_to_try:
        sock = socket.socket(AF_BTH, socket.SOCK_STREAM, BTPROTO_RFCOMM)
        try:
            # Bind to all local Bluetooth radios on specified RFCOMM channel
            sock.bind(("00:00:00:00:00:00", ch))
            sock.listen(1)
            actual_mac, actual_port = sock.getsockname()
            return sock, actual_mac, actual_port
        except Exception as e:
            last_err = e
            sock.close()
            continue

    raise RuntimeError(f"Could not bind to any RFCOMM channel (last error: {last_err}). Ensure Bluetooth is enabled in Windows Settings.")


def get_safe_destination_path(output_dir, filename):
    """Backwards-compatible wrapper around sanitize_filename."""
    return sanitize_filename(output_dir, filename)


def run_receiver(channel=DEFAULT_RFCOMM_CHANNEL, output_dir=None, auto_accept=False, loop_mode=False, mock=False, mock_port=MOCK_DEFAULT_PORT):
    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "received_files")

    hostname = socket.gethostname()
    sdp_context = None

    if mock:
        print(MOCK_DISCLAIMER_BANNER)
        print("=" * 70)
        print("  BLUETOOTH CLASSIC RECEIVER (SIMULATED LOCAL LOOPBACK MODE)")
        print("=" * 70)
        print(f"  Mode            : SIMULATED / LOCAL TEST (Single Laptop)")
        print(f"  Listen Address  : {MOCK_LOOPBACK_HOST}:{mock_port}")
        print(f"  Output Directory: {output_dir}")
        print("=" * 70)

        server_sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server_sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server_sock.bind((MOCK_LOOPBACK_HOST, mock_port))
        server_sock.listen(1)
        bound_mac = "127.0.0.1 (SIMULATED LOOPBACK)"
        bound_port = mock_port
        print(f"\n[OK] Simulated loopback server listening on {MOCK_LOOPBACK_HOST}:{bound_port}.")
        print("[WAITING] Awaiting local sender connection...")
    else:
        local_mac = get_local_bt_mac() or "UNKNOWN_MAC"
        print("=" * 70)
        print("  BLUETOOTH CLASSIC RFCOMM RECEIVER (SERVER)")
        print("=" * 70)
        print(f"  Mode            : PHYSICAL BLUETOOTH RFCOMM (Two Laptops)")
        print(f"  Device Hostname : {hostname}")
        print(f"  Bluetooth MAC   : {local_mac}")
        print(f"  Output Directory: {output_dir}")
        print("=" * 70)

        server_sock, bound_mac, bound_port = bind_rfcomm_server(channel)
        print(f"\n[OK] RFCOMM server listening successfully.")
        print(f"     Bound Channel : {bound_port}")
        print(f"     Adapter MAC   : {bound_mac}")

        # Register SDP Service in Windows
        sdp_ok, sdp_context, sdp_err = register_rfcomm_sdp_service(bound_port)
        if sdp_ok:
            print(f"     SDP Discovery : REGISTERED (Service advertised for remote clients)")
        else:
            print(f"     SDP Discovery : [WARNING] Registration failed ({sdp_err}). Remote client must specify channel {bound_port}.")

        print(f"\n--> Give this MAC address to the sender laptop: {bound_mac}")
        print(f"--> Ensure this laptop is set to 'Discoverable' in Windows Bluetooth settings.")
        print(f"\n[WAITING] Awaiting connection from sender over 2.4 GHz RF...")

    last_result = (False, None, None)
    try:
        while True:
            client_sock = None
            try:
                client_sock, client_addr = server_sock.accept()
                if mock:
                    client_desc = f"{client_addr[0]}:{client_addr[1]} [Simulated Local Loopback]"
                else:
                    client_mac = client_addr[0] if isinstance(client_addr, tuple) else str(client_addr)
                    client_desc = f"{client_mac} [Bluetooth RFCOMM Link]"
                print(f"\n[CONNECTED] Incoming link from: {client_desc}")

                client_sock.settimeout(20.0)

                # ─── Step 1: Short Test Message Handshake ────────────────────────
                print("\n--- Phase 1: Test Message Handshake ---")
                msg_in = recv_json(client_sock)
                if msg_in.get("type") != "PING":
                    raise ValueError(f"Expected PING frame, got: {msg_in}")
                print(f"  Sender sent test message: '{msg_in.get('msg')}'")

                pong_payload = {
                    "type": "PONG",
                    "msg": "PONG_BLUETOOTH",
                    "receiver_name": hostname,
                    "mock": mock,
                }
                send_json(client_sock, pong_payload)
                print("  [PASS] Test message handshake exchanged successfully.")

                # ─── Step 2: File Transfer Header & User Approval ───────────────
                print("\n--- Phase 2: File Transfer Negotiation ---")
                offer = recv_json(client_sock)
                if offer.get("type") != "FILE_OFFER":
                    raise ValueError(f"Expected FILE_OFFER frame, got: {offer}")

                # Validate metadata fields
                if "filename" not in offer or "filesize" not in offer or "sha256" not in offer:
                    send_json(client_sock, {"type": "FILE_RESPONSE", "accepted": False, "reason": "Malformed FILE_OFFER metadata"})
                    raise ValueError("Malformed FILE_OFFER: missing filename, filesize, or sha256.")

                filename = str(offer["filename"]).strip()
                if not filename:
                    filename = f"bt_received_{int(time.time())}.bin"

                try:
                    filesize = int(offer["filesize"])
                    if filesize < 0:
                        raise ValueError()
                except (ValueError, TypeError):
                    send_json(client_sock, {"type": "FILE_RESPONSE", "accepted": False, "reason": "Invalid filesize"})
                    raise ValueError(f"Invalid negative or non-integer filesize: {offer.get('filesize')}")

                expected_sha256 = str(offer["sha256"]).strip().lower()
                if len(expected_sha256) != 64 or not all(c in "0123456789abcdef" for c in expected_sha256):
                    send_json(client_sock, {"type": "FILE_RESPONSE", "accepted": False, "reason": "Invalid SHA-256 format (expected 64 hex characters)"})
                    raise ValueError(f"Invalid SHA-256 format: '{expected_sha256}'")

                chunk_size = int(offer.get("chunk_size", CHUNK_SIZE))
                if chunk_size <= 0:
                    chunk_size = CHUNK_SIZE

                print(f"  Incoming File    : {filename}")
                print(f"  File Size        : {format_bytes(filesize)} ({filesize:,} bytes)")
                print(f"  Expected SHA-256 : {expected_sha256}")

                # Prompt user for acceptance
                accepted = False
                if auto_accept or mock:
                    if mock:
                        print("  [Mock Auto-Accept] Local simulation automatically approves transfer.")
                    else:
                        print("  [Auto-Accept] --yes argument specified; auto-accepting.")
                    accepted = True
                else:
                    prompt_text = f"\n  Accept incoming file '{filename}' ({format_bytes(filesize)})? [Y/n]: "
                    choice = input(prompt_text).strip().lower()
                    accepted = choice in ("", "y", "yes")

                if not accepted:
                    print("  [REJECTED] Transfer declined by receiver.")
                    send_json(client_sock, {"type": "FILE_RESPONSE", "accepted": False, "reason": "User rejected transfer"})
                    client_sock.close()
                    if not loop_mode:
                        return False, None, None
                    continue

                dest_path = sanitize_filename(output_dir, filename)
                part_path = dest_path + ".part"
                send_json(client_sock, {"type": "FILE_RESPONSE", "accepted": True})
                print(f"  [ACCEPTED] Receiving to temporary buffer: {part_path}")

                # ─── Step 3: Bounded Chunk Reception ─────────────────────────────
                print("\n--- Phase 3: Binary Chunk Reception ---")
                client_sock.settimeout(30.0)
                bytes_received = 0
                start_time = time.time()
                last_time = start_time
                last_bytes = 0
                prog_label = "[MOCK]" if mock else "[BT]"

                try:
                    with open(part_path, "wb") as f_out:
                        while bytes_received < filesize:
                            remaining = filesize - bytes_received
                            read_target = min(chunk_size, remaining)
                            chunk = client_sock.recv(read_target)
                            if not chunk:
                                raise ConnectionError(f"Connection dropped by sender at byte {bytes_received:,} of {filesize:,}.")
                            f_out.write(chunk)
                            bytes_received += len(chunk)

                            if time.time() - last_time >= 0.25 or bytes_received == filesize:
                                print_progress_bar(bytes_received, filesize, start_time, last_bytes, last_time, label=prog_label)
                                last_bytes = bytes_received
                                last_time = time.time()
                except Exception as stream_err:
                    # Clean up temporary partial file if transmission interrupted
                    if os.path.exists(part_path):
                        try:
                            os.remove(part_path)
                        except Exception:
                            pass
                    raise stream_err

                elapsed = max(0.0001, time.time() - start_time)
                print("\n  [OK] All chunks received successfully.")

                # ─── Step 4: SHA-256 Cryptographic Verification ──────────────────
                print("\n--- Phase 4: Integrity Verification ---")
                print("  Computing SHA-256 checksum on saved file...")
                actual_sha256 = compute_file_sha256(part_path)
                sha256_match = (actual_sha256.lower() == expected_sha256.lower())

                print(f"  Expected SHA-256 : {expected_sha256}")
                print(f"  Actual SHA-256   : {actual_sha256}")

                if sha256_match:
                    print("  Result           : [VERIFIED] SHA-256 checksums match 100% -- Zero corruption.")
                    # Promote temporary .part file to final destination file
                    if os.path.exists(dest_path):
                        try:
                            os.remove(dest_path)
                        except Exception:
                            pass
                    os.replace(part_path, dest_path)
                    final_saved_path = dest_path
                else:
                    print("  Result           : [FAILED] SHA-256 MISMATCH -- File corrupted! Quarantining file.")
                    corrupt_path = dest_path + ".corrupt"
                    if os.path.exists(corrupt_path):
                        try:
                            os.remove(corrupt_path)
                        except Exception:
                            pass
                    os.replace(part_path, corrupt_path)
                    final_saved_path = corrupt_path

                # Transmit final verification frame to sender
                send_json(client_sock, {
                    "type": "TRANSFER_COMPLETE",
                    "match": sha256_match,
                    "receiver_sha256": actual_sha256,
                })

                # ─── Step 5: Performance Summary Report ──────────────────────────
                metrics = calculate_transfer_metrics(bytes_received, elapsed)

                print("\n" + "=" * 70)
                if mock:
                    print("  RECEIVER PERFORMANCE METRICS (SIMULATED LOCAL LOOPBACK)")
                    print("  * NOTICE: These are local RAM/loopback numbers, NOT Bluetooth speeds.")
                else:
                    print("  RECEIVER PERFORMANCE METRICS (PHYSICAL BLUETOOTH RFCOMM)")
                print("=" * 70)
                mode_str = "[SIMULATED LOCAL LOOPBACK - NOT ACTUAL BLUETOOTH]" if mock else "PHYSICAL BLUETOOTH RFCOMM"
                status_str = "SUCCESS" if sha256_match else "INTEGRITY_MISMATCH (FILE QUARANTINED)"
                print(f"  Transfer Mode     : {mode_str}")
                print(f"  Status            : {status_str}")
                print(f"  File Saved As     : {final_saved_path}")
                print(f"  Bytes Received    : {metrics['bytes_transferred']:,} bytes ({format_bytes(metrics['bytes_transferred'])})")
                print(f"  Elapsed Time      : {metrics['elapsed_seconds']:.2f} seconds")
                print(f"  Transfer Speed    : {metrics['kb_s']:.2f} KB/s  |  {metrics['mb_s']:.2f} MB/s")
                print(f"  Network Throughput: {metrics['mbps']:.2f} Mbps")
                if mock:
                    print("  Disclaimer        : Simulated results on single PC. Actual Bluetooth is ~100-250 KB/s.")
                print("=" * 70 + "\n")

                last_result = (sha256_match, final_saved_path, actual_sha256)

            except Exception as e:
                print(f"\n[ERROR] Receiver transfer error: {e}")
            finally:
                if client_sock:
                    try:
                        client_sock.close()
                    except Exception:
                        pass

            if not loop_mode:
                print("[SHUTDOWN] Exiting receiver after transfer (use --loop to keep running).")
                break
            print("\n[WAITING] Ready for next transfer...\n")

    finally:
        if sdp_context:
            unregister_rfcomm_sdp_service(sdp_context)
        server_sock.close()

    return last_result



def main():
    parser = argparse.ArgumentParser(description="Bluetooth Classic RFCOMM Receiver / Server (Windows)")
    parser.add_argument("--channel", type=int, default=DEFAULT_RFCOMM_CHANNEL, help="RFCOMM Channel to bind (default: 4)")
    parser.add_argument("--output", type=str, default=None, help="Output directory to save received files")
    parser.add_argument("--yes", "-y", action="store_true", help="Auto-accept incoming transfer offers without user prompt")
    parser.add_argument("--loop", "-l", action="store_true", help="Keep listening for subsequent transfers instead of exiting")
    parser.add_argument("--mock", "-m", action="store_true", help="Run in local loopback mock mode (single-laptop validation)")
    parser.add_argument("--mock-port", type=int, default=MOCK_DEFAULT_PORT, help="Port to bind for local loopback mode")
    args = parser.parse_args()

    run_receiver(
        channel=args.channel,
        output_dir=args.output,
        auto_accept=args.yes,
        loop_mode=args.loop,
        mock=args.mock,
        mock_port=args.mock_port,
    )


if __name__ == "__main__":
    main()

