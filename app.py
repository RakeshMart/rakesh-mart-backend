import os
import json
import time
import threading
import logging
from datetime import datetime, timezone, timedelta

import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from pywebpush import webpush, WebPushException

logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s')
log = logging.getLogger("rakeshmart")

app = Flask(__name__)
CORS(app)

# ---------------- CONFIG (env vars) ----------------
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_EMAIL = os.environ.get('VAPID_EMAIL', 'mailto:rakeshmart@gmail.com')
ADMIN_SECRET = os.environ.get('ADMIN_SECRET')

GAS_URL = os.environ.get('GAS_URL', '')          # same Apps Script /exec URL used by the site
GAS_SECRET = os.environ.get('GAS_SECRET', '')    # matches SCHED_NOTIF_SECRET in Apps Script
SITE_URL = os.environ.get('SITE_URL', 'https://rakeshmart.github.io/website6/').rstrip('/')
POLL_INTERVAL_SECONDS = int(os.environ.get('POLL_INTERVAL_SECONDS', '4'))

TOKENS_FILE = 'tokens.json'
SENT_IDS_FILE = 'sent_notif_ids.json'
IST = timezone(timedelta(hours=5, minutes=30))

_file_lock = threading.Lock()

# ---------------- TOKEN STORAGE ----------------
def load_tokens():
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_tokens(tokens):
    try:
        with _file_lock:
            with open(TOKENS_FILE, 'w') as f:
                json.dump(tokens, f)
    except Exception as e:
        log.error(f"save_tokens error: {e}")

# ---------------- SENT-IDS STORAGE (idempotency for scheduler) ----------------
def load_sent_ids():
    try:
        if os.path.exists(SENT_IDS_FILE):
            with open(SENT_IDS_FILE, 'r') as f:
                return json.load(f)
    except Exception:
        pass
    return {}

def save_sent_id(notif_id):
    try:
        with _file_lock:
            ids = load_sent_ids()
            ids[notif_id] = time.time()
            # keep file small — drop entries older than 3 days
            cutoff = time.time() - (3 * 86400)
            ids = {k: v for k, v in ids.items() if v > cutoff}
            with open(SENT_IDS_FILE, 'w') as f:
                json.dump(ids, f)
    except Exception as e:
        log.error(f"save_sent_id error: {e}")

# ---------------- CORE PUSH SENDER (shared by manual + scheduled) ----------------
def push_to_all(payload_dict):
    """Sends a Web Push payload (dict) to every registered subscriber.
    Returns (success_count, fail_count)."""
    payload = json.dumps(payload_dict)
    tokens = load_tokens()
    success_count = 0
    fail_count = 0
    valid_tokens = []

    for sub in tokens:
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_EMAIL}
            )
            success_count += 1
            valid_tokens.append(sub)
        except WebPushException as e:
            log.warning(f"WebPush failed: {e}")
            if '410' in str(e) or '404' in str(e):
                log.info("Expired token removed")
            else:
                valid_tokens.append(sub)
            fail_count += 1
        except Exception as e:
            log.error(f"WebPush unknown error: {e}")
            valid_tokens.append(sub)
            fail_count += 1

    save_tokens(valid_tokens)
    return success_count, fail_count

# ---------------- EXISTING ENDPOINTS (preserved) ----------------
@app.route('/register-token', methods=['POST'])
def register_token():
    data = request.json
    token_str = data.get('token')
    if not token_str:
        return jsonify({'error': 'No token'}), 400
    try:
        sub = json.loads(token_str) if isinstance(token_str, str) else token_str
        endpoint = sub.get('endpoint', '')
        tokens = load_tokens()
        existing = [t for t in tokens if t.get('endpoint') != endpoint]
        existing.append(sub)
        save_tokens(existing)
        log.info(f'Token registered. Total: {len(existing)}')
        return jsonify({'success': True})
    except Exception as e:
        log.error(f'Register error: {e}')
        return jsonify({'error': str(e)}), 400

@app.route('/send-notification', methods=['POST'])
def send_notification():
    auth = request.headers.get('X-Auth-Key')
    if auth != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json or {}
    title = data.get('title', '🛒 Rakesh Mart')
    body = data.get('body', '')
    image = data.get('image', '')
    if not body:
        return jsonify({'error': 'No message'}), 400

    payload = {
        'type': data.get('type', 'general'),
        'title': title,
        'body': body,
        'image': image,
        'url': data.get('url', SITE_URL + '/'),
        'notificationId': data.get('notificationId', f'manual:{int(time.time())}')
    }
    success_count, fail_count = push_to_all(payload)
    return jsonify({'success': True, 'sent': success_count, 'failed': fail_count})

@app.route('/health', methods=['GET'])
def health():
    tokens = load_tokens()
    return jsonify({
        'status': 'ok',
        'tokens': len(tokens),
        'vapid_ready': bool(VAPID_PRIVATE_KEY),
        'scheduler_ready': bool(GAS_URL and GAS_SECRET)
    })

# ---------------- SCHEDULER (Spin / Offer, server-side, no client dependency) ----------------
def fetch_sched_data():
    if not GAS_URL or not GAS_SECRET:
        return None
    try:
        resp = requests.get(GAS_URL, params={'action': 'getSchedNotifs', 'key': GAS_SECRET}, timeout=15)
        data = resp.json()
        if 'error' in data:
            log.error(f"getSchedNotifs error: {data['error']}")
            return None
        return data
    except Exception as e:
        log.error(f"fetch_sched_data network error: {e}")
        return None

def mark_done_in_sheet(notif_type, row):
    try:
        resp = requests.post(GAS_URL, json={
            'action': 'markSchedNotifDone',
            'key': GAS_SECRET,
            'notifType': notif_type,
            'row': row
        }, timeout=15)
        result = resp.json()
        if not result.get('success'):
            log.error(f"markSchedNotifDone failed for {notif_type} row={row}: {result}")
        return result.get('success', False)
    except Exception as e:
        log.error(f"mark_done_in_sheet network error: {e}")
        return False

def process_rows(notif_type, rows, enabled, image_url, today_str, current_hm, sent_ids):
    if not enabled or not rows:
        return
    title = "🎡 Rakesh Mart • Spin" if notif_type == 'spin' else "🔥 Rakesh Mart • Special Offer"
    url = SITE_URL + '/#spin' if notif_type == 'spin' else SITE_URL + '/#offer'

    for row_info in rows:
        row = row_info['row']
        message = row_info['message']
        time_val = str(row_info.get('time', '')).strip().lower()
        notif_id = f"{notif_type}:{row}:{today_str}"

        if notif_id in sent_ids:
            continue

        is_now = (time_val == 'now')
        is_exact_time = bool(time_val) and (time_val == current_hm)

        if not (is_now or is_exact_time):
            continue

        payload = {
            'type': notif_type,
            'title': title,
            'body': message,
            'image': image_url or '',
            'url': url,
            'notificationId': notif_id
        }

        success_count, fail_count = push_to_all(payload)
        log.info(f"[{notif_type.upper()}] row={row} scheduled={time_val} now={current_hm} sent={success_count} failed={fail_count}")

        if success_count > 0 or (success_count == 0 and fail_count == 0):
            # mark as sent locally immediately (protects against restart before sheet write completes)
            save_sent_id(notif_id)
            ok = mark_done_in_sheet(notif_type, row)
            if not ok:
                log.error(f"[{notif_type.upper()}] row={row} push sent but sheet Done write failed — will not resend today due to local id guard")
        else:
            log.error(f"[{notif_type.upper()}] row={row} push failed for all subscribers — status left blank, will retry next cycle")

def scheduler_tick():
    data = fetch_sched_data()
    if data is None:
        return
    now_ist = datetime.now(IST)
    today_str = now_ist.strftime('%Y-%m-%d')
    current_hm = now_ist.strftime('%H:%M')
    sent_ids = load_sent_ids()

    process_rows('spin', data.get('spinRows', []), data.get('spinEnabled', False),
                 data.get('spinImg', ''), today_str, current_hm, sent_ids)
    process_rows('offer', data.get('offerRows', []), data.get('offerEnabled', False),
                 data.get('offerImg', ''), today_str, current_hm, sent_ids)

def scheduler_loop():
    log.info(f"Scheduler started — polling every {POLL_INTERVAL_SECONDS}s (Asia/Kolkata)")
    while True:
        try:
            scheduler_tick()
        except Exception as e:
            log.error(f"scheduler_loop error: {e}")
        time.sleep(POLL_INTERVAL_SECONDS)

_scheduler_started = False
_scheduler_lock = threading.Lock()

def start_scheduler_once():
    global _scheduler_started
    with _scheduler_lock:
        if _scheduler_started:
            return
        if not GAS_URL or not GAS_SECRET:
            log.warning("GAS_URL / GAS_SECRET not set — scheduled Spin/Offer notifications disabled")
            return
        t = threading.Thread(target=scheduler_loop, daemon=True)
        t.start()
        _scheduler_started = True

start_scheduler_once()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
