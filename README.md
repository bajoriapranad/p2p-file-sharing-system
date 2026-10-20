# P2P LAN & Bluetooth File Sharing System using LAN/Wifi and Bluetooth

---

##  How It Works (Both Modes)

### 1. Wi-Fi / LAN Mode (High-Speed Parallel Transfer)
1. **Discovery:** Nodes automatically find each other by broadcasting UDP beacons containing their IP and port over port `9999` every 3 seconds.
2. **Handshake:** The sender sends an HTTP request (`POST /transfer-request`) offering the file name and size; the receiver clicks **Accept** or **Reject** on their dashboard.
3. **Parallel Transfer:** Upon acceptance, the file is sliced into 512 KB chunks and transferred concurrently across 4 worker threads to the receiver's `/upload-chunk` endpoint.
4. **Reassembly & Verification:** The receiver merges incoming chunks into a `.part` file, computes its SHA-256 hash, compares it with the sender's original checksum, and atomically renames the file upon successful match.

### 2. Bluetooth RFCOMM Mode (Direct Offline Transfer)
1. **Connection:** Nodes use pre-paired Windows Bluetooth radios. The receiver opens a native Winsock RFCOMM socket (`AF_BTH`) on Channel 4 and registers an SDP service record (`WSASetServiceW`).
2. **Handshake:** The sender connects to the receiver's Bluetooth MAC address and exchanges 4-byte length-prefixed JSON control frames (`FILE_OFFER` -> `ACCEPT`).
3. **Chunk Streaming:** The sender reads the file and streams binary frames in 64 KB chunks over the RFCOMM socket without requiring any Wi-Fi or router infrastructure.
4. **Integrity Check:** The receiver reads the incoming stream, writes to disk, verifies the SHA-256 digest upon completion, and quarantines the file as `.corrupt` if a checksum mismatch occurs.

---

## Features & Implementation

### 1. Zero-Configuration Peer Discovery
* **Implementation:** Built using UDP broadcast beacons over port `9999`. Nodes periodically announce their hostname, IP, and web port to `255.255.255.255`.
* **Heartbeat & Pruning:** Active peers are maintained in memory; stale peers that fail to send beacons within 8 seconds are automatically pruned.

### 2. Pre-Transfer Handshake & Authorization
* **Implementation:** Two-phase authorization flow via REST API.
* **Mechanism:** Sender triggers `POST /transfer-request`. The receiver is prompted with file details. Data transfer only starts after explicit user acceptance; rejected requests transmit zero payload bytes.

### 3. Chunk-Based Parallel File Transfer
* **Implementation:** Large files are partitioned into 512 KB chunks transferred simultaneously using Python's `ThreadPoolExecutor(max_workers=4)`.
* **Throughput Optimization:** Multi-threaded streaming maximizes network utilization and bypasses single-stream TCP throughput constraints over Wi-Fi.

### 4. Transfer Resumption & Crash Recovery
* **Implementation:** Progress is tracked via disk-backed JSON manifests stored in `.manifests/`.
* **Mechanism:** If interrupted mid-transfer, re-initiating the transfer reads the existing manifest and queries the receiver to skip already-delivered chunks, uploading only remaining parts.

### 5. Cryptographic Integrity Verification (SHA-256)
* **Implementation:** The sender computes an end-to-end SHA-256 digest before dispatching chunks.
* **Mechanism:** The receiver recalculates the full-file checksum upon reassembly. If hashes match, the file is finalized; if corrupted, it is quarantined as `.corrupt`.

### 6. Real-Time Transfer Metrics & Dynamic Dashboard
* **Implementation:** Web frontend polls transfer status endpoints every 500ms to calculate live transfer stats.
* **Metrics Tracked:** Instantaneous speeds (KB/s, MB/s, Mbps), percentage progress bars, dynamic ETA calculation, and a persistent 100-entry transfer history log (`uploads/.transfer_history.json`).

### 7. P2P Local Network Chat & Media Sharing
* **Implementation:** Direct peer-to-peer chat interface operating over HTTP REST routes with no central chat server.
* **Media Handling:** In-chat image sharing (up to 5 MB) with MIME-type checks and file sanitization via `werkzeug.utils.secure_filename`.

### 8. Native Windows Bluetooth Classic (RFCOMM) Transfer
* **Implementation:** Low-level Windows Winsock sockets (`AF_BTH = 32`, `BTPROTO_RFCOMM = 3`) over RFCOMM Channel 4.
* **Service Discovery Protocol (SDP):** Registers a Windows SDP service record via `WSASetServiceW` with SerialPort UUID `00001101-0000-1000-8000-00805F9B34FB`.
* **Device Discovery:** Reads paired Bluetooth devices directly from the Windows Registry (`SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices`).

### 9. Custom Protocol Framing
* **Implementation:** Prepends each socket message with a 4-byte big-endian frame length header (`struct.pack('>I', length)`).
* **Protocol Flow:** Supports structured JSON control messaging (`PING`, `PONG`, `FILE_OFFER`, `ACCEPT`, `REJECT`) followed by raw binary chunk streaming in 64 KB frames.

### 10. Security & Input Sanitization
* **Implementation:** Defensive sanitization against common attack vectors:
  * Strict path-traversal blocking by stripping relative paths (`../`).
  * Windows reserved DOS device name protection (`CON`, `PRN`, `AUX`, `NUL`, `COM1-9`, `LPT1-9`).
  * Background daemon cleaning up orphaned `.part` files older than 30 minutes.
