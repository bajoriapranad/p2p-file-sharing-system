# Windows Bluetooth Classic RFCOMM Proof-of-Concept & Validation Suite

This is an isolated, standalone proof-of-concept (PoC) located in `p2p_web/bt_poc/`. It demonstrates peer-to-peer file transfer using native Winsock **Bluetooth Classic RFCOMM (`AF_BTH`) sockets over 2.4 GHz radio waves** on Windows laptops, and includes a **single-laptop validation suite** for testing and demonstration without a second physical device.

---

## 🔒 Strict Scope Isolation Notice
* **Zero changes to existing LAN code:** `app.py`, `templates/index.html`, `static/script.js`, and `static/style.css` remain 100% untouched.
* **Zero network fallback:** The Bluetooth PoC does **not** use Wi-Fi, Ethernet, mobile hotspots, the internet, or Bluetooth PAN. All packets travel directly via length-prefixed RFCOMM frames.
* **Standard Python only:** Uses standard library modules (`socket`, `struct`, `hashlib`, `winreg`, `ctypes`, `unittest`). No third-party packages required.

---

## 🧪 Single-Laptop Validation Mode (No Second Device Needed)

Because a single Bluetooth controller cannot transmit baseband radio frames to its own MAC address, the PoC provides a local loopback transport mode. This tests the exact same framing, negotiation, streaming, integrity verification, and metric reporting logic without requiring a second laptop.

### 1. Automated Test Suite (29 Tests)
Executes 29 automated unit and reliability tests covering:
* Protocol framing (length-prefix encoding, fragmentation handling, EOF detection, 16 MB oversized frame protection)
* JSON message serialization and schema validation
* Metadata parsing (file offer structure, negative size rejection, malformed header handling)
* Chunked file streaming and buffer alignment (32 KB chunks)
* Empty file transfer (0 bytes) and small file transfer (< 1 chunk)
* SHA-256 calculation and single-bit tamper detection
* Interrupted connection handling & temporary `.part` file cleanup
* Corrupted payload detection & quarantine (`.corrupt` isolation)
* Filename sanitization (path traversal prevention, illegal character stripping, collision avoidance, Windows DOS device names like `CON`, `PRN`, `AUX`, `NUL`)
* Transfer metrics accuracy (elapsed time, KB/s, MB/s, Mbps)
* Winsock SDP service registration API via `ws2_32.dll`

**Command:**
```powershell
cd "p2p_web\bt_poc"
python -m unittest test_bt_poc.py -v
```

**Actual Execution Result:**
```text
Ran 29 tests in 0.212s
OK
```

---

### 2. Single-Laptop Automated End-to-End Demo (1 Command)
Runs receiver in a background thread and sender in the foreground over local loopback stream transport, transferring a 1 MB test file, verifying framing, metadata, progress, SHA-256 integrity, and printing full performance metrics:

**Command:**
```powershell
cd "p2p_web\bt_poc"
python bt_local_demo.py
```

**Actual Output:**
```text
============================================================================
  BLUETOOTH PoC: SINGLE-LAPTOP END-TO-END VALIDATION DEMO
============================================================================

******************************************************************************
* [SIMULATED LOCAL LOOPBACK - NOT ACTUAL BLUETOOTH RFCOMM METRICS]           *
* Notice: Running in local TCP/RAM loopback mode on a single machine.        *
* Throughput reflects memory/IPC bus speeds, NOT actual 2.4 GHz Bluetooth RF *
* air speed (~100 - 250 KB/s). Real over-the-air performance requires two   *
* physical devices with Bluetooth hardware transceivers.                     *
******************************************************************************

[*] Using existing test payload: 'C:\...\bt_poc\bt_test_1mb.bin' (1.00 MB)
[*] Starting receiver thread on local loopback transport...
[OK] Simulated loopback server listening on 127.0.0.1:14004.
[WAITING] Awaiting local sender connection...

[*] Starting sender client on local loopback transport...
[PREPARATION] Computing SHA-256 checksum of local file...
  SHA-256 Checksum : 1263e12ec9218e734bb97f758306ed87bff5edce25c215298e4d8f7671cfd9ec
[CONNECTING] Connecting to local loopback server 127.0.0.1:14004...
[CONNECTED] Local loopback stream established.

--- Phase 1: Test Message Handshake ---
  Sending test frame: 'HELLO_BLUETOOTH'...
  [PASS] Test message handshake exchanged successfully.

--- Phase 2: File Transfer Negotiation ---
  Offering file 'bt_test_1mb.bin' (1.00 MB) to receiver...
  [Mock Auto-Accept] Local simulation automatically approves transfer.
  [ACCEPTED] Receiving to temporary buffer: C:\...\received_files\bt_test_1mb_5.bin.part

--- Phase 3: Binary Chunk Reception ---
  [=========================] 100.0% | 1.00 MB / 1.00 MB | 333.20 MB/s (2795.07 Mbps) | 0.0s [MOCK]
  [OK] All chunks transmitted.
  [OK] All chunks received successfully.

--- Phase 4: Remote Integrity Verification ---
  Computing SHA-256 checksum on saved file...
  Expected SHA-256 : 1263e12ec9218e734bb97f758306ed87bff5edce25c215298e4d8f7671cfd9ec
  Actual SHA-256   : 1263e12ec9218e734bb97f758306ed87bff5edce25c215298e4d8f7671cfd9ec
  Result           : [VERIFIED] SHA-256 checksums match 100% -- Zero corruption.

============================================================================
  SINGLE-LAPTOP VALIDATION AUDIT REPORT
============================================================================
  Source File       : C:\...\p2p_web\bt_poc\bt_test_1mb.bin
  Source Size       : 1,048,576 bytes (1.00 MB)
  Source SHA-256    : 1263e12ec9218e734bb97f758306ed87bff5edce25c215298e4d8f7671cfd9ec
  Received File     : C:\...\p2p_web\bt_poc\received_files\bt_test_1mb_5.bin
  Received Size     : 1,048,576 bytes (1.00 MB)
  Received SHA-256  : 1263e12ec9218e734bb97f758306ed87bff5edce25c215298e4d8f7671cfd9ec
  Sender Status     : SUCCESS
  Receiver Status   : MATCH_VERIFIED

  [VERDICT] PASS: Received file is 100% bit-for-bit identical to source file.
============================================================================
```

---

### 3. Manual Dual-Terminal Loopback Test (Interactive)
To interactively test receiver and sender in separate terminal windows on the same machine:

**Terminal 1 (Receiver):**
```powershell
cd "p2p_web\bt_poc"
python bt_receiver.py --mock --yes
```

**Terminal 2 (Sender):**
```powershell
cd "p2p_web\bt_poc"
python bt_sender.py --mock
```

---

## 🔎 How to Inspect the Received File & Compare Hashes

After running any transfer, inspect the source and received files in PowerShell:

```powershell
# 1. Compare file sizes
Get-Item bt_test_1mb.bin | Select-Object Name, Length
Get-ChildItem received_files\bt_test_1mb*.bin | Select-Object Name, Length

# 2. Compare cryptographic SHA-256 hashes
Get-FileHash bt_test_1mb.bin -Algorithm SHA256
Get-ChildItem received_files\bt_test_1mb*.bin | Get-FileHash -Algorithm SHA256
```

Both files will display identical SHA-256 hashes, confirming bit-for-bit integrity.

---

## 📊 Performance Metrics & Simulation Disclaimer

All loopback / mock modes display prominent disclaimer banners:

```text
******************************************************************************
* [SIMULATED LOCAL LOOPBACK - NOT ACTUAL BLUETOOTH RFCOMM METRICS]           *
* Notice: Running in local TCP/RAM loopback mode on a single machine.        *
* Throughput reflects memory/IPC bus speeds, NOT actual 2.4 GHz Bluetooth RF *
* air speed (~100 - 250 KB/s). Real over-the-air performance requires two   *
* physical devices with Bluetooth hardware transceivers.                     *
******************************************************************************
```

### Metrics Formulae
* **Elapsed Time:** Transfer duration in seconds ($t = t_{\text{end}} - t_{\text{start}}$)
* **Bytes Transferred:** Exact byte count ($B$)
* **KB/s:** $B / (1024 \times t)$
* **MB/s:** $B / (1024 \times 1024 \times t)$
* **Mbps:** $(B \times 8) / (1,000,000 \times t)$

---

## 📡 Physical Two-Laptop RFCOMM Mode (For Two Windows Laptops)

When a second Windows laptop is available, the real over-the-air Bluetooth RFCOMM implementation is ready to run without code modification.

### Step 1: Pair the Laptops via Windows Settings (One-Time)
1. On **both laptops**, toggle Bluetooth **ON** in **Settings → Bluetooth & devices**.
2. On **Laptop A**: Click **"Add device"** → **"Bluetooth"** → select **Laptop B**.
3. Confirm the 6-digit PIN on both laptops to complete pairing.

### Step 2: Disable Wi-Fi and Hotspots
1. Turn **Wi-Fi OFF** on both laptops.
2. Disconnect any hotspots or ethernet cables.
3. Keep **Bluetooth ON** on both laptops.

### Step 3: Run the Receiver on Laptop B
```powershell
cd "p2p_web\bt_poc"
python bt_receiver.py
```
* Binds an `AF_BTH` RFCOMM socket (Channel 4 by default, with dynamic fallback 2–15).
* Registers the custom service UUID `{e2f6a91d-4f18-4b96-829b-8e3649a202c1}` in the Windows SDP database via Winsock `WSASetServiceW`.
* Displays its local Bluetooth MAC address (e.g. `28:C5:D2:27:1A:18`).

### Step 4: Run the Sender on Laptop A
```powershell
cd "p2p_web\bt_poc"
python bt_sender.py
```
* Reads paired devices from the Windows Registry (`HKLM\SYSTEM\CurrentControlSet\Services\BTHPORT\Parameters\Devices`) and presents a numbered selection menu.
* Select Laptop B from the list (or enter MAC manually).
* Choose an existing file or generate a 1 MB / 10 MB test file on the fly.
* Connects directly to Laptop B over `AF_BTH` RFCOMM and executes the 4-phase transfer over the 2.4 GHz radio.

---

## 🔍 Capability Matrix: Verified vs. Simulated vs. Unverified

| Component / Layer | Classification | Verification Method |
|---|---|---|
| **Protocol Framing (4-byte length prefix)** | **Verified** | Automated unit tests (`TestProtocolFraming`) |
| **Oversized Frame Protection (16 MB cap)** | **Verified** | Automated unit test (`test_oversized_frame_rejected`) |
| **JSON Metadata Negotiation (FILE_OFFER, approval)** | **Verified** | Automated unit tests + E2E demo |
| **Empty File (0 bytes) & Small File Handling** | **Verified** | Automated unit test (`test_empty_file_transfer`) |
| **Chunked Streaming Engine (32 KB bounded buffers)** | **Verified** | Automated unit tests (`TestChunkedFileHandling`) |
| **SHA-256 Integrity & Tamper Detection** | **Verified** | Unit tests with 1-bit mutation + live hashing |
| **Temporary File Cleanup (`.part` on disconnect)** | **Verified** | Automated unit test (`test_interrupted_connection_cleans_partial_file`) |
| **Corrupted Payload Quarantine (`.corrupt` isolation)** | **Verified** | Automated unit test (`test_corrupted_data_quarantined_not_promoted`) |
| **Filename Sanitization & Path Traversal Prevention** | **Verified** | Unit tests with `../../`, illegal characters, collisions |
| **Windows DOS Reserved Device Names (`CON`, `NUL`, etc.)** | **Verified** | Automated unit test (`test_dos_reserved_device_names_sanitized`) |
| **Transfer Statistics (Elapsed, KB/s, MB/s, Mbps)** | **Verified** | Unit tests against known transfer sizes |
| **Winsock SDP Service Registration (`WSASetServiceW`)** | **Verified** | Native `ws2_32.dll` API call returned code 0 (Success) |
| **Winsock `AF_BTH` Socket Creation & Binding** | **Verified** | Native Python socket binds RFCOMM channel cleanly |
| **Single-Laptop Transport** | **Simulated** | Local loopback stream (`127.0.0.1:14004`) |
| **Loopback Transfer Throughput** | **Simulated** | Local RAM/IPC bus throughput; explicitly disclaimed |
| **Physical 2.4 GHz RF Over-the-Air Transfer** | **Unverified** | **Requires 2nd physical Windows laptop** |
| **Physical RF Attenuation & Baseband Retries** | **Unverified** | **Requires 2nd physical Windows laptop** |
| **Physical Link Key Authentication across 2 Radios** | **Unverified** | **Requires 2nd physical Windows laptop** |
| **Actual Air Throughput Benchmarks (~100–250 KB/s)** | **Unverified** | **Requires 2nd physical Windows laptop** |
