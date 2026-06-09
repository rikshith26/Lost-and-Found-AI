from pymongo import MongoClient

print("Checking Local MongoDB...")
try:
    client = MongoClient("mongodb://localhost:27017/", serverSelectionTimeoutMS=2000)
    client.server_info()
    print("SUCCESS: Local MongoDB is running")
except Exception as e:
    print(f"FAILED: Local MongoDB not available: {e}")
