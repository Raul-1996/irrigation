#!/usr/bin/env python3
"""Re-encrypt secrets in the database from the old hostname-based key to the new random key.

Usage:
    python3 migrations/reencrypt_secrets.py [--db PATH] [--dry-run]

This script:
1. Computes the OLD key (hostname-based, from os.uname().nodename)
2. Reads the NEW key from .irrig_secret_key (or generates one)
3. Decrypts telegram_bot_token_encrypted with the old key
4. Re-encrypts with the new key
5. Updates the database

Run this ONCE after upgrading to the new key system.
"""
import argparse
import base64
import os
import sqlite3
import sys

# Add parent directory to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _decrypt_with_key(ciphertext: str, key: bytes) -> str | None:
    """Decrypt a secret using the given key."""
    if not ciphertext:
        return None

    # AES-GCM
    try:
        raw = base64.urlsafe_b64decode(ciphertext)
        if raw.startswith(b'aes:'):
            from Crypto.Cipher import AES
            raw = raw[4:]
            iv, tag, ct = raw[:12], raw[12:28], raw[28:]
            cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
            pt = cipher.decrypt_and_verify(ct, tag)
            return pt.decode('utf-8')
    except Exception:
        pass

    # XOR fallback
    try:
        if ciphertext.startswith('xor:'):
            x = base64.urlsafe_b64decode(ciphertext[4:])
            b = bytes([x[i] ^ key[i % len(key)] for i in range(len(x))])
            return b.decode('utf-8')
    except Exception:
        pass

    return None


def _encrypt_with_key(plaintext: str, key: bytes) -> str:
    """Encrypt a secret using the given key (AES-GCM)."""
    try:
        from Crypto.Cipher import AES
        from Crypto.Random import get_random_bytes
        iv = get_random_bytes(12)
        cipher = AES.new(key[:32], AES.MODE_GCM, nonce=iv)
        ct, tag = cipher.encrypt_and_digest(plaintext.encode('utf-8'))
        blob = b'aes:' + iv + tag + ct
        return base64.urlsafe_b64encode(blob).decode('utf-8')
    except ImportError:
        # XOR fallback if pycryptodome not available
        b = plaintext.encode('utf-8')
        x = bytes([b[i] ^ key[i % len(key)] for i in range(len(b))])
        return 'xor:' + base64.urlsafe_b64encode(x).decode('utf-8')


def main():
    parser = argparse.ArgumentParser(description='Re-encrypt secrets with new random key')
    parser.add_argument('--db', default='irrigation.db', help='Path to SQLite database')
    parser.add_argument('--dry-run', action='store_true', help='Show what would change without writing')
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Database not found: {args.db}")
        sys.exit(1)

    # Compute old key (hostname-based)
    from utils import _get_hostname_key, _get_secret_key
    old_key = _get_hostname_key()
    new_key = _get_secret_key()

    if old_key == new_key:
        print("Old and new keys are identical — nothing to do.")
        return

    print(f"Old key (hostname-based): {old_key[:8].hex()}...")
    print(f"New key (random):         {new_key[:8].hex()}...")

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    reencrypted = 0

    # Re-encrypt telegram_bot_token_encrypted in settings
    cur = conn.execute("SELECT key, value FROM settings WHERE key = 'telegram_bot_token_encrypted' AND value IS NOT NULL AND value != ''")
    row = cur.fetchone()
    if row and row['value']:
        plaintext = _decrypt_with_key(row['value'], old_key)
        if plaintext:
            new_encrypted = _encrypt_with_key(plaintext, new_key)
            print(f"  telegram_bot_token_encrypted: decrypted OK (token: {plaintext[:10]}...)")
            if not args.dry_run:
                conn.execute("UPDATE settings SET value = ? WHERE key = 'telegram_bot_token_encrypted'", (new_encrypted,))
                reencrypted += 1
            else:
                print(f"    [DRY-RUN] Would update to: {new_encrypted[:30]}...")
                reencrypted += 1
        else:
            # Try decrypting with the new key (maybe already migrated)
            plaintext_new = _decrypt_with_key(row['value'], new_key)
            if plaintext_new:
                print(f"  telegram_bot_token_encrypted: already encrypted with new key")
            else:
                print(f"  WARNING: Could not decrypt telegram_bot_token_encrypted with either key!")
    else:
        print("  No telegram_bot_token_encrypted found in settings (or empty)")

    if not args.dry_run:
        conn.commit()
    conn.close()

    action = "Would re-encrypt" if args.dry_run else "Re-encrypted"
    print(f"\n{action} {reencrypted} secret(s).")


if __name__ == '__main__':
    main()
