import os
import io
import re
import torch
import numpy as np
from PIL import Image
from flask import current_app

# Global Model Singletons (Lazy loaded for optimal startup & resource management)
_clip_model = None
_clip_processor = None
_device = None

def get_clip_engine():
    global _clip_model, _clip_processor, _device
    if _clip_model is None:
        try:
            from transformers import CLIPProcessor, CLIPModel
            model_name = "openai/clip-vit-base-patch32"
            _device = "cuda" if torch.cuda.is_available() else "cpu"
            print(f"[AI Scanner] Loading Hugging Face CLIP model on {_device.upper()}...")
            _clip_model = CLIPModel.from_pretrained(model_name).to(_device)
            _clip_processor = CLIPProcessor.from_pretrained(model_name)
            _clip_model.eval()
            print(f"[AI Scanner] Hugging Face CLIP model initialized successfully!")
        except Exception as e:
            print(f"[AI Scanner] Failed to load CLIP model: {e}")
            _clip_model = None
            _clip_processor = None
    return _clip_model, _clip_processor, _device

# ---------- IMAGE LOADER (Supports GridFS, Absolute Paths & Relative Paths) ----------
def load_image_pil(img_path):
    if not img_path:
        return None
    try:
        # 1. MongoDB GridFS images
        if str(img_path).startswith("db_uploads/"):
            file_id = str(img_path).split("/")[1]
            from app import get_db
            import gridfs
            from bson.objectid import ObjectId
            
            db = get_db()
            if db is not None:
                fs = gridfs.GridFS(db)
                grid_out = fs.get(ObjectId(file_id))
                img_bytes = grid_out.read()
                return Image.open(io.BytesIO(img_bytes)).convert('RGB')
        
        # 2. Local filesystem images
        candidates = [
            img_path,
            os.path.join(os.getcwd(), img_path) if not os.path.isabs(img_path) else None,
            os.path.join(current_app.root_path, img_path) if current_app and not os.path.isabs(img_path) else None
        ]
        for path in candidates:
            if path and os.path.exists(path) and os.path.isfile(path):
                return Image.open(path).convert('RGB')
                
    except Exception as e:
        print(f"[AI Scanner] Image loading error ({img_path}): {e}")
    return None

# ---------- EXTRACT TEXT EMBEDDING ----------
def get_text_embedding(text):
    if not text or not text.strip():
        return None
    model, processor, device = get_clip_engine()
    if model is None or processor is None:
        return None
    try:
        clean_txt = re.sub(r'[^a-zA-Z0-9\s,.-]', '', text).strip()[:180]
        if not clean_txt:
            return None
        inputs = processor(text=[clean_txt], return_tensors="pt", padding=True, truncation=True).to(device)
        with torch.no_grad():
            outputs = model.get_text_features(**inputs)
            features = outputs.pooler_output if hasattr(outputs, 'pooler_output') else outputs
            # L2 Normalize
            features = features / torch.norm(features, p=2, dim=-1, keepdim=True)
            return features
    except Exception as e:
        print(f"[AI Scanner] Text embedding error: {e}")
        return None

# ---------- EXTRACT IMAGE EMBEDDING ----------
def get_image_embedding(pil_image):
    if pil_image is None:
        return None
    model, processor, device = get_clip_engine()
    if model is None or processor is None:
        return None
    try:
        inputs = processor(images=[pil_image], return_tensors="pt").to(device)
        with torch.no_grad():
            outputs = model.get_image_features(**inputs)
            features = outputs.pooler_output if hasattr(outputs, 'pooler_output') else outputs
            # L2 Normalize
            features = features / torch.norm(features, p=2, dim=-1, keepdim=True)
            return features
    except Exception as e:
        print(f"[AI Scanner] Image embedding error: {e}")
        return None

# ---------- CALIBRATION UTILITIES ----------
def calibrate_image_similarity(raw_sim):
    """
    CLIP image-image raw cosine baseline:
    - Random/unrelated photos (e.g. book vs pen): ~0.20 - 0.35 -> 0% - 5%
    - Moderately similar / same color domain: ~0.50 - 0.65 -> 25% - 55%
    - Strong / exact item match: ~0.75 - 0.95+ -> 75% - 100%
    """
    if raw_sim is None or raw_sim <= 0.35:
        return 0
    calibrated = ((raw_sim - 0.35) / 0.55) * 100
    return int(round(min(100, max(0, calibrated))))

def calibrate_text_similarity(raw_sim):
    """
    CLIP text-text raw cosine baseline:
    - Different concepts (e.g. 'book' vs 'blue pen'): ~0.50 - 0.70 -> 0% - 5%
    - Related categories: ~0.75 - 0.82 -> 20% - 50%
    - High semantic / exact match: ~0.88 - 1.00 -> 75% - 100%
    """
    if raw_sim is None or raw_sim <= 0.70:
        return 0
    calibrated = ((raw_sim - 0.70) / 0.25) * 100
    return int(round(min(100, max(0, calibrated))))

# ---------- MULTI-MODAL MATCHING FUNCTION ----------
def final_match(lost, found):
    lost_title = lost.get("item_name", "")
    lost_desc = lost.get("description", "")
    lost_full_text = f"{lost_title}. {lost_desc}".strip()
    
    found_title = found.get("item_name", "")
    found_desc = found.get("description", "")
    found_full_text = f"{found_title}. {found_desc}".strip()

    # 1. Load Images
    lost_img_pil = load_image_pil(lost.get("image_path"))
    found_img_pil = load_image_pil(found.get("image_path"))

    # 2. Extract Embeddings
    lost_img_emb = get_image_embedding(lost_img_pil) if lost_img_pil else None
    found_img_emb = get_image_embedding(found_img_pil) if found_img_pil else None

    lost_txt_emb = get_text_embedding(lost_full_text)
    found_txt_emb = get_text_embedding(found_full_text)

    # 3. Compute Raw Similarities
    # Image vs Image
    has_both_images = (lost_img_emb is not None and found_img_emb is not None)
    if has_both_images:
        raw_img_sim = torch.cosine_similarity(lost_img_emb, found_img_emb).item()
        image_score = calibrate_image_similarity(raw_img_sim)
    else:
        image_score = 0

    # Text vs Text
    if lost_txt_emb is not None and found_txt_emb is not None:
        raw_txt_sim = torch.cosine_similarity(lost_txt_emb, found_txt_emb).item()
        text_score = calibrate_text_similarity(raw_txt_sim)
    else:
        text_score = 0

    # 4. Final Weighted Overall Score (as an integer percentage 0-100)
    if has_both_images:
        # Both photos available: 60% Visual, 40% Text Description
        final_score = int(round((image_score * 0.60) + (text_score * 0.40)))
    elif lost_img_emb is not None or found_img_emb is not None:
        # Cross check if one image exists
        cross_scores = []
        if lost_img_emb is not None and found_txt_emb is not None:
            raw_c1 = torch.cosine_similarity(lost_img_emb, found_txt_emb).item()
            cross_scores.append(calibrate_text_similarity(raw_c1))
        if found_img_emb is not None and lost_txt_emb is not None:
            raw_c2 = torch.cosine_similarity(found_img_emb, lost_txt_emb).item()
            cross_scores.append(calibrate_text_similarity(raw_c2))
        cross_score = int(round(float(np.mean(cross_scores)))) if cross_scores else 0
        final_score = int(round((cross_score * 0.50) + (text_score * 0.50)))
        image_score = cross_score
    else:
        # Text only
        final_score = text_score

    return {
        "text_score": text_score,
        "image_score": image_score,
        "final_score": final_score
    }
