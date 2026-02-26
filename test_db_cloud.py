import os
import sys
from urllib.parse import urlparse
import psycopg2
from sqlalchemy import create_engine
from dotenv import load_dotenv

# Load environment variables from .env
load_dotenv()

def test_connection():
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        print("❌ DATABASE_URL not found in .env")
        return

    print(f"🔄 Attempting to connect to: {db_url.split('@')[-1]}")
    
    # 1. Test with psycopg2
    print("\n--- Testing with psycopg2 ---")
    try:
        conn = psycopg2.connect(db_url, connect_timeout=5)
        print("✅ psycopg2: Success!")
        conn.close()
    except Exception as e:
        print(f"❌ psycopg2: Failed\n   Error: {e}")

    # 2. Test with SQLAlchemy
    print("\n--- Testing with SQLAlchemy ---")
    try:
        engine = create_engine(db_url)
        with engine.connect() as conn:
            print("✅ SQLAlchemy: Success!")
    except Exception as e:
        print(f"❌ SQLAlchemy: Failed\n   Error: {e}")

    # 3. Check public IP
    print("\n--- System Diagnostics ---")
    try:
        from urllib.request import urlopen
        ip = urlopen('https://api.ipify.org').read().decode('utf8')
        print(f"🌍 Your public IP: {ip}")
        print("👉 Make sure this IP is whitelisted in DigitalOcean 'Trusted Sources'.")
    except Exception as e:
        print(f"⚠️  Could not fetch public IP: {e}")

if __name__ == "__main__":
    test_connection()
