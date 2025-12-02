import discord
import asyncio
import requests
import json
import io
import qrcode
import websockets
import traceback
import os
import signal
import logging
from discord import File, Embed
from discord.ext import commands, tasks

# Import database module
from database import (
    init_db,
    add_pending_invoice,
    get_pending_invoice,
    remove_pending_invoice,
    get_all_pending_invoices,
    cleanup_expired_invoices,
    INVOICE_EXPIRY_HOURS
)

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Graceful shutdown flag
shutdown_event = asyncio.Event()

# --- Configuration Loading ---
DATA_DIR = os.environ.get('APP_DATA_DIR', '/app/data')
CONFIG_FILE = os.path.join(DATA_DIR, 'config.json')

try:
    with open(CONFIG_FILE, "r") as f:
        config = json.load(f)
except FileNotFoundError:
    logging.error(f"❌ Error: {CONFIG_FILE} not found. Please configure through web interface.")
    exit(1)
except json.JSONDecodeError:
    logging.error(f"❌ Error: {CONFIG_FILE} is not valid JSON.")
    exit(1)

# Load values
TOKEN = config.get("discord_token")
GUILD_ID = int(config.get("guild_id", 0))
ROLE_ID = int(config.get("role_id", 0))
LNBITS_URL = config.get("lnbits_url")
LNBITS_API_KEY = config.get("lnbits_api_key")
PRICE = config.get("price")
CHANNEL_ID = int(config.get("channelid", 0))
INVOICE_MESSAGE_TEMPLATE = config.get("invoicemessage", "Invoice for your purchase.")
COMMAND_NAME = config.get("command_name", "support")

# Validate
if not LNBITS_URL or not LNBITS_API_KEY:
    logging.error("❌ Error: LNBITS_URL or LNBITS_API_KEY is missing in config.")
    exit(1)

clean_lnbits_http_url = LNBITS_URL.rstrip('/')
if clean_lnbits_http_url.startswith("https://"):
    base_ws_url = clean_lnbits_http_url.replace("https://", "wss://", 1)
elif clean_lnbits_http_url.startswith("http://"):
    base_ws_url = clean_lnbits_http_url.replace("http://", "ws://", 1)
else:
    logging.error(f"❌ Error: Invalid LNBITS_URL scheme: {LNBITS_URL}.")
    exit(1)

LNBITS_WEBSOCKET_URL = f"{base_ws_url}/api/v1/ws/{LNBITS_API_KEY}"

if not all([TOKEN, GUILD_ID, ROLE_ID, PRICE, CHANNEL_ID, COMMAND_NAME]):
    logging.error("❌ Error: One or more essential configuration options are missing.")
    exit(1)

# Discord client
intents = discord.Intents.default()
intents.members = True
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

# Initialize database
init_db()
logging.info(f"Invoice expiry set to {INVOICE_EXPIRY_HOURS} hour(s)")

async def assign_role_after_payment(payment_hash_received, payment_details_from_ws):
    logging.info(f"Processing payment for hash: {payment_hash_received}")

    # Look up invoice in database
    invoice_data = get_pending_invoice(payment_hash_received)
    if not invoice_data:
        logging.info(f"Hash {payment_hash_received} not found in pending invoices.")
        return

    # Remove from database immediately to prevent double processing
    remove_pending_invoice(payment_hash_received)

    user_id = invoice_data['user_id']
    logging.info(f"Found pending invoice for user_id={user_id}")

    guild = bot.get_guild(GUILD_ID)
    if not guild:
        logging.error(f"Guild {GUILD_ID} not found.")
        return

    member = guild.get_member(user_id)
    if not member:
        try:
            member = await asyncio.wait_for(guild.fetch_member(user_id), timeout=10)
        except Exception as e:
            logging.error(f"Error fetching member: {e}")
            return

    role = guild.get_role(ROLE_ID)
    if not role:
        logging.error(f"Role ID {ROLE_ID} not found in guild.")
        return

    invoice_channel = bot.get_channel(CHANNEL_ID)
    if not invoice_channel:
        logging.error(f"Invoice channel ID {CHANNEL_ID} not found.")

    if role not in member.roles:
        try:
            await asyncio.wait_for(member.add_roles(role, reason="Paid Lightning Invoice"), timeout=10)
            logging.info(f"Role '{role.name}' assigned to {member.name}")
            if invoice_channel:
                await invoice_channel.send(
                    f"🎉 {member.mention} has paid {PRICE} sats and been granted the '{role.name}' role!"
                )
        except Exception as e:
            logging.error(f"Error assigning role: {e}")
    else:
        if invoice_channel:
            await invoice_channel.send(
                f"✅ {member.mention}, payment confirmed! You already have the '{role.name}' role."
            )

async def lnbits_websocket_listener():
    await bot.wait_until_ready()
    logging.info(f"Connecting to LNBits WebSocket...")

    while True:
        try:
            async with websockets.connect(LNBITS_WEBSOCKET_URL) as ws:
                logging.info("✅ WebSocket connected.")
                while True:
                    try:
                        msg = await ws.recv()
                        logging.debug(f"WebSocket message: {msg}")
                        data = json.loads(msg)
                        if isinstance(data.get("payment"), dict):
                            p = data["payment"]
                            h = p.get("checking_id") or p.get("payment_hash")
                            amt = p.get("amount", 0)
                            status = p.get("status")
                            is_paid = (status == "success") or (p.get("paid") is True) or (p.get("pending") is False)

                            if h and is_paid and amt > 0:
                                logging.info(f"Payment confirmed: hash={h}, amount={amt}")
                                bot.loop.create_task(
                                    assign_role_after_payment(h, p)
                                )
                            else:
                                logging.debug(f"Payment update ignored: hash={h}, status={status}")
                        else:
                            logging.warning(f"Unexpected WebSocket format: {msg}")
                    except websockets.exceptions.ConnectionClosed:
                        logging.warning("WebSocket closed, reconnecting...")
                        break
                    except Exception as e:
                        logging.error(f"Error handling WebSocket message: {e}")
                        traceback.print_exc()
        except Exception as e:
            logging.error(f"WebSocket connection error: {e}. Retrying in 15s...")
            await asyncio.sleep(15)

@tasks.loop(minutes=5)
async def cleanup_expired_invoices_task():
    """Periodically clean up expired invoices"""
    try:
        removed = cleanup_expired_invoices()
        if removed > 0:
            logging.info(f"Cleanup task removed {removed} expired invoice(s)")
    except Exception as e:
        logging.error(f"Error in cleanup task: {e}")


@bot.event
async def on_ready():
    logging.info(f"✅ Bot ready as {bot.user} ({bot.user.id})")

    # Sync commands
    try:
        synced = await bot.tree.sync()
        logging.info(f"Synced {len(synced)} command(s)")
    except Exception as e:
        logging.error(f"Failed to sync commands: {e}")

    # Start periodic cleanup task
    if not cleanup_expired_invoices_task.is_running():
        cleanup_expired_invoices_task.start()
        logging.info("Started invoice cleanup task (runs every 5 minutes)")

    # Log any pending invoices from previous session
    pending = get_all_pending_invoices()
    if pending:
        logging.info(f"Found {len(pending)} pending invoice(s) from previous session")

    bot.loop.create_task(lnbits_websocket_listener())

def get_lnbits_error_message(status_code: int, response_text: str = "") -> str:
    """Return user-friendly error message based on LNBits response."""
    if status_code >= 500:
        return f"❌ LNBits server error ({status_code}) - Lightning node may be disconnected. Please contact admin."
    elif status_code == 401:
        return "❌ LNBits authentication failed - invalid API key."
    elif status_code == 403:
        return "❌ LNBits permission denied - API key may not have invoice permission."
    elif status_code == 404:
        return "❌ LNBits endpoint not found - check server configuration."
    else:
        return f"❌ LNBits error ({status_code}). Please try again later."


# Dynamic slash command
@bot.tree.command(name=COMMAND_NAME, description="Pay to get your role via Lightning.")
async def dynamic_command(interaction: discord.Interaction):
    invoice_channel = bot.get_channel(CHANNEL_ID)
    if not invoice_channel:
        await interaction.response.send_message("❌ Invoice channel misconfigured.", ephemeral=True)
        return

    invoice_data = {"out": False, "amount": PRICE, "memo": f"Role for {interaction.user.display_name}"}
    headers = {"X-Api-Key": LNBITS_API_KEY, "Content-Type": "application/json"}
    loop = asyncio.get_running_loop()

    try:
        resp = await loop.run_in_executor(None, lambda: requests.post(
            f"{clean_lnbits_http_url}/api/v1/payments", json=invoice_data, headers=headers,
            timeout=15
        ))
    except requests.exceptions.ConnectionError:
        await interaction.response.send_message(
            "❌ Cannot connect to LNBits - check network configuration.", ephemeral=True
        )
        logging.error("Invoice creation failed: Connection error to LNBits")
        return
    except requests.exceptions.Timeout:
        await interaction.response.send_message(
            "❌ LNBits connection timed out - Lightning node may be slow.", ephemeral=True
        )
        logging.error("Invoice creation failed: Timeout connecting to LNBits")
        return
    except Exception as e:
        await interaction.response.send_message(
            "❌ Could not create invoice. Please try again later.", ephemeral=True
        )
        logging.error(f"Invoice creation error: {e}")
        return

    if resp.status_code != 201:
        error_msg = get_lnbits_error_message(resp.status_code, resp.text)
        await interaction.response.send_message(error_msg, ephemeral=True)
        logging.error(f"LNBits error response: {resp.status_code} - {resp.text}")
        return

    inv = resp.json()
    pr = inv.get("bolt11")
    h = inv.get("payment_hash")
    if not pr or not h:
        await interaction.response.send_message("❌ Invalid invoice data from LNBits.", ephemeral=True)
        return

    # Store in database for persistence across restarts
    add_pending_invoice(h, interaction.user.id, CHANNEL_ID)
    logging.info(f"Created invoice {h} for user {interaction.user.id}")

    buf = io.BytesIO()
    try:
        await loop.run_in_executor(None, lambda: qrcode.make(pr.upper()).save(buf, format="PNG"))
        buf.seek(0)
        qr_file = File(buf, filename="invoice_qr.png")
    except Exception as e:
        logging.warning(f"QR generation failed: {e}")
        qr_file = None

    embed = Embed(title="⚡ Payment Required ⚡", description=INVOICE_MESSAGE_TEMPLATE)
    embed.add_field(name="Invoice", value=f"```{pr}```", inline=False)
    embed.add_field(name="Amount", value=f"{PRICE} sats", inline=True)
    embed.set_footer(text=h)
    if qr_file:
        embed.set_image(url="attachment://invoice_qr.png")

    try:
        await invoice_channel.send(content=interaction.user.mention, embed=embed, file=qr_file if qr_file else None)
        await interaction.response.send_message("✅ Invoice posted!", ephemeral=True)
    except Exception as e:
        logging.error(f"Error sending invoice message: {e}")
        await interaction.response.send_message("❌ Failed to post invoice.", ephemeral=True)

async def shutdown():
    """Graceful shutdown handler"""
    logging.info("Shutting down bot...")

    # Stop the cleanup task
    if cleanup_expired_invoices_task.is_running():
        cleanup_expired_invoices_task.cancel()

    # Close the bot connection
    await bot.close()
    logging.info("Bot shutdown complete")


def handle_signal(sig, frame):
    """Handle shutdown signals"""
    logging.info(f"Received signal {sig}, initiating shutdown...")
    asyncio.create_task(shutdown())


if __name__ == "__main__":
    logging.info("Starting Discord Lightning Bot...")

    # Register signal handlers for graceful shutdown
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    try:
        bot.run(TOKEN)
    except KeyboardInterrupt:
        logging.info("Received keyboard interrupt")
    finally:
        logging.info("Bot process ended")
