from flask import Flask, request, jsonify
from flask_cors import CORS
import firebase_admin
from firebase_admin import credentials, messaging
import requests
import os
import json

app = Flask(__name__)
CORS(app)  # GitHub Pages se request allow karne ke liye

# Firebase initialize — environment variable se
firebase_creds = json.loads(os.environ.get('FIREBASE_SERVICE_ACCOUNT'))
cred = credentials.Certificate(firebase_creds)
firebase_admin.initialize_app(cred)

SHEET_SCRIPT_URL = os.environ.get('SHEET_SCRIPT_URL')

# FCM Tokens store karne ke liye (simple approach)
# Production mein database use karo
fcm_tokens = set()

@app.route('/register-token', methods=['POST'])
def register_token():
    """User ka FCM token save karo"""
    data = request.json
    token = data.get('token')
    if token:
        fcm_tokens.add(token)
        return jsonify({'success': True})
    return jsonify({'error': 'No token'}), 400

@app.route('/send-notification', methods=['POST'])
def send_notification():
    """Sheet se notification fetch karke sab users ko bhejo"""
    # Secret key check karo
    auth = request.headers.get('X-Auth-Key')
    if auth != os.environ.get('ADMIN_SECRET'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    data = request.json
    title = data.get('title', 'Rakesh Mart')
    body = data.get('body', '')
    
    if not body:
        return jsonify({'error': 'No message'}), 400
    
    # Sab tokens ko notification bhejo
    success_count = 0
    fail_count = 0
    
    for token in list(fcm_tokens):
        try:
            message = messaging.Message(
                notification=messaging.Notification(
                    title=title,
                    body=body,
                ),
                webpush=messaging.WebpushConfig(
                    notification=messaging.WebpushNotification(
                        title=title,
                        body=body,
                        icon='/website6/icon-192.png',
                        badge='/website6/icon-192.png',
                    ),
                    fcm_options=messaging.WebpushFCMOptions(
                        link='https://rakeshmart.github.io/website6/'
                    )
                ),
                token=token,
            )
            messaging.send(message)
            success_count += 1
        except Exception as e:
            print(f'Token failed: {e}')
            fcm_tokens.discard(token)  # Invalid token remove karo
            fail_count += 1
    
    return jsonify({
        'success': True,
        'sent': success_count,
        'failed': fail_count
    })

@app.route('/check-sheet', methods=['GET'])
def check_sheet():
    """Sheet mein nayi notification check karo (cron job ke liye)"""
    try:
        resp = requests.get(
            SHEET_SCRIPT_URL + '?action=getNotification',
            timeout=10
        )
        data = resp.json()
        notifications = data.get('notifications', [])
        
        if notifications:
            latest = notifications[0]
            # Yahan bhejo
            return jsonify({'notification': latest})
        
        return jsonify({'notification': None})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({'status': 'ok', 'tokens': len(fcm_tokens)})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)