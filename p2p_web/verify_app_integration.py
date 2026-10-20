import os
import sys
import time

from app import app, bt_mgr, UPLOAD_DIR

def run_checks():
    client = app.test_client()

    # 1. Test GET /
    res = client.get('/')
    assert res.status_code == 200, f'GET / failed: {res.status_code}'
    html = res.data.decode('utf-8')
    assert 'id="lan-view"' in html, 'lan-view missing from index.html'
    assert 'id="bt-view"' in html, 'bt-view missing from index.html'
    assert 'id="tab-lan"' in html, 'tab-lan missing from index.html'
    assert 'id="tab-bt"' in html, 'tab-bt missing from index.html'
    print('[PASS] GET / serves both LAN and Bluetooth views')

    # 2. Test LAN endpoints & Auto-accept toggle
    res = client.get('/peers')
    assert res.status_code == 200, f'GET /peers failed: {res.status_code}'
    res = client.get('/files')
    assert res.status_code == 200, f'GET /files failed: {res.status_code}'
    res = client.get('/transfer-history')
    assert res.status_code == 200, f'GET /transfer-history failed: {res.status_code}'
    res = client.get('/stats')
    assert res.status_code == 200, f'GET /stats failed: {res.status_code}'

    # 2a. Test Auto-accept toggle
    res = client.get('/auto-accept-status')
    assert res.status_code == 200 and res.get_json()['auto_accept'] is False, 'Default auto-accept should be False'
    res = client.post('/toggle-auto-accept', json={'auto_accept': True})
    assert res.get_json()['auto_accept'] is True
    res = client.post('/toggle-auto-accept', json={'auto_accept': False})
    assert res.get_json()['auto_accept'] is False
    print('[PASS] Auto-accept toggle API tested (default is False / manual approval required)')

    # 2b. Test LAN transfer request rejection
    res = client.post('/transfer-request', json={
        'transfer_id': 'test_lan_reject',
        'filename': 'sample.pdf',
        'total_size': 10240,
        'sender_name': 'Test Laptop A'
    })
    assert res.status_code == 200
    assert res.get_json()['status'] == 'pending', 'LAN request should be pending when auto-accept is off'

    res = client.get('/transfer-requests')
    reqs = res.get_json()
    assert any(r['transfer_id'] == 'test_lan_reject' for r in reqs), 'Pending LAN request not listed'

    res = client.post('/transfer-request/test_lan_reject/respond', json={'accept': False})
    assert res.status_code == 200 and res.get_json()['status'] == 'rejected'

    res = client.get('/transfer-request-status/test_lan_reject')
    assert res.get_json()['status'] == 'rejected', 'Status should be rejected'

    # Verify that trying to upload a chunk for a non-accepted transfer returns 403 Forbidden!
    import io
    dummy_stream = io.BytesIO(b'dummy unapproved chunk data')
    res = client.post('/upload-chunk', data={
        'transfer_id': 'test_lan_reject',
        'filename': 'sample.pdf',
        'chunk_index': 0,
        'total_chunks': 1,
        'total_size': len(b'dummy unapproved chunk data'),
        'chunk': (dummy_stream, 'chunk.bin')
    }, content_type='multipart/form-data')
    assert res.status_code == 403, f'Expected 403 for unapproved chunk upload, got {res.status_code}'
    print('[PASS] Unapproved chunk upload correctly blocked with HTTP 403 Forbidden!')

    # 2c. Test LAN transfer request acceptance & chunk reassembly
    import hashlib
    test_lan_data = b'ANTIGRAVITY_P2P_LAN_CHUNKED_TEST_PAYLOAD' * 500  # ~20 KB
    test_lan_sha = hashlib.sha256(test_lan_data).hexdigest()
    test_lan_size = len(test_lan_data)

    res = client.post('/transfer-request', json={
        'transfer_id': 'test_lan_accept_full',
        'filename': 'lan_full_test.bin',
        'total_size': test_lan_size,
        'sender_name': 'Test Laptop A'
    })
    assert res.get_json()['status'] == 'pending'
    res = client.post('/transfer-request/test_lan_accept_full/respond', json={'accept': True})
    assert res.get_json()['status'] == 'accepted'

    # Upload chunk
    chunk_stream = io.BytesIO(test_lan_data)
    res = client.post('/upload-chunk', data={
        'transfer_id': 'test_lan_accept_full',
        'filename': 'lan_full_test.bin',
        'chunk_index': 0,
        'total_chunks': 1,
        'total_size': test_lan_size,
        'sha256': test_lan_sha,
        'chunk': (chunk_stream, 'chunk.bin')
    }, content_type='multipart/form-data')
    assert res.status_code == 200, f'Upload chunk failed: {res.data}'

    # Check file exists and verify SHA-256
    written_file = os.path.join(UPLOAD_DIR, 'lan_full_test.bin')
    assert os.path.exists(written_file), 'Reassembled file not found on disk'
    with open(written_file, 'rb') as f:
        written_data = f.read()
    assert written_data == test_lan_data, 'Written data does not match original!'
    print('[PASS] LAN transfer accepted, uploaded, and verified bit-for-bit!')

    # Clean up test file
    os.remove(written_file)

    # 2d. Test LAN chat
    res = client.post('/chat/receive', json={'from': 'Friend Laptop', 'message': 'Hey, ready to test file sharing!'})
    assert res.status_code == 200
    res = client.get('/chat/messages')
    assert res.status_code == 200
    msgs = res.get_json()
    assert any('ready to test' in m.get('message', '') for m in msgs), 'Chat message not found'
    print('[PASS] LAN chat messages verified')

    # 3. Test Bluetooth endpoints
    res = client.get('/api/bt/status')
    assert res.status_code == 200, f'GET /api/bt/status failed: {res.status_code}'
    status_data = res.get_json()
    assert 'adapter' in status_data and 'receiver' in status_data and 'transfer' in status_data
    print(f'[PASS] Bluetooth status: adapter_avail={status_data["adapter"]["available"]}, mac={status_data["adapter"]["mac_address"]}')

    res = client.get('/api/bt/devices')
    assert res.status_code == 200, f'GET /api/bt/devices failed: {res.status_code}'
    devices_data = res.get_json()
    assert 'devices' in devices_data
    print(f'[PASS] Paired devices endpoint returned {len(devices_data["devices"])} devices')

    # 4. Test Mock Receiver Start & File Transfer (Reject then Accept)
    test_file_path = os.path.join(UPLOAD_DIR, 'test_integration.txt')
    with open(test_file_path, 'w', encoding='utf-8') as f:
        f.write('Hello Bluetooth integration from automated verification test!')

    res = client.post('/api/bt/receiver/start', json={'mock': True, 'channel': 4})
    assert res.status_code == 200
    rec_start_data = res.get_json()
    assert rec_start_data['ok'] is True, f'Receiver start error: {rec_start_data}'
    print('[PASS] Mock Bluetooth receiver started successfully')

    # 4a. Test Bluetooth REJECT:
    res = client.post('/api/bt/send', json={
        'target_mac': '127.0.0.1',
        'filename': 'test_integration.txt',
        'mock': True,
        'channel': 4
    })
    assert res.status_code == 200

    # Wait for the incoming request to appear
    bt_req_id = None
    for _ in range(30):
        time.sleep(0.1)
        res = client.get('/transfer-requests')
        reqs = res.get_json()
        bt_reqs = [r for r in reqs if r.get('mode') == 'bluetooth']
        if bt_reqs:
            bt_req_id = bt_reqs[0]['transfer_id']
            break

    assert bt_req_id is not None, 'Bluetooth incoming request was not generated'
    # Reject it!
    res = client.post(f'/transfer-request/{bt_req_id}/respond', json={'accept': False})
    assert res.status_code == 200 and res.get_json()['status'] == 'rejected'

    # Sender should terminate with error
    for _ in range(50):
        time.sleep(0.1)
        res = client.get('/api/bt/status')
        st = res.get_json()['transfer']['state']
        if st in ('failed', 'error', 'cancelled'):
            break

    assert st in ('failed', 'error', 'cancelled'), f'Expected rejected transfer to fail, got {st}'
    print('[PASS] Bluetooth transfer correctly rejected and cancelled (0 bytes received)')

    # 4b. Test Bluetooth ACCEPT:
    time.sleep(0.5)
    res = client.post('/api/bt/send', json={
        'target_mac': '127.0.0.1',
        'filename': 'test_integration.txt',
        'mock': True,
        'channel': 4
    })
    assert res.status_code == 200

    bt_req_id_2 = None
    for _ in range(30):
        time.sleep(0.1)
        res = client.get('/transfer-requests')
        reqs = res.get_json()
        bt_reqs = [r for r in reqs if r.get('mode') == 'bluetooth']
        if bt_reqs:
            bt_req_id_2 = bt_reqs[0]['transfer_id']
            break

    assert bt_req_id_2 is not None, 'Second Bluetooth incoming request was not generated'
    # Accept it!
    res = client.post(f'/transfer-request/{bt_req_id_2}/respond', json={'accept': True})
    assert res.status_code == 200 and res.get_json()['status'] == 'accepted'

    # Poll until transfer completes
    s = None
    for _ in range(60):
        time.sleep(0.1)
        res = client.get('/api/bt/status')
        s = res.get_json()
        state = s['transfer']['state']
        if state in ('completed', 'failed', 'cancelled'):
            break

    assert s is not None and s['transfer']['state'] == 'completed', f'Transfer ended in state: {s}'
    assert s['transfer']['sha256_match'] is True, 'Integrity verification failed!'
    print(f'[PASS] Bluetooth transfer accepted & completed with verified SHA-256 match! Hash: {s["transfer"]["sha256_hash"][:16]}...')

    # Stop receiver
    res = client.post('/api/bt/receiver/stop')
    assert res.status_code == 200
    print('[PASS] Bluetooth receiver stopped successfully')

    # Verify transfer history contains the completed BT transfer
    res = client.get('/transfer-history')
    hist = res.get_json()
    bt_entries = [h for h in hist if 'Bluetooth' in h.get('peer_name', '')]
    assert len(bt_entries) > 0, 'Bluetooth transfer not recorded in history'
    print(f'[PASS] Bluetooth transfer correctly recorded in transfer history (entries: {len(bt_entries)})')

    # Clean up test file
    if os.path.exists(test_file_path):
        os.remove(test_file_path)

    print('\n==========================================')
    print('  ALL APPLICATION INTEGRATION CHECKS PASSED!')
    print('==========================================')

if __name__ == '__main__':
    run_checks()
