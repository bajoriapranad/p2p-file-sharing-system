"""
bt_local_demo.py — Single-Laptop Automated End-to-End Loopback Validation Demo.
Part of the standalone Windows Bluetooth Classic RFCOMM Proof-of-Concept.

Usage:
    python bt_local_demo.py [--size-mb 1]

This script runs the complete receiver and sender protocol on a SINGLE Windows laptop
using local loopback stream transport, allowing full validation of:
- Length-prefixed protocol framing
- JSON metadata negotiation
- Chunked streaming engine
- SHA-256 cryptographic verification
- Transfer throughput metric calculations (KB/s, MB/s, Mbps)

NOTE: All throughput metrics in loopback mode are explicitly labeled as SIMULATED.
"""

import os
import sys
import time
import socket
import argparse
import threading

# Ensure current directory is on path
_HERE = os.path.dirname(os.path.abspath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

from bt_common import (
    MOCK_LOOPBACK_HOST,
    MOCK_DEFAULT_PORT,
    MOCK_DISCLAIMER_BANNER,
    format_bytes,
    compute_file_sha256,
)
from bt_receiver import run_receiver
from bt_sender import run_sender, select_or_create_file


def run_single_laptop_demo(size_mb=1):
    print("=" * 76)
    print("  BLUETOOTH PoC: SINGLE-LAPTOP END-TO-END VALIDATION DEMO")
    print("=" * 76)
    print(MOCK_DISCLAIMER_BANNER)

    # 1. Prepare sample file
    sample_file = os.path.join(_HERE, f"bt_test_{size_mb}mb.bin")
    if not os.path.isfile(sample_file):
        print(f"[*] Generating {size_mb} MB structured test payload: '{sample_file}'...")
        with open(sample_file, "wb") as f:
            for _ in range(size_mb * 16):
                f.write(os.urandom(65536))
        print("    [OK] Test file generated.")
    else:
        print(f"[*] Using existing test payload: '{sample_file}' ({format_bytes(os.path.getsize(sample_file))})")

    out_dir = os.path.join(_HERE, "received_files")
    os.makedirs(out_dir, exist_ok=True)

    receiver_res = {}

    def receiver_runner():
        res = run_receiver(
            output_dir=out_dir,
            auto_accept=True,
            loop_mode=False,
            mock=True,
            mock_port=MOCK_DEFAULT_PORT,
        )
        receiver_res["result"] = res

    # 2. Start Receiver in background thread
    print("\n[*] Starting receiver thread on local loopback transport...")
    receiver_thread = threading.Thread(target=receiver_runner, daemon=True)
    receiver_thread.start()

    # Allow receiver to bind
    time.sleep(0.5)

    # 3. Run Sender in main thread
    print("\n[*] Starting sender client on local loopback transport...")
    sender_success = False
    try:
        sender_success = run_sender(
            target_mac="LOOPBACK",
            filepath=sample_file,
            mock=True,
            mock_port=MOCK_DEFAULT_PORT,
        )
    except Exception as exc:
        print(f"\n[FATAL] Demo execution failed: {exc}")
        return False

    # Wait for receiver thread to cleanly finish
    receiver_thread.join(timeout=5.0)

    # 4. Hash and size verification
    source_sha = compute_file_sha256(sample_file)
    source_size = os.path.getsize(sample_file)

    rec_match, rec_path, rec_sha = receiver_res.get("result", (False, None, None))

    print("\n" + "=" * 76)
    print("  SINGLE-LAPTOP VALIDATION AUDIT REPORT")
    print("=" * 76)
    print(f"  Source File       : {sample_file}")
    print(f"  Source Size       : {source_size:,} bytes ({format_bytes(source_size)})")
    print(f"  Source SHA-256    : {source_sha}")
    print(f"  Received File     : {rec_path}")
    rec_size = os.path.getsize(rec_path) if rec_path and os.path.isfile(rec_path) else -1
    print(f"  Received Size     : {rec_size:,} bytes ({format_bytes(rec_size)})")
    print(f"  Received SHA-256  : {rec_sha}")
    print(f"  Sender Status     : {'SUCCESS' if sender_success else 'FAILED'}")
    print(f"  Receiver Status   : {'MATCH_VERIFIED' if rec_match else 'FAILED'}")

    hashes_match = (source_sha.lower() == str(rec_sha).lower() and source_size == rec_size)
    if hashes_match and sender_success:
        print("\n  [VERDICT] PASS: Received file is 100% bit-for-bit identical to source file.")
    else:
        print("\n  [VERDICT] FAIL: Hash or size discrepancy detected!")
    print("=" * 76 + "\n")

    return hashes_match and sender_success



def main():
    parser = argparse.ArgumentParser(description="Single-Laptop Loopback Bluetooth PoC Demo")
    parser.add_argument("--size-mb", type=int, default=1, help="Size of test file in MB (default: 1)")
    args = parser.parse_args()

    success = run_single_laptop_demo(size_mb=args.size_mb)
    sys.exit(0 if success else 1)


if __name__ == "__main__":
    main()
