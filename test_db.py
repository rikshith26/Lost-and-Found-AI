import os
from dotenv import load_dotenv
from pymongo import MongoClient
import certifi

load_dotenv()

MONGO_URI = os.environ.get("MONGO_URI")
DB_NAME = os.environ.get("DB_NAME")

print(f"Connecting to: {MONGO_URI}")
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tlsAllowInvalidCertificates=True)
    client.server_info()
    print("SUCCESS (Unsafe Mode): Connected to MongoDB Atlas")
    db = client[DB_NAME]
    print(f"Database: {db.name}")
except Exception as e:
    print(f"FAILED (Unsafe Mode): {e}")

