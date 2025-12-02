"""
SQLite database module for pending invoice persistence.
"""
import sqlite3
import os
import logging
from datetime import datetime, timedelta
from contextlib import contextmanager

# Configuration
DATA_DIR = os.environ.get('APP_DATA_DIR', '/app/data')
DB_FILE = os.path.join(DATA_DIR, 'bot.db')

# Invoice expiry time (1 hour)
INVOICE_EXPIRY_HOURS = 1


def init_db():
    """Initialize the database and create tables if they don't exist."""
    os.makedirs(DATA_DIR, exist_ok=True)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS pending_invoices (
                payment_hash TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        conn.commit()
        logging.info(f"Database initialized at {DB_FILE}")


@contextmanager
def get_connection():
    """Context manager for database connections."""
    conn = sqlite3.connect(DB_FILE, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()


def add_pending_invoice(payment_hash: str, user_id: int, channel_id: int) -> bool:
    """
    Add a pending invoice to the database.

    Returns True if successful, False if payment_hash already exists.
    """
    try:
        with get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute(
                'INSERT INTO pending_invoices (payment_hash, user_id, channel_id) VALUES (?, ?, ?)',
                (payment_hash, user_id, channel_id)
            )
            conn.commit()
            logging.debug(f"Added pending invoice: {payment_hash}")
            return True
    except sqlite3.IntegrityError:
        logging.warning(f"Invoice already exists: {payment_hash}")
        return False


def get_pending_invoice(payment_hash: str) -> dict | None:
    """
    Get a pending invoice by payment hash.

    Returns dict with user_id, channel_id, created_at or None if not found.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'SELECT user_id, channel_id, created_at FROM pending_invoices WHERE payment_hash = ?',
            (payment_hash,)
        )
        row = cursor.fetchone()

        if row:
            return {
                'user_id': row['user_id'],
                'channel_id': row['channel_id'],
                'created_at': row['created_at']
            }
        return None


def remove_pending_invoice(payment_hash: str) -> bool:
    """
    Remove a pending invoice from the database.

    Returns True if an invoice was removed, False otherwise.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM pending_invoices WHERE payment_hash = ?',
            (payment_hash,)
        )
        conn.commit()
        removed = cursor.rowcount > 0

        if removed:
            logging.debug(f"Removed pending invoice: {payment_hash}")

        return removed


def get_all_pending_invoices() -> list[dict]:
    """
    Get all pending invoices.

    Returns list of dicts with payment_hash, user_id, channel_id, created_at.
    """
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT payment_hash, user_id, channel_id, created_at FROM pending_invoices')
        rows = cursor.fetchall()

        return [
            {
                'payment_hash': row['payment_hash'],
                'user_id': row['user_id'],
                'channel_id': row['channel_id'],
                'created_at': row['created_at']
            }
            for row in rows
        ]


def cleanup_expired_invoices() -> int:
    """
    Remove invoices older than INVOICE_EXPIRY_HOURS.

    Returns the number of invoices removed.
    """
    expiry_time = datetime.now() - timedelta(hours=INVOICE_EXPIRY_HOURS)

    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM pending_invoices WHERE created_at < ?',
            (expiry_time,)
        )
        conn.commit()
        removed = cursor.rowcount

        if removed > 0:
            logging.info(f"Cleaned up {removed} expired invoice(s)")

        return removed


def get_pending_invoice_count() -> int:
    """Get the number of pending invoices."""
    with get_connection() as conn:
        cursor = conn.cursor()
        cursor.execute('SELECT COUNT(*) FROM pending_invoices')
        return cursor.fetchone()[0]
