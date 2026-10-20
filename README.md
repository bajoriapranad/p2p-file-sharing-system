# Peer-to-Peer (P2P) File Sharing System

A high-speed, decentralized peer-to-peer file sharing and messaging platform designed for local area networks (LANs) and Bluetooth communication. Zero external servers, zero cloud dependency, and zero manual IP configuration.

---

## 👥 Project Team
* **Team Members:** Vidhi Agarwal (24), Pranad Bajoria (24), Arin Kulkarni (25), Kethana Reddy Tukiakula (25), Shreya Zanwar (25), Shivang Dixit (25)
* **Mentor:** Adith

---

## 📁 Repository Structure

```text
.
├── p2p_web/                  # Primary P2P Web Application (Flask + Vanilla JS)
│   ├── app.py                # Main Flask backend & peer coordination logic
│   ├── bt_manager.py         # Bluetooth connection & transfer manager
│   ├── requirements.txt      # Python dependencies
│   ├── verify_app_integration.py # Integration validation suite
│   ├── static/               # CSS stylesheets & frontend JavaScript
│   ├── templates/            # HTML dashboard template
│   ├── uploads/              # Shared and received files workspace
│   ├── bt_poc/               # Bluetooth Proof of Concept & standalone scripts
│   │   ├── bt_sender.py      # Standalone Bluetooth sender
│   │   ├── bt_receiver.py    # Standalone Bluetooth receiver
│   │   ├── bt_common.py      # Shared Bluetooth protocol utilities
│   │   └── test_bt_poc.py    # Bluetooth test harness
│   └── README.md             # Detailed guide for the web application
└── README.md                 # Project overview and quick start
```

---

## 🚀 Quick Start (Web Application)

1. **Navigate to the web application directory:**
   ```bash
   cd p2p_web
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Start the application:**
   ```bash
   python app.py
   ```

4. **Open your browser:**
   Navigate to `http://localhost:5000` (or the IP printed in your console).

For in-depth documentation, demo walkthroughs, and architecture details, see [p2p_web/README.md](p2p_web/README.md).
