import os
import io
import hashlib
import logging
import datetime
import requests
from PIL import Image
from bson.objectid import ObjectId

# Configure module logger
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FoundifyAIService")

EMBEDDING_MODEL_NAME = "foundify-image-v1"
EMBEDDING_MODEL_VERSION = "1.0"

def get_ai_api_url():
    """Returns the base URL for the Foundify Image Similarity AI API."""
    url = os.getenv("FOUNDIFY_IMAGE_AI_URL", "https://removal-deployment-volunteer-contest.trycloudflare.com")
    return url.rstrip("/")

def check_ai_health():
    """Checks health of the AI API endpoint."""
    base_url = get_ai_api_url()
    target_url = f"{base_url}/health"
    try:
        response = requests.get(target_url, timeout=5)
        if response.status_code == 200:
            return True, response.json()
        return False, f"Status code: {response.status_code}"
    except Exception as e:
        return False, str(e)

def compute_image_hash(img_bytes):
    """Computes SHA-256 hex digest of image raw bytes for tracking & deduplication."""
    if not img_bytes:
        return ""
    return hashlib.sha256(img_bytes).hexdigest()

def init_database_indexes(db):
    """Initializes required MongoDB Atlas indexes for performance and vector search readiness."""
    if db is None:
        return
    try:
        # 1. db.images indexes
        db.images.create_index([("userId", 1)])
        db.images.create_index([("reportId", 1)])
        db.images.create_index([("itemType", 1)])
        db.images.create_index([("imageHash", 1)])
        db.images.create_index([("uploadedAt", -1)])

        # 2. db.lost_items indexes
        db.lost_items.create_index([("user_id", 1)])
        db.lost_items.create_index([("status", 1)])
        db.lost_items.create_index([("created_at", -1)])
        db.lost_items.create_index([("category", 1)])
        # 3. db.found_items indexes
        db.found_items.create_index([("user_id", 1)])
        db.found_items.create_index([("status", 1)])
        db.found_items.create_index([("created_at", -1)])
        db.found_items.create_index([("category", 1)])

        # 4. db.ai_matches indexes
        db.ai_matches.create_index([("lostReportId", 1)])
        db.ai_matches.create_index([("foundReportId", 1)])
        db.ai_matches.create_index([("similarityScore", -1)])
        db.ai_matches.create_index([("status", 1)])
        db.ai_matches.create_index([("createdAt", -1)])

        # 5. db.training_pairs indexes
        db.training_pairs.create_index([("label", 1)])
        db.training_pairs.create_index([("lostReportId", 1)])
        db.training_pairs.create_index([("foundReportId", 1)])
        db.training_pairs.create_index([("verifiedAt", -1)])

        logger.info("[AI Service] MongoDB Atlas collection indexes verified/created successfully.")
    except Exception as e:
        logger.error(f"[AI Service] Error initializing database indexes: {e}")

def generate_image_embedding(image_input):
    """
    Sends an image to the AI API and returns a 256-dimensional embedding.
    Accepts bytes, BytesIO, PIL Image, or Werkzeug FileStorage.
    Returns a list of 256 floats, or None if the request fails.
    """
    base_url = get_ai_api_url()
    target_url = f"{base_url}/api/v1/image/embed"
    
    logger.info(f"[AI Service] AI request started: POST {target_url}")
    
    try:
        if isinstance(image_input, bytes):
            img_bytes = image_input
        elif hasattr(image_input, 'read'):
            if hasattr(image_input, 'seek'):
                image_input.seek(0)
            img_bytes = image_input.read()
            if hasattr(image_input, 'seek'):
                image_input.seek(0)
        elif isinstance(image_input, Image.Image):
            buf = io.BytesIO()
            image_input.convert('RGB').save(buf, format='JPEG')
            img_bytes = buf.getvalue()
        else:
            logger.error(f"[AI Service] AI request failed: Unsupported image input type: {type(image_input)}")
            return None

        if not img_bytes:
            logger.error("[AI Service] AI request failed: Empty image bytes provided")
            return None

        files = {'file': ('item.jpg', img_bytes, 'image/jpeg')}
        response = requests.post(target_url, files=files, timeout=10)
        
        if response.status_code == 200:
            logger.info("[AI Service] AI request succeeded")
            data = response.json()
            
            embedding = None
            if isinstance(data, dict):
                embedding = data.get("embedding") or data.get("vector") or data.get("data")
            elif isinstance(data, list):
                embedding = data
                
            if isinstance(embedding, dict) and "embedding" in embedding:
                embedding = embedding["embedding"]

            if isinstance(embedding, list) and len(embedding) == 256:
                logger.info(f"[AI Service] embedding generated: received {len(embedding)}-dim vector")
                return [float(x) for x in embedding]
            elif isinstance(embedding, list):
                logger.info(f"[AI Service] embedding generated: received {len(embedding)}-dim vector")
                return [float(x) for x in embedding]
            else:
                logger.error(f"[AI Service] AI request failed: Invalid embedding format: {type(embedding)}")
                return None
        else:
            logger.error(f"[AI Service] AI request failed: Server returned HTTP {response.status_code}: {response.text}")
            return None

    except requests.exceptions.Timeout:
        logger.error("[AI Service] AI request failed: Connection to AI API timed out after 10s")
        return None
    except requests.exceptions.ConnectionError as ce:
        logger.error(f"[AI Service] AI request failed: Connection error: {ce}")
        return None
    except Exception as e:
        logger.error(f"[AI Service] AI request failed: Unexpected error: {e}")
        return None

def calculate_cosine_similarity(vec_a, vec_b):
    """Calculates cosine similarity (dot product) between two normalized 256-dim vectors."""
    if not vec_a or not vec_b or len(vec_a) != len(vec_b):
        return 0.0
    dot_product = sum(a * b for a, b in zip(vec_a, vec_b))
    return max(-1.0, min(1.0, dot_product))

def save_image_record(db, user_id, report_id, item_type, storage_url, original_filename, img_bytes, embedding=None):
    """
    Saves a persistent metadata record in db.images.
    Stores SHA-256 hash, model name, versioning, storage URL, and embedding.
    """
    if db is None:
        return None
        
    try:
        user_obj_id = ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id
        report_obj_id = ObjectId(report_id) if ObjectId.is_valid(report_id) else report_id
        img_hash = compute_image_hash(img_bytes)
        
        now = datetime.datetime.now(datetime.timezone.utc)
        doc = {
            "userId": user_obj_id,
            "reportId": report_obj_id,
            "itemType": item_type,
            "storageUrl": storage_url,
            "originalFileName": original_filename or "item.jpg",
            "mimeType": "image/jpeg",
            "fileSize": len(img_bytes) if img_bytes else 0,
            "uploadedAt": now,
            "imageHash": img_hash,
            "aiEmbedding": embedding,
            "embeddingModel": EMBEDDING_MODEL_NAME,
            "embeddingVersion": EMBEDDING_MODEL_VERSION,
            "aiProcessed": bool(embedding is not None),
            "aiProcessedAt": now if embedding is not None else None
        }
        
        result = db.images.insert_one(doc)
        logger.info(f"[AI Service] Image record stored in db.images (id={result.inserted_id}, hash={img_hash[:8]}...)")
        return result.inserted_id
    except Exception as e:
        logger.error(f"[AI Service] Error saving image record in db.images: {e}")
        return None

def find_similar_found_items(lost_embedding, db, threshold=0.50, top_k=1, lost_report_id=None):
    """
    Searches found_items/images in MongoDB for matches against lost_embedding using cosine similarity.
    Returns candidate dicts sorted by similarity score descending (top_k highest match score only).
    If lost_report_id or candidate found item is solved/resolved, skips matching.
    """
    if not lost_embedding or db is None:
        logger.warning("[AI Service] similarity search completed: Empty embedding or DB unavailable")
        return []

    # If lost report is already solved or resolved, no matching should take place
    if lost_report_id:
        lost_obj = ObjectId(lost_report_id) if ObjectId.is_valid(lost_report_id) else lost_report_id
        lost_doc = db.lost_items.find_one({"_id": lost_obj})
        if lost_doc and lost_doc.get("status") in ["resolved", "solved", "matched", "completed", "handed_over", "claimed"]:
            logger.info(f"[AI Service] Lost report {lost_report_id} is solved/resolved. Skipping AI matching.")
            return []

    # Only evaluate active pending/found items (exclude resolved, solved, matched, completed items)
    found_cursor = db.found_items.find({
        "status": {"$in": ["found", "pending"]},
        "embedding": {"$exists": True, "$ne": None}
    })

    candidates = []
    for item in found_cursor:
        candidate_emb = item.get("embedding")
        if not candidate_emb or not isinstance(candidate_emb, list):
            continue
            
        raw_sim = calculate_cosine_similarity(lost_embedding, candidate_emb)
        
        if raw_sim >= threshold:
            similarity_pct = int(raw_sim * 100)
            if similarity_pct >= 99:
                similarity_pct = 98
                
            item_data = {
                "id": str(item["_id"]),
                "reportId": str(item["_id"]),
                "item_name": item.get("item_name", item.get("title", "Found Item")),
                "description": item.get("description", ""),
                "location": item.get("location", "Unknown Location"),
                "date": item.get("date", "Unknown Date"),
                "category": item.get("category", "General"),
                "image_path": item.get("image_path", "static/images/default_item.png"),
                "user_id": str(item.get("user_id")),
                "similarity_score": round(raw_sim, 4),
                "similarity_pct": similarity_pct,
                "created_at": item.get("created_at")
            }
            candidates.append(item_data)

    # Sort descending by similarity score
    candidates.sort(key=lambda x: x["similarity_score"], reverse=True)
    top_candidates = candidates[:top_k]
    
    logger.info(f"[AI Service] similarity search completed: found {len(top_candidates)} highest match candidate(s) above threshold {threshold}")
    return top_candidates

def find_similar_lost_items(found_embedding, db, threshold=0.50, top_k=1, found_report_id=None):
    """
    Searches lost_items in MongoDB for matches against found_embedding using cosine similarity.
    Excludes solved/resolved items. Returns top_k highest scoring candidates.
    """
    if not found_embedding or db is None:
        return []

    if found_report_id:
        found_obj = ObjectId(found_report_id) if ObjectId.is_valid(found_report_id) else found_report_id
        found_doc = db.found_items.find_one({"_id": found_obj})
        if found_doc and found_doc.get("status") in ["resolved", "solved", "matched", "completed", "handed_over", "claimed"]:
            logger.info(f"[AI Service] Found report {found_report_id} is solved/resolved. Skipping AI matching.")
            return []

    lost_cursor = db.lost_items.find({
        "status": {"$in": ["lost", "pending"]},
        "embedding": {"$exists": True, "$ne": None}
    })

    candidates = []
    for item in lost_cursor:
        candidate_emb = item.get("embedding")
        if not candidate_emb or not isinstance(candidate_emb, list):
            continue
            
        raw_sim = calculate_cosine_similarity(found_embedding, candidate_emb)
        if raw_sim >= threshold:
            similarity_pct = int(raw_sim * 100)
            if similarity_pct >= 99:
                similarity_pct = 98
                
            candidates.append({
                "id": str(item["_id"]),
                "reportId": str(item["_id"]),
                "item_name": item.get("item_name", "Lost Item"),
                "description": item.get("description", ""),
                "location": item.get("location", "Unknown Location"),
                "category": item.get("category", "General"),
                "image_path": item.get("image_path", "static/images/default_item.png"),
                "user_id": str(item.get("user_id")),
                "similarity_score": round(raw_sim, 4),
                "similarity_pct": similarity_pct,
                "created_at": item.get("created_at")
            })

    candidates.sort(key=lambda x: x["similarity_score"], reverse=True)
    return candidates[:top_k]

def create_match_records(db, lost_report_id, lost_image_id, lost_user_id, lost_embedding, threshold=0.50):
    """
    Scans existing found items and inserts ONLY the candidate with the HIGHEST match score into db.ai_matches.
    If the lost item is solved/resolved, skips matching and removes candidate records.
    """
    if db is None or not lost_embedding:
        return []

    lost_report_obj = ObjectId(lost_report_id) if ObjectId.is_valid(lost_report_id) else lost_report_id
    lost_user_obj = ObjectId(lost_user_id) if ObjectId.is_valid(lost_user_id) else lost_user_id
    lost_image_obj = ObjectId(lost_image_id) if lost_image_id and ObjectId.is_valid(lost_image_id) else None

    # Check if lost item is solved/resolved
    lost_doc = db.lost_items.find_one({"_id": lost_report_obj})
    if lost_doc and lost_doc.get("status") in ["resolved", "solved", "matched", "completed", "handed_over", "claimed"]:
        db.ai_matches.delete_many({"lostReportId": lost_report_obj, "status": "candidate"})
        return []

    candidates = find_similar_found_items(lost_embedding, db, threshold=threshold, top_k=1, lost_report_id=lost_report_id)
    if not candidates:
        return []

    # Highest match score candidate ONLY!
    top_cand = candidates[0]
    found_report_obj = ObjectId(top_cand["id"])
    found_user_obj = ObjectId(top_cand["user_id"]) if ObjectId.is_valid(top_cand["user_id"]) else top_cand["user_id"]
    
    found_img_doc = db.images.find_one({"reportId": found_report_obj})
    found_image_obj = found_img_doc["_id"] if found_img_doc else None

    match_doc = {
        "lostReportId": lost_report_obj,
        "lostImageId": lost_image_obj,
        "foundReportId": found_report_obj,
        "foundImageId": found_image_obj,
        "lostUserId": lost_user_obj,
        "foundUserId": found_user_obj,
        "similarityScore": top_cand["similarity_score"],
        "similarityPercentage": top_cand["similarity_pct"],
        "modelVersion": EMBEDDING_MODEL_VERSION,
        "status": "candidate",
        "createdAt": datetime.datetime.now(datetime.timezone.utc)
    }

    match_ids = []
    result = db.ai_matches.update_one(
        {"lostReportId": lost_report_obj, "foundReportId": found_report_obj},
        {"$set": match_doc},
        upsert=True
    )
    if result.upserted_id:
        match_ids.append(result.upserted_id)
            
    logger.info(f"[AI Service] Created single HIGHEST match score record ({top_cand['similarity_pct']}%) in db.ai_matches")
    return match_ids

def create_match_records_for_found(db, found_report_id, found_image_id, found_user_id, found_embedding, threshold=0.50):
    """
    Scans existing lost items when a found item is reported and creates ONLY the single candidate with the HIGHEST match score.
    """
    if db is None or not found_embedding:
        return []

    found_report_obj = ObjectId(found_report_id) if ObjectId.is_valid(found_report_id) else found_report_id
    found_user_obj = ObjectId(found_user_id) if ObjectId.is_valid(found_user_id) else found_user_id
    found_image_obj = ObjectId(found_image_id) if found_image_id and ObjectId.is_valid(found_image_id) else None

    found_doc = db.found_items.find_one({"_id": found_report_obj})
    if found_doc and found_doc.get("status") in ["resolved", "solved", "matched", "completed", "handed_over", "claimed"]:
        db.ai_matches.delete_many({"foundReportId": found_report_obj, "status": "candidate"})
        return []

    candidates = find_similar_lost_items(found_embedding, db, threshold=threshold, top_k=1, found_report_id=found_report_id)
    if not candidates:
        return []

    top_cand = candidates[0]
    lost_report_obj = ObjectId(top_cand["id"])
    lost_user_obj = ObjectId(top_cand["user_id"]) if ObjectId.is_valid(top_cand["user_id"]) else top_cand["user_id"]
    
    lost_img_doc = db.images.find_one({"reportId": lost_report_obj})
    lost_image_obj = lost_img_doc["_id"] if lost_img_doc else None

    match_doc = {
        "lostReportId": lost_report_obj,
        "lostImageId": lost_image_obj,
        "foundReportId": found_report_obj,
        "foundImageId": found_image_obj,
        "lostUserId": lost_user_obj,
        "foundUserId": found_user_obj,
        "similarityScore": top_cand["similarity_score"],
        "similarityPercentage": top_cand["similarity_pct"],
        "modelVersion": EMBEDDING_MODEL_VERSION,
        "status": "candidate",
        "createdAt": datetime.datetime.now(datetime.timezone.utc)
    }

    result = db.ai_matches.update_one(
        {"lostReportId": lost_report_obj, "foundReportId": found_report_obj},
        {"$set": match_doc},
        upsert=True
    )
    match_ids = []
    if result.upserted_id:
        match_ids.append(result.upserted_id)
    logger.info(f"[AI Service] Created single HIGHEST match score record ({top_cand['similarity_pct']}%) for found item in db.ai_matches")
    return match_ids

def record_training_pair(db, lost_report_id, found_report_id, verified_by, status_label):
    """
    Records a verified positive or negative training pair in db.training_pairs for future fine-tuning.
    Updates the status of the candidate match in db.ai_matches to 'confirmed' or 'rejected'.
    """
    if db is None:
        return False
        
    try:
        lost_report_obj = ObjectId(lost_report_id) if ObjectId.is_valid(lost_report_id) else lost_report_id
        found_report_obj = ObjectId(found_report_id) if ObjectId.is_valid(found_report_id) else found_report_id
        verified_by_obj = ObjectId(verified_by) if ObjectId.is_valid(verified_by) else verified_by

        # Update status in db.ai_matches
        new_status = "confirmed" if status_label.lower() in ["confirmed", "positive", "approve"] else "rejected"
        
        db.ai_matches.update_one(
            {"lostReportId": lost_report_obj, "foundReportId": found_report_obj},
            {"$set": {"status": new_status, "updatedAt": datetime.datetime.now(datetime.timezone.utc)}}
        )

        # Retrieve report and image documents
        lost_item = db.lost_items.find_one({"_id": lost_report_obj})
        found_item = db.found_items.find_one({"_id": found_report_obj})

        lost_img_doc = db.images.find_one({"reportId": lost_report_obj})
        found_img_doc = db.images.find_one({"reportId": found_report_obj})

        lost_embedding = lost_item.get("embedding") if lost_item else (lost_img_doc.get("aiEmbedding") if lost_img_doc else None)
        found_embedding = found_item.get("embedding") if found_item else (found_img_doc.get("aiEmbedding") if found_img_doc else None)

        score = calculate_cosine_similarity(lost_embedding, found_embedding) if (lost_embedding and found_embedding) else 0.0

        pair_doc = {
            "lostReportId": lost_report_obj,
            "lostImageId": lost_img_doc["_id"] if lost_img_doc else None,
            "foundReportId": found_report_obj,
            "foundImageId": found_img_doc["_id"] if found_img_doc else None,
            "lostEmbedding": lost_embedding,
            "foundEmbedding": found_embedding,
            "similarityScore": round(score, 4),
            "label": "positive" if new_status == "confirmed" else "negative",
            "verifiedBy": verified_by_obj,
            "verifiedAt": datetime.datetime.now(datetime.timezone.utc)
        }

        db.training_pairs.update_one(
            {"lostReportId": lost_report_obj, "foundReportId": found_report_obj},
            {"$set": pair_doc},
            upsert=True
        )

        logger.info(f"[AI Service] Recorded training pair ({pair_doc['label']}) in db.training_pairs for fine-tuning dataset.")
        return True
    except Exception as e:
        logger.error(f"[AI Service] Error recording training pair: {e}")
        return False
