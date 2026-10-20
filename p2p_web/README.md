# P2P LAN File Share System (Web Version)

A high-speed, decentralized peer-to-peer file sharing and messaging platform designed for local area networks (LANs). Zero external servers, zero cloud dependency, and zero manual IP configuration.

---

## 👥 Project Team
* **Team Members:** Vidhi Agarwal (24), Pranad Bajoria (24), Arin Kulkarni (25), Kethana Reddy Tukiakula (25), Shreya Zanwar (25), Shivang Dixit (25)
* **Mentor:** Adith

---

## 🚀 Quick Setup (Run on BOTH Machines)

Both machines (Laptop A and Laptop B) must be connected to the **same Wi-Fi network** or mobile hotspot.

1. **Install Python dependencies:**
   ```bash
   pip install flask requests
   ```
2. **Start the application:**
   ```bash
   python app.py
   ```
3. **Console Output:**
   Each machine will print its hostname and local IP address, e.g.:
   ```text
   'DESKTOP-PC-A' running at http://192.168.1.15:5000
   ```
4. **Open in Web Browser:**
   Open the printed URL in your browser:
   * On PC-A: `http://localhost:5000` (or `http://192.168.1.15:5000`)
   * On PC-B: `http://localhost:5000` (or `http://192.168.1.28:5000`)

---

## ✨ Implemented Core Features

* **Zero-Config Discovery:** Automatic LAN detection via UDP broadcast beacons on port `9999` every 3 seconds.
* **Network Presence:** Heartbeat monitoring with automatic removal of stale peers after 8 seconds of silence.
* **Secure Handshake:** Pre-transfer authorization workflow; receiver is prompted with Accept/Reject before any chunk is transmitted.
* **Chunk-Based Parallel Transfers:** Files split into 512 KB chunks transferred concurrently via `ThreadPoolExecutor(max_workers=4)`.
* **Resume-on-Failure:** Interrupted transfers automatically resume from the last received chunk using disk-persisted JSON manifests.
* **Cryptographic Integrity:** End-to-end SHA-256 hash comparison on reassembled files to guarantee corruption-free delivery.
* **Transfer Dashboard:** Real-time speed (KB/s / MB/s), dynamic ETA estimates, and persistent transfer history (last 100 entries).
* **LAN Chat & Media:** Real-time peer-to-peer messaging, inline image sharing (up to 5 MB), and automated System event notifications.
* **Resource Cleanup:** Background daemon purging orphaned `.part` files (> 30 mins) and completed manifests (> 1 hour).

---

## 🔄 Side-by-Side Demo Guide (Sender vs Receiver)

| Step | Sender Action (PC-A) | Receiver Action (PC-B) | What is Happening Behind the Scenes |
|---|---|---|---|
| **1. Peer Discovery** | Start `app.py`. Under **Discovered peers**, see `PC-B` appear automatically within 2–3s. | Start `app.py`. Under **Discovered peers**, see `PC-A` appear automatically within 2–3s. | Both devices broadcast identity beacons over UDP port 9999. No manual IP entry required. |
| **2. Add File** | In **My shared files**, choose a test file (e.g. 20–50 MB) and click **Add to shared folder**. | Keep dashboard open. | File is saved locally in `uploads/` ready for transmission. |
| **3. Reject Handshake** | Select the file radio button and click **Send selected file here** next to PC-B. | Card appears in **Incoming transfer requests**. Click **Reject**. | Sender calls `POST /transfer-request`. PC-B rejects. Sender logs rejection; **zero bytes** written to PC-B. |
| **4. Accept & Transfer** | Click **Send selected file here** again. Watch live progress bar, speed (MB/s), and ETA. | Card appears again. Click **Accept**. | Sender spawns 4 parallel worker threads uploading 512 KB chunks concurrently to `POST /upload-chunk`. |
| **5. Integrity Check** | Progress bar reaches 100% and turns green with `✅ SHA-256 verified`. | File appears in **My shared files** with download link. | PC-B reassembles `.part` file, verifies SHA-256 hash against sender's checksum, and renames file atomically. |
| **6. Check History** | Scroll down to **Transfer history**: Blue `SENT` badge, size, duration, and green `SHA-256 Verified` badge. | Scroll down to **Transfer history**: Amber `RECEIVED` badge with matching transfer metrics. | Both machines append transfer record to `uploads/.transfer_history.json` under thread-safe file lock. |
| **7. Resume Demo** | Start sending a 50+ MB file. At ~30%, press `Ctrl+C` in PC-A terminal. Restart `app.py`, re-select file, and click Send. | Do not touch PC-B. Watch progress resume immediately from 30% to 100%! | PC-B auto-approves existing manifest. Sender checks `GET /transfer-status` and only uploads missing chunks. |
| **8. LAN Chat & Images** | Select PC-B in chat dropdown. Send a message or click 📷 to send an image (< 5 MB). | Message or photo renders inline inside chat feed immediately. | Direct peer-to-peer HTTP POST with sanitization via `werkzeug.utils.secure_filename`. |

---

## 🦈 Wireshark Packet Inspection Guide

Wireshark is only required on **ONE laptop** (PC-A is recommended).

### 1. Start Wireshark
1. Open Wireshark on PC-A.
2. Double-click your active **Wi-Fi** interface.

### 2. Useful Capture Filters

* **Discovery Beacons (UDP Broadcast):**
  ```wireshark
  udp.port == 9999
  ```
  *Shows periodic 3-second heartbeat packets containing JSON `{name, ip, port}` sent to broadcast address `255.255.255.255`.*

* **Concurrent Parallel Chunk Uploads:**
  ```wireshark
  http.request.method == "POST" && http contains "upload-chunk"
  ```
  *Shows overlapping TCP streams uploading different `chunk_index` numbers at the exact same millisecond.*

* **All Application Traffic:**
  ```wireshark
  udp.port == 9999 || tcp.port == 5000
  ```
  *Captures both discovery and data-plane transfers.*

---

## 📁 Technical Specifications

* **LAN Ports:** `5000` (TCP - Web UI, Transfer, Chat), `9999` (UDP - Discovery)
* **Bluetooth Transport:** Native Windows Winsock `AF_BTH` (address family 32), `BTPROTO_RFCOMM` (protocol 3)
* **RFCOMM Channel:** Channel 4 (configurable 1–30)
* **SDP Service Record:** Windows `WSASetServiceW` advertising service UUID `00001101-0000-1000-8000-00805F9B34FB` (SerialPort)
* **Chunk Size:** 512 KB (LAN HTTP) / 64 KB (Bluetooth RFCOMM frames with 4-byte big-endian framing)
* **Concurrency:** `ThreadPoolExecutor(max_workers=4)` for LAN parallel chunks; dedicated background threads for non-blocking Bluetooth sender/receiver
* **File Integrity:** End-to-end SHA-256 cryptographic hashing with quarantine on mismatch
* **Persistence:** JSON manifests & JSON transfer history (`uploads/.manifests/` & `uploads/.transfer_history.json`)

---

## 📶 Bluetooth Classic RFCOMM Mode

The application provides native Windows Bluetooth Classic RFCOMM transfer alongside LAN mode within the same web interface.

### Prerequisites (Before Testing with a Second Device)
1. **Windows 10/11 Operating System:** Native Winsock Bluetooth (`AF_BTH`) is built into the Windows networking stack.
2. **Bluetooth Radio Enabled:** Ensure Bluetooth is turned **ON** in **Windows Settings > Bluetooth & devices** on both machines.
3. **One-Time Hardware Pairing:**
   - On PC-A: Open Windows Settings > Bluetooth & devices > Add device.
   - Select PC-B from the list and pair them.
   - Verify the numeric PIN matches on both laptops.
   - The devices will now appear in the application's paired devices dropdown automatically via Windows Registry discovery (`SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices`).

### How to Use Bluetooth Mode in the Web Interface

1. **Launch the application on both laptops:**
   ```bash
   python app.py
   ```
   Open `http://localhost:5000` in the browser.

2. **Switch to Bluetooth Mode:**
   Click the **📶 Bluetooth RFCOMM Mode** tab in the navigation bar.

3. **Start the Receiver on PC-B (Receiver):**
   - Click **▶ Start Bluetooth Receiver**.
   - The status badge will switch to **LISTENING (ACTIVE)** on RFCOMM Channel 4.
   - An SDP service record (`WSASetServiceW`) is automatically registered so remote Bluetooth devices can discover the service.

4. **Send a File from PC-A (Sender):**
   - Under **Target Paired Device**, select PC-B from the dropdown (or enter PC-B's Bluetooth MAC address manually).
   - Under **File to Send**, choose an existing file from the shared folder or upload a new file.
   - Click **🚀 Send File over Bluetooth**.

5. **Monitor Live Transfer & Verification:**
   - Both laptops display real-time progress: percentage, bytes transferred, speed (KB/s and MB/s), network throughput (Mbps), and elapsed time.
   - Long-running transfers run asynchronously on background threads without blocking the Flask server or UI.
   - Upon completion, the receiver reassembles the file, computes its SHA-256 digest, and verifies it bit-for-bit against the sender's digest.
   - If verified, the badge turns green (`✅ Verified OK`) and the file is promoted to the shared folder.
   - If corrupted, the file is automatically quarantined as `.corrupt` to protect user data.

---

## 🧪 Single-Laptop Validation & Simulation Mode

For development, automated testing, or single-laptop grading when a second hardware laptop is not available:

1. **Web UI Simulation:**
   - In Bluetooth mode, check **"Simulation / Loopback Mode"**.
   - Start the receiver and click Send. The application executes the complete protocol (4-byte length framing, JSON handshake, metadata negotiation, chunked streaming, and SHA-256 verification) over a local TCP loopback bridge without hardware radios.
   - Notice banner: `⚠️ SIMULATED LOCAL LOOPBACK - NOT ACTUAL BLUETOOTH HARDWARE TRANSFER` is clearly displayed.

2. **Standalone 1-Command Loopback Demo:**
   ```bash
   python bt_poc/bt_local_demo.py
   ```
   Generates a 1 MB binary payload, executes all 4 transfer phases, verifies SHA-256 bit-for-bit, and prints a formatted audit report.

3. **Automated Unit Tests (29 Tests):**
   ```bash
   python -m unittest bt_poc/test_bt_poc.py -v
   ```
   Verifies protocol framing, JSON messaging, MAC address normalization, metadata validation, chunking, SHA-256 edge cases, DOS reserved name sanitization, directory traversal prevention, and corruption quarantining.

4. **Full Application Integration Suite:**
   ```bash
   python verify_app_integration.py
   ```
   Verifies web endpoints, dual-mode HTML serving, LAN routes, Bluetooth REST API, background receiver lifecycle, and simulated file transfer.

---

## ⚖️ Capability Matrix: Software-Verified vs Physical Hardware Requirements

| Feature / Capability | Software-Verified Status | Physical Hardware Requirement |
|---|---|---|
| **LAN UDP Zero-Config Discovery** | ✅ Verified in test suites & local network | Wi-Fi router / Mobile Hotspot connecting devices |
| **LAN Parallel Chunked Transfer** | ✅ Verified with resume manifests & SHA-256 | Same local network subnet |
| **LAN Peer-to-Peer Chat & Photos** | ✅ Verified with live polling | Same local network subnet |
| **Bluetooth Protocol Framing (4-byte length header)** | ✅ Verified in 29 unit tests | Software only |
| **Bluetooth JSON Handshake & Metadata** | ✅ Verified with PING/PONG & FILE_OFFER | Software only |
| **Path Traversal & DOS Name Sanitization** | ✅ Verified (`CON`, `NUL`, `AUX`, `..` stripped) | Software only |
| **SHA-256 Quarantine on Bit Corruption** | ✅ Verified (`.corrupt` file protection) | Software only |
| **SDP Service Registration (`WSASetServiceW`)** | ✅ Verified with Windows ws2_32 API | Windows Bluetooth Radio |
| **Paired Bluetooth Devices Registry Query** | ✅ Verified querying Windows registry | Windows Bluetooth Radio |
| **Physical RFCOMM Socket Transfer** | ✅ Fully implemented via `AF_BTH` / `BTPROTO_RFCOMM` | **Two physical Windows laptops with Bluetooth paired** |
| **Mobile / Android Transfer** | ℹ️ Protocol documented; stock Android lacks RFCOMM receiver | Requires custom Android companion app |
