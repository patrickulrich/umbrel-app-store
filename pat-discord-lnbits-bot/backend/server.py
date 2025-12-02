from flask import Flask, request, jsonify, send_from_directory, abort
from flask_cors import CORS
from werkzeug.utils import safe_join
import json
import os
import re
import signal
import subprocess
import threading
import logging
import atexit
from datetime import datetime

app = Flask(__name__)
CORS(app)

# Frontend directory (absolute path)
FRONTEND_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'frontend'))
# Bot script path (absolute path)
BOT_SCRIPT = os.path.join(os.path.dirname(__file__), 'bot.py')

# Configuration
DATA_DIR = os.environ.get('APP_DATA_DIR', '/app/data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')
LOG_FILE = os.path.join(DATA_DIR, 'bot.log')

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)

# Bot process management
bot_process = None
bot_thread = None

def run_bot():
    """Run the Discord bot in a subprocess"""
    global bot_process
    try:
        bot_process = subprocess.Popen(
            ['python', BOT_SCRIPT],
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )

        # Stream output to log
        for line in bot_process.stdout:
            logging.info(f"BOT: {line.strip()}")

    except Exception as e:
        logging.error(f"Error running bot: {e}")
    finally:
        if bot_process:
            bot_process.wait()
            logging.info(f"Bot process ended with code: {bot_process.returncode}")

def start_bot():
    """Start the bot in a separate thread"""
    global bot_thread
    
    if bot_thread and bot_thread.is_alive():
        return False, "Bot is already running"
    
    if not os.path.exists(CONFIG_FILE):
        return False, "Configuration not found. Please save settings first."
    
    bot_thread = threading.Thread(target=run_bot, daemon=True)
    bot_thread.start()
    
    return True, "Bot started successfully"

def stop_bot():
    """Stop the running bot gracefully"""
    global bot_process

    if bot_process and bot_process.poll() is None:
        try:
            # Send SIGINT for graceful shutdown
            bot_process.send_signal(signal.SIGINT)
            bot_process.wait(timeout=5)
            return True, "Bot stopped successfully"
        except subprocess.TimeoutExpired:
            # Fallback to SIGKILL if graceful shutdown fails
            logging.warning("Bot did not stop gracefully, forcing termination")
            bot_process.kill()
            bot_process.wait(timeout=2)
            return True, "Bot force stopped"
        except Exception as e:
            logging.error(f"Error stopping bot: {e}")
            return False, f"Error stopping bot: {str(e)}"

    return False, "Bot is not running"


def cleanup_on_exit():
    """Cleanup function called on server exit"""
    logging.info("Server shutting down, stopping bot...")
    stop_bot()


# Register cleanup handler
atexit.register(cleanup_on_exit)

# Routes
@app.route('/')
def index():
    """Serve the frontend"""
    return send_from_directory(FRONTEND_DIR, 'index.html')


@app.route('/<path:path>')
def serve_static(path):
    """Serve static files securely"""
    # Validate path doesn't escape frontend directory
    safe_path = safe_join(FRONTEND_DIR, path)
    if safe_path is None or not os.path.isfile(safe_path):
        abort(404)
    return send_from_directory(FRONTEND_DIR, path)

@app.route('/api/config', methods=['GET'])
def get_config():
    """Get current configuration"""
    if os.path.exists(CONFIG_FILE):
        with open(CONFIG_FILE, 'r') as f:
            config = json.load(f)
            # Don't send sensitive data to frontend
            safe_config = {k: v for k, v in config.items() if k not in ['discord_token', 'lnbits_api_key']}
            safe_config['configured'] = bool(config.get('discord_token')) and bool(config.get('lnbits_api_key'))
            return jsonify(safe_config)
    return jsonify({'configured': False})

def validate_config(data: dict) -> tuple[bool, str]:
    """
    Validate configuration data.

    Returns (is_valid, error_message).
    """
    # Required fields check
    required_fields = ['discord_token', 'guild_id', 'role_id', 'lnbits_url',
                       'lnbits_api_key', 'price', 'channelid', 'command_name']

    for field in required_fields:
        if field not in data or not data[field]:
            return False, f'Missing required field: {field}'

    # Validate command_name: alphanumeric and underscore only, 1-32 chars
    command_name = str(data.get('command_name', ''))
    if not re.match(r'^[a-z0-9_]{1,32}$', command_name.lower()):
        return False, 'Command name must be 1-32 characters, alphanumeric and underscores only'

    # Validate numeric ID fields
    for field in ['guild_id', 'role_id', 'channelid']:
        try:
            value = int(data[field])
            if value <= 0:
                return False, f'{field} must be a positive number'
        except (ValueError, TypeError):
            return False, f'{field} must be a valid number'

    # Validate price: positive integer
    try:
        price = int(data['price'])
        if price <= 0:
            return False, 'Price must be a positive number of sats'
        if price > 21_000_000 * 100_000_000:  # Max possible sats
            return False, 'Price exceeds maximum possible sats'
    except (ValueError, TypeError):
        return False, 'Price must be a valid number'

    # Validate invoice message length
    invoice_msg = data.get('invoicemessage', '')
    if len(invoice_msg) > 1000:
        return False, 'Invoice message must be 1000 characters or less'

    # Validate LNBits URL format
    lnbits_url = data.get('lnbits_url', '')
    if not lnbits_url.startswith(('http://', 'https://')):
        return False, 'LNBits URL must start with http:// or https://'

    return True, ''


@app.route('/api/config', methods=['POST'])
def save_config():
    """Save configuration"""
    try:
        data = request.json

        # Validate configuration
        is_valid, error_msg = validate_config(data)
        if not is_valid:
            return jsonify({'error': error_msg}), 400

        # Set defaults
        data.setdefault('invoicemessage', 'Please pay this Lightning invoice to receive your role!')

        # Normalize numeric fields to strings for JSON storage
        for field in ['guild_id', 'role_id', 'channelid', 'price']:
            data[field] = str(data[field])

        # Save configuration
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(CONFIG_FILE, 'w') as f:
            json.dump(data, f, indent=2)

        logging.info("Configuration saved successfully")
        return jsonify({'success': True, 'message': 'Configuration saved successfully'})

    except Exception as e:
        logging.error(f"Error saving config: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/api/bot/start', methods=['POST'])
def start_bot_endpoint():
    """Start the Discord bot"""
    success, message = start_bot()
    return jsonify({'success': success, 'message': message})

@app.route('/api/bot/stop', methods=['POST'])
def stop_bot_endpoint():
    """Stop the Discord bot"""
    success, message = stop_bot()
    return jsonify({'success': success, 'message': message})

@app.route('/api/bot/status', methods=['GET'])
def bot_status():
    """Get bot status"""
    is_running = bot_process and bot_process.poll() is None
    return jsonify({
        'running': is_running,
        'configured': os.path.exists(CONFIG_FILE)
    })

@app.route('/api/logs', methods=['GET'])
def get_logs():
    """Get recent log entries"""
    try:
        if os.path.exists(LOG_FILE):
            with open(LOG_FILE, 'r') as f:
                lines = f.readlines()
                # Return last 100 lines
                recent_logs = lines[-100:]
                return jsonify({'logs': recent_logs})
        return jsonify({'logs': []})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/test-connection', methods=['POST'])
def test_connection():
    """Test Discord and LNBits connections"""
    import requests as req

    data = request.json
    results = {}

    # Test Discord token - just check if provided (actual validation requires bot login)
    discord_token = data.get('discord_token', '')
    if discord_token:
        results['discord'] = {
            'valid': True,
            'message': 'Token provided (will be validated when bot starts)'
        }
    else:
        results['discord'] = {
            'valid': False,
            'message': 'No token provided'
        }

    # Test LNBits connection - both wallet read AND invoice creation
    lnbits_url = data.get('lnbits_url', '').rstrip('/')
    lnbits_key = data.get('lnbits_api_key', '')

    if not lnbits_url or not lnbits_key:
        results['lnbits'] = {
            'valid': False,
            'message': 'LNBits URL and API key required'
        }
        return jsonify(results)

    headers = {"X-Api-Key": lnbits_key, "Content-Type": "application/json"}

    # Step 1: Test wallet read access
    try:
        wallet_response = req.get(
            f"{lnbits_url}/api/v1/wallet",
            headers=headers,
            timeout=5
        )

        if wallet_response.status_code != 200:
            results['lnbits'] = {
                'valid': False,
                'message': f'Wallet access failed: HTTP {wallet_response.status_code}'
            }
            return jsonify(results)

    except req.exceptions.ConnectionError:
        results['lnbits'] = {
            'valid': False,
            'message': 'Cannot connect to LNBits - check URL and network'
        }
        return jsonify(results)
    except req.exceptions.Timeout:
        results['lnbits'] = {
            'valid': False,
            'message': 'LNBits connection timed out'
        }
        return jsonify(results)
    except Exception as e:
        results['lnbits'] = {
            'valid': False,
            'message': f'Connection failed: {str(e)}'
        }
        return jsonify(results)

    # Step 2: Test invoice creation (1 sat test invoice)
    try:
        invoice_response = req.post(
            f"{lnbits_url}/api/v1/payments",
            headers=headers,
            json={"out": False, "amount": 1, "memo": "Connection test"},
            timeout=10
        )

        if invoice_response.status_code == 201:
            results['lnbits'] = {
                'valid': True,
                'message': 'Connected and invoice creation working'
            }
        elif invoice_response.status_code >= 500:
            results['lnbits'] = {
                'valid': False,
                'message': f'LNBits server error ({invoice_response.status_code}) - check if Lightning node is connected'
            }
        elif invoice_response.status_code == 401:
            results['lnbits'] = {
                'valid': False,
                'message': 'Invalid API key'
            }
        elif invoice_response.status_code == 403:
            results['lnbits'] = {
                'valid': False,
                'message': 'API key does not have invoice permission'
            }
        else:
            results['lnbits'] = {
                'valid': False,
                'message': f'Invoice creation failed: HTTP {invoice_response.status_code}'
            }

    except req.exceptions.Timeout:
        results['lnbits'] = {
            'valid': False,
            'message': 'Invoice creation timed out - Lightning node may be slow or disconnected'
        }
    except Exception as e:
        results['lnbits'] = {
            'valid': False,
            'message': f'Invoice test failed: {str(e)}'
        }

    return jsonify(results)

if __name__ == '__main__':
    # Ensure data directory exists
    os.makedirs(DATA_DIR, exist_ok=True)
    
    # Start with saved config if exists
    if os.path.exists(CONFIG_FILE):
        logging.info("Found existing configuration, starting bot...")
        start_bot()
    
    # Start Flask server
    app.run(host='0.0.0.0', port=3050, debug=False)
