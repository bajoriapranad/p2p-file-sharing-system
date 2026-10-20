let selectedFile = null;
let currentPeers = {};

function log(msg) {
  const el = document.getElementById("status-log");
  const time = new Date().toLocaleTimeString();
  el.textContent = `[${time}] ${msg}\n` + el.textContent;
}

async function refreshPeers() {
  const res = await fetch("/peers");
  currentPeers = await res.json();
  const list = document.getElementById("peer-list");
  const ips = Object.keys(currentPeers);

  if (ips.length === 0) {
    list.innerHTML = '<li class="empty">Searching for peers on the LAN...</li>';
  } else {
    list.innerHTML = ips.map(ip => {
      const p = currentPeers[ip];
      return `<li>
        <span class="peer-name">${p.name}</span>
        <span class="peer-ip">${ip}:${p.port}</span>
        <button onclick="sendSelectedFile('${ip}', ${p.port})">Send selected file here</button>
      </li>`;
    }).join("");
  }

  // keep the chat peer dropdown in sync
  const chatSelect = document.getElementById("chat-peer");
  const prevValue = chatSelect.value;
  chatSelect.innerHTML = '<option value="">Select a peer...</option>' +
    ips.map(ip => `<option value="${ip}|${currentPeers[ip].port}">${currentPeers[ip].name} (${ip})</option>`).join("");
  if ([...chatSelect.options].some(o => o.value === prevValue)) chatSelect.value = prevValue;
}

function escapeHtml(str) {
  if (!str) return "";
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

async function refreshRequests() {
  try {
    const res = await fetch("/transfer-requests");
    if (!res.ok) return;
    const reqs = await res.json();
    const list = document.getElementById("request-list");
    if (!list) return;
    if (reqs.length === 0) {
      list.innerHTML = '<li class="empty">No pending requests.</li>';
      return;
    }
    list.innerHTML = reqs.map(r => {
      const mode = r.mode || "lan";
      const isBt = (mode === "bluetooth");
      const badgeHtml = isBt
        ? '<span class="badge badge-bt">📶 Bluetooth</span>'
        : '<span class="badge badge-lan">🌐 LAN</span>';

      const sizeNum = Number(r.total_size) || 0;
      const sizeStr = sizeNum >= 1024 * 1024
        ? `${(sizeNum / (1024 * 1024)).toFixed(2)} MB`
        : `${Math.ceil(sizeNum / 1024)} KB`;

      return `
        <li style="display:flex; justify-content:space-between; align-items:center; padding:12px 14px; border-left:4px solid ${isBt ? '#0284c7' : '#16a34a'}; margin-bottom:8px; background:#ffffff; border-radius:6px; box-shadow:0 1px 3px rgba(0,0,0,0.06); gap:12px;">
          <div style="display:flex; flex-direction:column; gap:4px; min-width:0;">
            <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
              ${badgeHtml}
              <strong style="font-size:14px; color:#0f172a; word-break:break-all;">${escapeHtml(r.filename)}</strong>
              <span style="font-size:12px; color:#64748b;">(${sizeStr})</span>
            </div>
            <span style="font-size:12px; color:#475569;">From: <strong>${escapeHtml(r.sender_name)}</strong></span>
          </div>
          <div style="display:flex; gap:8px; flex-shrink:0;">
            <button type="button" class="accept-btn" onclick="respondRequest('${r.transfer_id}', true)">✅ Accept</button>
            <button type="button" class="reject-btn" onclick="respondRequest('${r.transfer_id}', false)">❌ Reject</button>
          </div>
        </li>`;
    }).join("");
  } catch (err) {
    console.error("Error fetching transfer requests:", err);
  }
}

async function respondRequest(transferId, accept) {
  try {
    const res = await fetch(`/transfer-request/${transferId}/respond`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ accept }),
    });
    const data = await res.json();
    log(accept ? "✅ Accepted incoming transfer request." : "❌ Rejected incoming transfer request.");
    refreshRequests();
    refreshBtStatus();
  } catch (err) {
    log(`Error responding to transfer request: ${err}`);
  }
}

async function toggleAutoAccept(enabled) {
  try {
    const res = await fetch("/toggle-auto-accept", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ auto_accept: enabled })
    });
    const data = await res.json();
    const isNow = Boolean(data.auto_accept);
    log(isNow ? "⚡ Enabled auto-accept for all incoming transfers (LAN & Bluetooth)" : "🔒 Disabled auto-accept (manual Accept / Reject required)");
  } catch (e) {
    console.error("Error toggling auto-accept:", e);
  }
}

async function initAutoAcceptState() {
  try {
    const res = await fetch("/auto-accept-status");
    if (!res.ok) return;
    const data = await res.json();
    const toggle = document.getElementById("auto-accept-toggle");
    if (toggle) toggle.checked = Boolean(data.auto_accept);
  } catch (e) {
    // Ignore error
  }
}

function selectFile(name) {
  selectedFile = name;
  const items = document.querySelectorAll("#file-list .file-item");
  items.forEach(el => {
    if (el.dataset.filename === name) {
      el.classList.add("selected");
      const radio = el.querySelector('input[type="radio"]');
      if (radio) radio.checked = true;
    } else {
      el.classList.remove("selected");
    }
  });
  log(`Selected file for transfer: ${name}`);
}

async function deleteFile(name) {
  if (!confirm(`Are you sure you want to remove "${name}" from your shared folder?`)) {
    return;
  }
  try {
    const res = await fetch("/delete-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ filename: name }),
    });
    const result = await res.json();
    if (result.ok) {
      log(`Removed ${name} from shared folder`);
      if (selectedFile === name) {
        selectedFile = null;
      }
      refreshFiles();
    } else {
      log(`Error deleting file: ${result.error || "Failed"}`);
    }
  } catch (err) {
    log(`Network error deleting file: ${err}`);
  }
}

async function refreshFiles() {
  const res = await fetch("/files");
  const files = await res.json();
  const list = document.getElementById("file-list");
  if (!files || files.length === 0) {
    list.innerHTML = '<li class="empty">No files shared yet.</li>';
    selectedFile = null;
    if (typeof updateBtSharedFilesList === "function") updateBtSharedFilesList([]);
    return;
  }

  // Preserve selectedFile only if it still exists in files
  if (selectedFile && !files.includes(selectedFile)) {
    selectedFile = null;
  }

  list.innerHTML = "";
  files.forEach(name => {
    const isChecked = (name === selectedFile);
    const li = document.createElement("li");
    li.className = "file-item" + (isChecked ? " selected" : "");
    li.dataset.filename = name;

    const label = document.createElement("label");
    label.className = "file-label";

    const radio = document.createElement("input");
    radio.type = "radio";
    radio.name = "select-file";
    radio.value = name;
    radio.checked = isChecked;
    radio.addEventListener("change", () => {
      selectFile(name);
    });

    const nameSpan = document.createElement("span");
    nameSpan.className = "file-name";
    nameSpan.textContent = name;

    label.appendChild(radio);
    label.appendChild(nameSpan);

    const actions = document.createElement("div");
    actions.className = "file-actions";

    const downloadLink = document.createElement("a");
    downloadLink.href = `/download/${encodeURIComponent(name)}`;
    downloadLink.textContent = "download";
    downloadLink.className = "download-link";

    const deleteBtn = document.createElement("button");
    deleteBtn.type = "button";
    deleteBtn.className = "delete-btn";
    deleteBtn.textContent = "Delete";
    deleteBtn.title = `Delete ${name}`;
    deleteBtn.addEventListener("click", (e) => {
      e.stopPropagation();
      deleteFile(name);
    });

    actions.appendChild(downloadLink);
    actions.appendChild(deleteBtn);

    li.appendChild(label);
    li.appendChild(actions);
    list.appendChild(li);
  });
  if (typeof updateBtSharedFilesList === "function") {
    updateBtSharedFilesList(files);
  }
}

function setProgress(sent, total, label, speedStr = "", etaStr = "") {
  const pct = total > 0 ? Math.round((sent / total) * 100) : 0;
  document.getElementById("progress-fill").style.width = pct + "%";
  let extra = "";
  if (speedStr) extra += ` • ${speedStr}`;
  if (etaStr) extra += ` • ${etaStr}`;
  document.getElementById("progress-label").textContent = `${label} — ${sent}/${total} chunks (${pct}%)${extra}`;
}

async function pollProgress(transferId) {
  let announcedWaiting = false;
  let announcedResuming = false;
  const timer = setInterval(async () => {
    const res = await fetch(`/send-progress/${transferId}`);
    if (!res.ok) { clearInterval(timer); return; }
    const p = await res.json();

    if (p.waiting_for_approval) {
      if (!announcedWaiting) {
        log(`Waiting for ${p.filename} to be accepted on the receiving end...`);
        announcedWaiting = true;
      }
      document.getElementById("progress-label").textContent = `Waiting for approval — ${p.filename}`;
      return;
    }

    // Fast-path: file was already fully delivered in a previous session
    if (p.already_complete) {
      document.getElementById("progress-fill").style.width = "100%";
      document.getElementById("progress-fill").style.background = "#2563eb";
      document.getElementById("progress-label").textContent =
        `${p.filename} — already delivered to peer (100%)`;
      if (p.done) {
        if (p.match === true) {
          log(`✅ '${p.filename}' was already fully received by peer — SHA-256 verified`);
        } else {
          log(`✅ '${p.filename}' was already fully received by peer — no new chunks sent`);
        }
        refreshHistory();
        clearInterval(timer);
      }
      return;
    }

    // Show "Resuming" label when picking up a partial transfer
    if (!announcedResuming && p.initial_sent > 0 && !p.done) {
      log(`Resuming ${p.filename}: ${p.initial_sent}/${p.total} chunks already at peer, sending remaining...`);
      announcedResuming = true;
    }

    let speedStr = "";
    let etaStr = "";
    if (!p.done && p.elapsed && p.elapsed > 0.2 && p.sent_in_session !== undefined) {
      const chunkSize = p.chunk_size || 512 * 1024;
      const bytesSent = p.sent_in_session * chunkSize;
      const speedBps = bytesSent / p.elapsed;

      if (speedBps > 0) {
        if (speedBps >= 1024 * 1024) {
          speedStr = `${(speedBps / (1024 * 1024)).toFixed(2)} MB/s`;
        } else {
          speedStr = `${(speedBps / 1024).toFixed(1)} KB/s`;
        }

        const remainingChunks = Math.max(0, p.total - p.sent);
        const remainingBytes = remainingChunks * chunkSize;
        const remainingSec = Math.ceil(remainingBytes / speedBps);

        if (remainingChunks > 0 && remainingSec > 0) {
          if (remainingSec >= 60) {
            const mins = Math.floor(remainingSec / 60);
            const secs = remainingSec % 60;
            etaStr = `~${mins}m ${secs}s remaining`;
          } else {
            etaStr = `~${remainingSec}s remaining`;
          }
        }
      }
    }

    // Prefix label with "Resuming" when continuing a partial session
    const labelPrefix = (p.initial_sent > 0 && p.sent < p.total) ? "Resuming" : p.filename;
    const displayLabel = (p.initial_sent > 0 && p.sent < p.total)
      ? `Resuming ${p.filename}` : p.filename;
    setProgress(p.sent, p.total, displayLabel, speedStr, etaStr);

    if (p.error) {
      document.getElementById("progress-fill").style.background = "#dc2626";
      document.getElementById("progress-fill").style.width = "100%";
      document.getElementById("progress-label").textContent = `❌ ${p.error}`;
      log(`${p.error}${p.error.includes("rejected") ? "" : " — check network/firewall and try again"}`);
      clearInterval(timer);
      return;
    }
    if (p.done) {
      if (p.match === true) {
        log(`Finished sending ${p.filename} (${p.total} chunks, sent in parallel)`);
        log(`✅ SHA-256 verified — file integrity confirmed`);
        document.getElementById("progress-fill").style.background = "#16a34a";
      } else if (p.match === false) {
        log(`Finished sending ${p.filename} (${p.total} chunks, sent in parallel)`);
        log(`❌ SHA-256 mismatch — file may be corrupted`);
        document.getElementById("progress-fill").style.background = "#dc2626";
      } else {
        log(`Finished sending ${p.filename} (${p.total} chunks, sent in parallel)`);
      }
      refreshHistory();
      clearInterval(timer);
    }
  }, 400);
}

async function sendSelectedFile(peerIp, peerPort) {
  if (!selectedFile) {
    alert("Select a file from 'My shared files' first.");
    return;
  }
  document.getElementById("progress-fill").style.background = "#16a34a";
  log(`Starting chunked transfer of ${selectedFile} to ${peerIp}:${peerPort}...`);
  const form = new FormData();
  form.append("peer_ip", peerIp);
  form.append("peer_port", peerPort);
  form.append("filename", selectedFile);
  const res = await fetch("/send", { method: "POST", body: form });
  const result = await res.json();
  if (result.error) {
    log(`Error: ${result.error}`);
    return;
  }
  pollProgress(result.transfer_id);
}

document.getElementById("add-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const input = document.getElementById("file-input");
  if (!input.files.length) return;
  const form = new FormData();
  form.append("file", input.files[0]);
  const res = await fetch("/local-add", { method: "POST", body: form });
  const result = await res.json();
  if (result.filename) {
    log(`Added ${result.filename} to shared folder`);
    selectedFile = result.filename;
  }
  input.value = "";
  refreshFiles();
});

document.getElementById("chat-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const select = document.getElementById("chat-peer");
  const input = document.getElementById("chat-input");
  if (!select.value || !input.value.trim()) return;
  const [peerIp, peerPort] = select.value.split("|");
  await fetch("/chat/send", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ peer_ip: peerIp, peer_port: peerPort, message: input.value.trim() }),
  });
  input.value = "";
  refreshChat();
});

document.getElementById("chat-image-btn").addEventListener("click", () => {
  const select = document.getElementById("chat-peer");
  if (!select.value) {
    alert("Select a peer first to send an image.");
    return;
  }
  document.getElementById("chat-image-input").click();
});

document.getElementById("chat-image-input").addEventListener("change", async (e) => {
  const file = e.target.files[0];
  if (!file) return;

  const select = document.getElementById("chat-peer");
  if (!select.value) {
    alert("Select a peer first.");
    e.target.value = "";
    return;
  }

  if (file.size > 5 * 1024 * 1024) {
    alert("Image exceeds 5 MB limit. Please use the file transfer section for large files.");
    e.target.value = "";
    return;
  }

  const [peerIp, peerPort] = select.value.split("|");
  const formData = new FormData();
  formData.append("peer_ip", peerIp);
  formData.append("peer_port", peerPort);
  formData.append("image", file);

  log(`Sending image ${file.name} to ${peerIp}:${peerPort}...`);
  try {
    const res = await fetch("/chat/send-image", {
      method: "POST",
      body: formData,
    });
    const result = await res.json();
    if (result.error) {
      log(`Error sending image: ${result.error}`);
    } else {
      log(`Sent image ${file.name} to ${peerIp}:${peerPort}`);
    }
  } catch (err) {
    log(`Network error sending image: ${err}`);
  }
  e.target.value = "";
  refreshChat();
});

async function refreshChat() {
  const res = await fetch("/chat/messages");
  const messages = await res.json();
  const log = document.getElementById("chat-log");
  log.innerHTML = messages.map(m => {
    if (m.from === "System") {
      return `<div class="chat-msg system-msg"><span class="chat-time">${m.time}</span> ${m.message}</div>`;
    }
    if (m.type === "image" && m.image_url) {
      return `<div class="chat-msg">
        <strong>${m.from}</strong> <span class="chat-time">${m.time}</span><br>
        <a href="${m.image_url}" target="_blank">
          <img src="${m.image_url}" class="chat-img" alt="Shared image">
        </a>
      </div>`;
    }
    return `<div class="chat-msg"><strong>${m.from}</strong> <span class="chat-time">${m.time}</span><br>${m.message}</div>`;
  }).join("");
  log.scrollTop = log.scrollHeight;
}

async function refreshHistory() {
  const res = await fetch("/transfer-history");
  if (!res.ok) return;
  const items = await res.json();
  const list = document.getElementById("history-list");
  if (!items || items.length === 0) {
    list.innerHTML = '<li class="empty">No past transfers recorded.</li>';
    return;
  }

  list.innerHTML = items.map(item => {
    const dirClass = item.direction === "sent" ? "badge-sent" : "badge-recv";
    const dirLabel = item.direction === "sent" ? "Sent" : "Received";
    let matchBadge = "";
    if (item.sha256_match === true) {
      matchBadge = '<span class="badge badge-verified">SHA-256 Verified</span>';
    } else if (item.sha256_match === false) {
      matchBadge = '<span class="badge badge-mismatch">SHA-256 Mismatch</span>';
    }

    const sizeStr = item.file_size >= 1024 * 1024
      ? `${(item.file_size / (1024 * 1024)).toFixed(2)} MB`
      : `${(item.file_size / 1024).toFixed(0)} KB`;

    return `<li>
      <div class="history-main">
        <span class="badge ${dirClass}">${dirLabel}</span>
        <strong>${item.filename}</strong>
        <span class="history-meta">(${sizeStr} &middot; ${item.duration_seconds}s &middot; ${item.peer_name})</span>
        ${matchBadge}
      </div>
      <span class="history-time">${item.timestamp}</span>
    </li>`;
  }).join("");
}

// ─── Utility helpers ───────────────────────────────────────────────────────

function formatBytes(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  if (bytes >= 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`;
  if (bytes >= 1024 * 1024)        return `${(bytes / (1024 * 1024)).toFixed(2)} MB`;
  if (bytes >= 1024)               return `${(bytes / 1024).toFixed(1)} KB`;
  return `${bytes} B`;
}

function formatSpeed(bps) {
  if (!bps || bps <= 0) return '—';
  if (bps >= 1024 * 1024) return `${(bps / (1024 * 1024)).toFixed(2)} MB/s`;
  if (bps >= 1024)        return `${(bps / 1024).toFixed(1)} KB/s`;
  return `${bps.toFixed(0)} B/s`;
}

// ─── Analytics Dashboard ───────────────────────────────────────────────────

function renderDevicesTable(peerStats) {
  const tbody = document.getElementById('devices-tbody');
  if (!tbody) return;
  const entries = Object.entries(peerStats || {});
  if (!entries.length) {
    tbody.innerHTML = '<tr><td colspan="8" class="empty-row">No device data yet — complete a transfer first.</td></tr>';
    return;
  }
  tbody.innerHTML = entries.map(([ip, ps]) => {
    const diff = ps.net_diff_bytes || 0;
    const diffStr = diff >= 0 ? `+${formatBytes(diff)}` : `\u2212${formatBytes(Math.abs(diff))}`;
    const diffClass = diff >= 0 ? 'diff-positive' : 'diff-negative';
    const total = (ps.transfers_sent || 0) + (ps.transfers_received || 0);
    const isLive = Object.prototype.hasOwnProperty.call(currentPeers, ip);
    const statusBadge = isLive
      ? '<span class="badge badge-verified">\u25cf Online</span>'
      : '<span class="badge" style="background:#f1f5f9;color:#94a3b8">\u25cb Offline</span>';
    return `<tr>
      <td><strong>${ps.name || ip}</strong></td>
      <td class="mono">${ip}</td>
      <td>${statusBadge}</td>
      <td>${formatBytes(ps.bytes_sent)}</td>
      <td>${formatBytes(ps.bytes_received)}</td>
      <td class="${diffClass}">${diffStr}</td>
      <td>${total}</td>
      <td>${formatSpeed(ps.avg_speed_bps)}</td>
    </tr>`;
  }).join('');
}

function renderAnalyticsTable(history) {
  const tbody = document.getElementById('analytics-tbody');
  if (!tbody) return;
  if (!history || !history.length) {
    tbody.innerHTML = '<tr><td colspan="10" class="empty-row">No transfers recorded yet.</td></tr>';
    return;
  }

  // Compute best (minimum) duration per filename across all history
  const bestTimes = {};
  history.forEach(item => {
    const d = item.duration_seconds;
    if (!bestTimes[item.filename] || d < bestTimes[item.filename]) bestTimes[item.filename] = d;
  });

  tbody.innerHTML = history.map(item => {
    const speed = item.file_size / Math.max(0.1, item.duration_seconds);
    const dirClass = item.direction === 'sent' ? 'badge-sent' : 'badge-recv';
    const dirLabel = item.direction === 'sent' ? 'Sent' : 'Received';
    const best = bestTimes[item.filename];
    const isBest = item.duration_seconds === best;
    const bestCell = isBest
      ? `<span class="best-time">${best}s \u2B50</span>`
      : `${best}s`;
    const ext = item.file_ext || ('.' + (item.filename.split('.').pop() || '?'));
    const cat = item.file_category || 'Other';
    const sha = item.sha256_match === true
      ? '<span class="badge badge-verified">\u2705 OK</span>'
      : item.sha256_match === false
        ? '<span class="badge badge-mismatch">\u274C Fail</span>'
        : '<span class="badge" style="background:#f1f5f9;color:#94a3b8">\u2014</span>';
    return `<tr>
      <td class="filename-cell" title="${item.filename}">${item.filename}</td>
      <td><span class="file-type-badge">${cat}</span><span class="ext-label">${ext}</span></td>
      <td>${formatBytes(item.file_size)}</td>
      <td><span class="badge ${dirClass}">${dirLabel}</span></td>
      <td>${item.peer_name}</td>
      <td>${formatSpeed(speed)}</td>
      <td>${item.duration_seconds}s</td>
      <td>${bestCell}</td>
      <td>${formatBytes(item.file_size)}</td>
      <td>${sha}</td>
    </tr>`;
  }).join('');
}

async function refreshAnalytics() {
  try {
    const [statsRes, histRes] = await Promise.all([
      fetch('/stats'),
      fetch('/transfer-history'),
    ]);
    if (!statsRes.ok || !histRes.ok) return;
    const stats   = await statsRes.json();
    const history = await histRes.json();

    // Update network info + config cards
    const ni = stats.network_info || {};
    const setTxt = (id, v) => { const el = document.getElementById(id); if (el) el.textContent = v; };
    setTxt('net-type',    ni.type      || '\u2014');
    setTxt('net-iface',   ni.interface || '\u2014');
    setTxt('net-ip',      ni.ip        || '\u2014');
    setTxt('cfg-chunk',   stats.chunk_size_kb != null ? `${stats.chunk_size_kb} KB` : '\u2014');
    setTxt('cfg-workers', stats.parallel_workers ?? '\u2014');
    setTxt('cfg-peers',   stats.live_peer_count  ?? '\u2014');

    renderDevicesTable(stats.peer_stats);
    renderAnalyticsTable(history);
  } catch (e) {
    // silently ignore fetch errors during polling
  }
}

// ─── Initial calls & polling intervals ────────────────────────────────────
initAutoAcceptState();
refreshPeers();
refreshFiles();
refreshChat();
refreshRequests();
refreshHistory();
refreshAnalytics();
setInterval(refreshPeers,    2000);
setInterval(refreshFiles,    4000);
setInterval(refreshChat,     1500);
setInterval(refreshRequests, 1500);
setInterval(refreshHistory,  3000);
setInterval(refreshAnalytics, 5000);

// ─── Bluetooth Classic RFCOMM Handlers & Polling ──────────────────────────
let currentMode = "lan";
let lastBtState = "idle";

function switchMode(mode) {
  currentMode = mode;
  const lanTab = document.getElementById("tab-lan");
  const btTab = document.getElementById("tab-bt");
  const lanView = document.getElementById("lan-view");
  const btView = document.getElementById("bt-view");

  if (mode === "bt") {
    lanTab.classList.remove("active");
    btTab.classList.add("active");
    lanView.style.display = "none";
    btView.style.display = "block";
    refreshBtStatus();
    loadBtDevices();
  } else {
    btTab.classList.remove("active");
    lanTab.classList.add("active");
    btView.style.display = "none";
    lanView.style.display = "block";
  }
}

function updateBtSharedFilesList(files) {
  const sel = document.getElementById("bt-shared-file-select");
  if (sel) {
    const prevVal = sel.value;
    if (!files || files.length === 0) {
      sel.innerHTML = '<option value="">-- No files available in shared folder --</option>';
    } else {
      sel.innerHTML = files.map(f => `<option value="${f}">${f}</option>`).join("");
      if ([...sel.options].some(o => o.value === prevVal)) sel.value = prevVal;
    }
  }

  // Also update Bluetooth received list
  const recList = document.getElementById("bt-received-list");
  if (recList) {
    if (!files || files.length === 0) {
      recList.innerHTML = '<li class="empty">No received files yet.</li>';
    } else {
      recList.innerHTML = files.map(name => `
        <li class="file-item">
          <span class="file-name">${name}</span>
          <div class="file-actions">
            <a href="/download/${encodeURIComponent(name)}" class="download-link">download</a>
            <button type="button" class="delete-btn" onclick="deleteFile('${name}')">Delete</button>
          </div>
        </li>
      `).join("");
    }
  }
}

function onBtFileSourceChange() {
  const src = document.getElementById("bt-file-source").value;
  const sharedGroup = document.getElementById("bt-shared-file-group");
  const uploadGroup = document.getElementById("bt-upload-file-group");
  if (src === "upload") {
    sharedGroup.style.display = "none";
    uploadGroup.style.display = "block";
  } else {
    sharedGroup.style.display = "block";
    uploadGroup.style.display = "none";
  }
}

function onBtPeerSelectChange() {
  const sel = document.getElementById("bt-peer-select");
  const manualInput = document.getElementById("bt-target-mac");
  if (sel.value && sel.value !== "undefined") {
    manualInput.value = sel.value;
  }
}

async function loadBtDevices() {
  const sel = document.getElementById("bt-peer-select");
  if (!sel) return;
  try {
    const res = await fetch("/api/bt/devices");
    if (!res.ok) return;
    const data = await res.json();
    const devices = data.devices || [];
    if (devices.length === 0) {
      sel.innerHTML = '<option value="">-- No paired Bluetooth devices detected (Pair in Windows Settings) --</option>';
    } else {
      sel.innerHTML = '<option value="">-- Select a paired device --</option>' +
        devices.map(d => {
          const addr = d.mac || d.address;
          return `<option value="${addr}">${d.name} (${addr})</option>`;
        }).join("");
    }
  } catch (err) {
    console.error("Error loading paired BT devices:", err);
  }
}

async function startBtReceiver() {
  const channelInput = document.getElementById("bt-send-channel");
  const mockToggle = document.getElementById("bt-receiver-mock-toggle");
  const channel = channelInput ? parseInt(channelInput.value, 10) || 4 : 4;
  const mock = mockToggle ? mockToggle.checked : false;

  try {
    const res = await fetch("/api/bt/receiver/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ channel, mock })
    });
    const data = await res.json();
    if (data.ok) {
      log(`Bluetooth Receiver: ${data.message}`);
    } else {
      log(`Bluetooth Receiver error: ${data.error}`);
    }
    refreshBtStatus();
  } catch (err) {
    log(`Bluetooth Receiver start failed: ${err}`);
  }
}

async function stopBtReceiver() {
  try {
    const res = await fetch("/api/bt/receiver/stop", { method: "POST" });
    const data = await res.json();
    log(`Bluetooth Receiver: ${data.message || 'Stopped'}`);
    refreshBtStatus();
  } catch (err) {
    log(`Bluetooth Receiver stop failed: ${err}`);
  }
}

async function cancelBtTransfer() {
  try {
    const res = await fetch("/api/bt/cancel", { method: "POST" });
    const data = await res.json();
    log(`Bluetooth Transfer cancel: ${data.message}`);
    refreshBtStatus();
  } catch (err) {
    log(`Bluetooth cancel failed: ${err}`);
  }
}

async function sendBtFile() {
  const fileSource = document.getElementById("bt-file-source").value;
  let filename = "";

  if (fileSource === "upload") {
    const fileInput = document.getElementById("bt-upload-file-input");
    if (!fileInput.files || fileInput.files.length === 0) {
      alert("Please select a file to upload.");
      return;
    }
    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    log(`Uploading ${fileInput.files[0].name} to shared folder first...`);
    const uploadRes = await fetch("/add-file", { method: "POST", body: formData });
    const uploadData = await uploadRes.json();
    if (!uploadData.ok && !uploadData.filename) {
      alert("Failed to upload file: " + (uploadData.error || "Unknown error"));
      return;
    }
    filename = uploadData.filename;
    await refreshFiles();
  } else {
    filename = document.getElementById("bt-shared-file-select").value;
    if (!filename) {
      alert("Please select a file to send (or choose 'Upload New Local File' to pick one from your computer).");
      return;
    }
  }

  const peerSelect = document.getElementById("bt-peer-select");
  const manualInput = document.getElementById("bt-target-mac");
  let targetMac = (manualInput.value || peerSelect.value || "").trim();
  if (targetMac === "undefined") targetMac = "";

  const channel = parseInt(document.getElementById("bt-send-channel").value, 10) || 4;
  const mock = document.getElementById("bt-send-mock-toggle").checked;

  if (!mock && !targetMac) {
    alert("Please select a paired Bluetooth device or enter a target MAC address.");
    return;
  }

  log(`Initiating Bluetooth RFCOMM transfer: ${filename} -> ${mock ? '[MOCK LOOPBACK]' : targetMac}...`);

  try {
    const res = await fetch("/api/bt/send", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        target_mac: targetMac,
        filename: filename,
        channel: channel,
        mock: mock
      })
    });
    const data = await res.json();
    if (!data.ok) {
      log(`Bluetooth send failed: ${data.error}`);
      alert("Bluetooth send failed: " + data.error);
    } else {
      log(`Bluetooth transfer started: ${data.message}`);
      refreshBtStatus();
    }
  } catch (err) {
    log(`Error starting Bluetooth send: ${err}`);
  }
}

async function refreshBtStatus() {
  try {
    const res = await fetch("/api/bt/status");
    if (!res.ok) return;
    const s = await res.json();

    // 1. Adapter & Listener Badge
    const badge = document.getElementById("bt-status-badge");
    const startBtn = document.getElementById("bt-start-btn");
    const stopBtn = document.getElementById("bt-stop-btn");
    const msg = document.getElementById("bt-listener-message");

    if (badge && startBtn && stopBtn) {
      if (s.receiver && s.receiver.running) {
        badge.textContent = s.receiver.mock ? "Listening (Mock Mode)" : "Listening (Active)";
        badge.className = "badge badge-active";
        startBtn.style.display = "none";
        stopBtn.style.display = "inline-block";
        if (msg) {
          msg.textContent = s.receiver.mock
            ? "Mock loopback receiver is active for simulated testing."
            : `Listening on RFCOMM Channel ${s.receiver.channel}. Ready to accept paired connections.`;
        }
      } else {
        badge.textContent = s.adapter && s.adapter.available ? "Stopped (Idle)" : "Unavailable";
        badge.className = "badge badge-idle";
        startBtn.style.display = "inline-block";
        stopBtn.style.display = "none";
        if (msg) {
          msg.textContent = s.receiver && s.receiver.error
            ? `Receiver stopped with error: ${s.receiver.error}`
            : "Click 'Start Bluetooth Receiver' to listen for incoming RFCOMM connections from paired devices.";
        }
      }
    }

    // 2. Transfer Progress
    const tx = s.transfer || {};
    const pill = document.getElementById("bt-transfer-state-pill");
    const mockBanner = document.getElementById("bt-mock-warning");
    const fileEl = document.getElementById("bt-tx-file");
    const sizeEl = document.getElementById("bt-tx-size");
    const dirEl = document.getElementById("bt-tx-direction");
    const peerEl = document.getElementById("bt-tx-peer");
    const pctEl = document.getElementById("bt-progress-pct");
    const bytesEl = document.getElementById("bt-progress-bytes");
    const fillEl = document.getElementById("bt-progress-fill");
    const speedEl = document.getElementById("bt-metric-speed");
    const mbpsEl = document.getElementById("bt-metric-mbps");
    const timeEl = document.getElementById("bt-metric-time");
    const integEl = document.getElementById("bt-metric-integrity");
    const hashEl = document.getElementById("bt-hash-val");
    const cancelBtn = document.getElementById("bt-cancel-btn");
    const liveMsg = document.getElementById("bt-live-message");

    const state = (tx.state || tx.status || "idle").toLowerCase();

    if (pill) {
      if (state === "waiting_for_approval") {
        pill.textContent = "WAITING FOR APPROVAL";
        pill.className = "badge";
        pill.style.background = "#fef08a";
        pill.style.color = "#854d0e";
      } else {
        pill.style.background = "";
        pill.style.color = "";
        pill.textContent = state.toUpperCase();
        if (state === "transferring" || state === "streaming") {
          pill.className = "badge badge-transferring";
        } else if (state === "completed" || state === "success") {
          pill.className = "badge badge-completed";
        } else if (state === "failed" || state === "error") {
          pill.className = "badge badge-failed";
        } else if (state === "cancelled") {
          pill.className = "badge badge-cancelled";
        } else if (state === "connecting" || state === "negotiating" || state === "verifying") {
          pill.className = "badge badge-connecting";
        } else {
          pill.className = "badge badge-idle";
        }
      }
    }

    if (mockBanner) mockBanner.style.display = tx.mock ? "block" : "none";

    if (fileEl) fileEl.textContent = tx.filename || "—";
    if (sizeEl) sizeEl.textContent = tx.total_bytes ? formatBytes(tx.total_bytes) : "—";
    if (dirEl) dirEl.textContent = tx.direction ? tx.direction.toUpperCase() : "—";
    if (peerEl) peerEl.textContent = tx.peer_mac || "—";

    const pct = tx.percent != null ? tx.percent : 0;
    if (fillEl) fillEl.style.width = pct + "%";
    if (pctEl) pctEl.textContent = pct + "%";
    if (bytesEl) bytesEl.textContent = `${formatBytes(tx.transferred_bytes || 0)} / ${formatBytes(tx.total_bytes || 0)}`;

    if (speedEl) speedEl.textContent = `${(tx.speed_kbps || 0).toFixed(1)} KB/s`;
    if (mbpsEl) mbpsEl.textContent = `${(tx.mbps || 0).toFixed(2)} Mbps`;
    if (timeEl) timeEl.textContent = `${(tx.elapsed_seconds || 0).toFixed(1)}s`;

    if (integEl) {
      if (tx.sha256_match === true) {
        integEl.textContent = "✅ Verified OK";
        integEl.className = "metric-val badge badge-verified";
      } else if (tx.sha256_match === false) {
        integEl.textContent = "❌ Mismatch";
        integEl.className = "metric-val badge badge-mismatch";
      } else if (state === "verifying") {
        integEl.textContent = "⏳ Verifying...";
        integEl.className = "metric-val badge badge-connecting";
      } else {
        integEl.textContent = "—";
        integEl.className = "metric-val badge badge-idle";
      }
    }

    if (hashEl) hashEl.textContent = tx.sha256_hash || "Not yet computed";

    const isActive = (state === "connecting" || state === "negotiating" || state === "streaming" || state === "transferring" || state === "verifying" || state === "waiting_for_approval");
    if (cancelBtn) cancelBtn.style.display = isActive ? "inline-block" : "none";

    if (liveMsg) {
      if (tx.error) {
        liveMsg.textContent = `❌ ${tx.error}`;
        liveMsg.style.color = "#dc2626";
      } else if (state === "waiting_for_approval") {
        liveMsg.textContent = "⏳ Waiting for receiver to accept the incoming transfer request...";
        liveMsg.style.color = "#d97706";
      } else if (state === "completed" || state === "success") {
        liveMsg.textContent = `Transfer complete! SHA-256 match verified bit-for-bit.`;
        liveMsg.style.color = "#16a34a";
      } else if (state === "cancelled") {
        liveMsg.textContent = "Transfer was cancelled by user.";
        liveMsg.style.color = "#4b5563";
      } else if (isActive) {
        liveMsg.textContent = tx.mock ? "Local loopback simulation running..." : `Transferring ${tx.filename || 'file'} over Bluetooth...`;
        liveMsg.style.color = "#2563eb";
      } else {
        liveMsg.textContent = "Ready for transfer.";
        liveMsg.style.color = "#64748b";
      }
    }

    if (lastBtState !== "completed" && state === "completed") {
      refreshFiles();
      refreshHistory();
      refreshAnalytics();
    }
    lastBtState = state;
  } catch (err) {
    // ignore polling failures
  }
}

// Initial Bluetooth status fetch & background poll
refreshBtStatus();
setInterval(refreshBtStatus, 1500);
loadBtDevices();


