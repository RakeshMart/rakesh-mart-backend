from flask import Flask, request, jsonify
from flask_cors import CORS
from pywebpush import webpush, WebPushException
import os
import json

app = Flask(__name__)
CORS(app)

# VAPID keys — Render environment variables mein daalni hain
VAPID_PRIVATE_KEY = os.environ.get('VAPID_PRIVATE_KEY')
VAPID_PUBLIC_KEY = os.environ.get('VAPID_PUBLIC_KEY')
VAPID_EMAIL = os.environ.get('VAPID_EMAIL', 'mailto:rakeshmart@gmail.com')

# Tokens store (memory mein — Render restart pe reset hoga)
# Isliye hum file mein save karenge
TOKENS_FILE = 'tokens.json'

def load_tokens():
    try:
        if os.path.exists(TOKENS_FILE):
            with open(TOKENS_FILE, 'r') as f:
                return json.load(f)
    except:
        pass
    return []

def save_tokens(tokens):
    try:
        with open(TOKENS_FILE, 'w') as f:
            json.dump(tokens, f)
    except:
        pass

@app.route('/register-token', methods=['POST'])
def register_token():
    data = request.json
    token_str = data.get('token')
    if not token_str:
        return jsonify({'error': 'No token'}), 400
    
    try:
        # token ek JSON string hai (Web Push subscription object)
        sub = json.loads(token_str) if isinstance(token_str, str) else token_str
        endpoint = sub.get('endpoint', '')
        
        tokens = load_tokens()
        
        # Duplicate check (endpoint se)
        existing = [t for t in tokens if t.get('endpoint') != endpoint]
        existing.append(sub)
        save_tokens(existing)
        
        print(f'Token registered. Total: {len(existing)}')
        return jsonify({'success': True})
    except Exception as e:
        print(f'Register error: {e}')
        return jsonify({'error': str(e)}), 400

@app.route('/send-notification', methods=['POST'])
def send_notification():
    # Auth check
    auth = request.headers.get('X-Auth-Key')
    if auth != os.environ.get('ADMIN_SECRET'):
        return jsonify({'error': 'Unauthorized'}), 401

    data = request.json
    title = data.get('title', '🛒 Rakesh Mart')
    body = data.get('body', '')
    image = data.get('image', '')  # Sheet D column

    if not body:
        return jsonify({'error': 'No message'}), 400

    payload = json.dumps({
        'title': title,
        'body': body,
        'image': image,
        'url': 'https://rakeshmart.github.io/website6/'
    })

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
            print(f'WebPush failed: {e}')
            # 410 = subscription expired, remove karo
            if '410' in str(e) or '404' in str(e):
                print('Removing expired token')
            else:
                valid_tokens.append(sub)  # Other errors mein rakhlo
            fail_count += 1
        except Exception as e:
            print(f'Unknown error: {e}')
            valid_tokens.append(sub)
            fail_count += 1

    save_tokens(valid_tokens)

    return jsonify({
        'success': True,
        'sent': success_count,
        'failed': fail_count,
        'total_tokens': len(valid_tokens)
    })

@app.route('/health', methods=['GET'])
def health():
    tokens = load_tokens()
    return jsonify({
        'status': 'ok',
        'tokens': len(tokens),
        'vapid_ready': bool(VAPID_PRIVATE_KEY)
    })

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
