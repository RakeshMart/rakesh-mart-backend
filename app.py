import os
import json
import time
import hashlib
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

# Local cache file — NOT the source of truth anymore. It only protects
# /health and push_to_all from briefly showing 0 subscribers if the Google
# Sheet happens to be unreachable at the exact moment Render restarts.
# The Google Sheet ("Push Subscriptions" tab) is the real source of truth
# and overwrites this cache on every successful load.
SUBSCRIPTIONS_CACHE_FILE = 'push_subscriptions_cache.json'
SENT_IDS_FILE = 'sent_notif_ids.json'
IST = timezone(timedelta(hours=5, minutes=30))

_file_lock = threading.Lock()

# ---------------- IN-MEMORY SUBSCRIPTION STORE ----------------
# endpoint -> {"endpoint": ..., "keys": {"p256dh": ..., "auth": ...}}
_subs_lock = threading.Lock()
_subscriptions = {}


def _sub_to_webpush_format(endpoint, p256dh, auth):
    return {"endpoint": endpoint, "keys": {"p256dh": p256dh, "auth": auth}}


def _save_local_cache():
    try:
        with _subs_lock:
            data = list(_subscriptions.values())
        with _file_lock:
            with open(SUBSCRIPTIONS_CACHE_FILE, 'w') as f:
                json.dump(data, f)
    except Exception as e:
        log.error(f"_save_local_cache error: {e}")


def _load_local_cache_fallback():
    """Stale fallback ONLY — used if the Google Sheet fetch fails at startup,
    so /health isn't misleadingly 0 while GAS is briefly unreachable. The
    sheet remains the source of truth and overwrites this on next success."""
    try:
        if os.path.exists(SUBSCRIPTIONS_CACHE_FILE):
            with open(SUBSCRIPTIONS_CACHE_FILE, 'r') as f:
                cached = json.load(f)
            with _subs_lock:
                for s in cached:
                    ep = s.get('endpoint')
                    if ep:
                        _subscriptions[ep] = s
            log.warning(f"Loaded {len(cached)} subscription(s) from local cache fallback (Google Sheet unreachable at startup)")
    except Exception as e:
        log.error(f"_load_local_cache_fallback error: {e}")


def load_subscriptions_from_gas():
    """Source of truth reload — call at startup (and safe to call anytime)."""
    if not GAS_URL or not GAS_SECRET:
        log.warning("GAS_URL/GAS_SECRET not set — cannot load persistent subscriptions from Google Sheet")
        return
    try:
        resp = requests.get(GAS_URL, params={'action': 'getPushSubscriptions', 'key': GAS_SECRET}, timeout=15)
        data = resp.json()
        subs = data.get('subscriptions', [])
        with _subs_lock:
            _subscriptions.clear()
            for s in subs:
                endpoint = s.get('endpoint')
                if not endpoint:
                    continue
                _subscriptions[endpoint] = _sub_to_webpush_format(endpoint, s.get('p256dh', ''), s.get('auth', ''))
        log.info(f"Loaded {len(_subscriptions)} active push subscription(s) from Google Sheet")
        _save_local_cache()
    except Exception as e:
        log.error(f"load_subscriptions_from_gas error: {e}")


def register_subscription_remote(endpoint, p256dh, auth, user_agent=''):
    if not GAS_URL or not GAS_SECRET:
        return False
    try:
        resp = requests.post(GAS_URL, json={
            'action': 'registerPushSubscription',
            'key': GAS_SECRET,
            'subscription': {
                'endpoint': endpoint,
                'p256dh': p256dh,
                'auth': auth,
                'userAgent': user_agent
            }
        }, timeout=15)
        result = resp.json()
        return bool(result.get('success'))
    except Exception as e:
        log.error(f"register_subscription_remote error: {e}")
        return False


def deactivate_subscription_remote(endpoint):
    if not GAS_URL or not GAS_SECRET:
        return False
    try:
        resp = requests.post(GAS_URL, json={
            'action': 'deactivatePushSubscription',
            'key': GAS_SECRET,
            'endpoint': endpoint
        }, timeout=15)
        result = resp.json()
        return bool(result.get('success'))
    except Exception as e:
        log.error(f"deactivate_subscription_remote error: {e}")
        return False


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
    Returns (success_count, fail_count). Expired (404/410) subscriptions are
    removed from memory AND deactivated in the Google Sheet."""
    payload = json.dumps(payload_dict)
    with _subs_lock:
        subs_snapshot = dict(_subscriptions)

    success_count = 0
    fail_count = 0
    expired_endpoints = []

    for endpoint, sub in subs_snapshot.items():
        try:
            webpush(
                subscription_info=sub,
                data=payload,
                vapid_private_key=VAPID_PRIVATE_KEY,
                vapid_claims={"sub": VAPID_EMAIL}
            )
            success_count += 1
        except WebPushException as e:
            status_code = getattr(e.response, 'status_code', None)
            log.warning(f"WebPush failed ({status_code}) for endpoint ...{endpoint[-12:]}: {e}")
            if status_code in (404, 410) or '410' in str(e) or '404' in str(e):
                expired_endpoints.append(endpoint)
            fail_count += 1
        except Exception as e:
            log.error(f"WebPush unknown error: {e}")
            fail_count += 1

    if expired_endpoints:
        with _subs_lock:
            for ep in expired_endpoints:
                _subscriptions.pop(ep, None)
        _save_local_cache()
        for ep in expired_endpoints:
            deactivate_subscription_remote(ep)
        log.info(f"Removed {len(expired_endpoints)} expired subscription(s) (404/410)")

    return success_count, fail_count


# ---------------- EXISTING ENDPOINTS (preserved) ----------------
@app.route('/register-token', methods=['POST'])
def register_token():
    data = request.json or {}
    token_str = data.get('token')
    if not token_str:
        return jsonify({'error': 'No token'}), 400
    try:
        sub = json.loads(token_str) if isinstance(token_str, str) else token_str
        endpoint = sub.get('endpoint', '')
        keys = sub.get('keys', {}) or {}
        p256dh = keys.get('p256dh', '')
        auth = keys.get('auth', '')

        if not endpoint or not p256dh or not auth:
            return jsonify({'error': 'Invalid subscription — endpoint/p256dh/auth missing'}), 400

        user_agent = request.headers.get('User-Agent', '')[:250]

        # Update in-memory immediately — /health and the next push_to_all
        # reflect this registration right away, no need to wait on the sheet.
        with _subs_lock:
            _subscriptions[endpoint] = _sub_to_webpush_format(endpoint, p256dh, auth)
        _save_local_cache()

        # Persist to Google Sheet (source of truth). Endpoint is the unique
        # key — an existing row is updated in place, no duplicate rows.
        ok = register_subscription_remote(endpoint, p256dh, auth, user_agent)
        if not ok:
            log.warning("Subscription accepted locally but Google Sheet persistence failed — will retry via next registration or manual reload")

        with _subs_lock:
            total = len(_subscriptions)
        log.info(f'Token registered. Total active: {total}')
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


@app.route('/reload-subscriptions', methods=['POST'])
def reload_subscriptions():
    """Manual trigger to re-sync in-memory subscribers from the Google Sheet
    without restarting the service. Protected by ADMIN_SECRET."""
    auth = request.headers.get('X-Auth-Key')
    if auth != ADMIN_SECRET:
        return jsonify({'error': 'Unauthorized'}), 401
    load_subscriptions_from_gas()
    with _subs_lock:
        total = len(_subscriptions)
    return jsonify({'success': True, 'tokens': total})


@app.route('/health', methods=['GET'])
def health():
    with _subs_lock:
        token_count = len(_subscriptions)
    return jsonify({
        'status': 'ok',
        'tokens': token_count,
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


def process_rows(notif_type, rows, enabled, today_str, current_hm, sent_ids):
    if not enabled or not rows:
        return
    title = "🎡 Rakesh Mart • Spin" if notif_type == 'spin' else "🔥 Rakesh Mart • Special Offer"
    url = SITE_URL + '/#spin' if notif_type == 'spin' else SITE_URL + '/#offer'

    for row_info in rows:
        row = row_info['row']
        message = row_info['message']
        time_val = str(row_info.get('time', '')).strip().lower()
        image_url = str(row_info.get('image', '') or '').strip()

        # notif_id is based on the ROW + its CONTENT (type+row+time+message+image),
        # not just row+date. Editing the text/time/image — even after the row
        # was already marked Done — is treated as a brand new notification and
        # will be sent again (as long as the sheet status column is not still
        # "Done" for that row — clear it to blank to allow a resend). An
        # unchanged row will never duplicate-send.
        content_key = hashlib.sha256(f"{message}|{time_val}|{image_url}".encode('utf-8')).hexdigest()[:12]
        notif_id = f"{notif_type}:{row}:{content_key}"

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
            'image': image_url,  # empty string -> service worker sends text-only
            'url': url,
            'notificationId': notif_id
        }

        success_count, fail_count = push_to_all(payload)
        log.info(f"[{notif_type.upper()}] row={row} scheduled={time_val} now={current_hm} sent={success_count} failed={fail_count}")

        # Only mark Done when at least one subscriber ACTUALLY received the
        # push. Zero registered subscribers, or all pushes failing, must NOT
        # mark Done — the row stays pending and is retried next cycle.
        if success_count > 0:
            save_sent_id(notif_id)
            ok = mark_done_in_sheet(notif_type, row)
            if not ok:
                log.error(f"[{notif_type.upper()}] row={row} push sent but sheet Done write failed — will not resend today due to local id guard")
        else:
            if fail_count == 0:
                log.warning(f"[{notif_type.upper()}] row={row} NOT sent — 0 registered subscribers. Status left blank, will retry next cycle.")
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
                 today_str, current_hm, sent_ids)
    process_rows('offer', data.get('offerRows', []), data.get('offerEnabled', False),
                 today_str, current_hm, sent_ids)


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


# ---------------- STARTUP ----------------
# Google Sheet is the source of truth for subscriptions. Load it first;
# only fall back to the local cache file if the Sheet was unreachable.
load_subscriptions_from_gas()
with _subs_lock:
    _has_subs = len(_subscriptions) > 0
if not _has_subs:
    _load_local_cache_fallback()

start_scheduler_once()

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
