from flask import (
    Flask, render_template, request, redirect,
    session, abort, url_for, send_from_directory, flash, send_file
)
import pillow_heif
from PIL import Image
pillow_heif.register_heif_opener()

from flask_socketio import SocketIO, emit, join_room, leave_room
from pymongo import MongoClient
from bson.objectid import ObjectId
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename
import os
from dotenv import load_dotenv

load_dotenv()
import datetime
from typing import Any
from flask_mail import Mail, Message
from itsdangerous import URLSafeTimedSerializer
import image_similarity_service
import re

import requests
import json
from flask import Response, make_response, g
from fpdf import FPDF
import io
import qrcode

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "super-secret-key-fallback")

# PROXY FIX: Trust headers from Render/Heroku/AWS (X-Forwarded-Proto)
from werkzeug.middleware.proxy_fix import ProxyFix
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1, x_host=1, x_prefix=1)

socketio = SocketIO(app, cors_allowed_origins="*")

# ---------- MAIL CONFIGURATION ----------
app.config['MAIL_SERVER'] = 'smtp.gmail.com'
app.config['MAIL_PORT'] = 587
app.config['MAIL_USE_TLS'] = True
app.config['MAIL_USERNAME'] = os.environ.get("MAIL_USERNAME", "")
app.config['MAIL_PASSWORD'] = os.environ.get("MAIL_PASSWORD", "")
app.config['MAIL_DEFAULT_SENDER'] = os.environ.get("MAIL_USERNAME", "")


mail = Mail(app)
serializer = URLSafeTimedSerializer(app.secret_key)

# ---------- GOOGLE AUTH CONFIG ----------
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
GOOGLE_DISCOVERY_URL = "https://accounts.google.com/.well-known/openid-configuration"

def get_google_provider_cfg():
    return requests.get(GOOGLE_DISCOVERY_URL).json()

# ---------- MIDDLEWARE: BLOCK CHECK & SESSION INVALIDATION ----------
@app.before_request
def check_user_status():
    # Define exempt paths that don't need status checks
    exempt_paths = ['/login', '/logout', '/account-blocked', '/request-unblock', '/static', '/auth/check-status', '/verify-admin', '/api/qrcode']
    
    # Skip if path starts with exempt prefix (simple string check)
    for path in exempt_paths:
        if request.path.startswith(path) or request.path == "/":
            return

    # If user is logged in, check status
    if "user_id" in session:
        db = get_db()
        if db is None:
            return
            
        user = db.users.find_one({"_id": ObjectId(session["user_id"])})
        
        # DEBUG LOGGING
        print(f"Middleware Check: UserID={session['user_id']} | Found={bool(user)} | Active={user.get('is_active') if user else 'N/A'}")

        if not user:
            # User in session but not in DB? specific edge case. Log them out.
            session.clear()
            return redirect("/login")
        
        # If user is blocked (is_active=False)
        if not user.get("is_active", True):
            print(">> BLOCKING USER - REDIRECTING <<")
            # Force clear any residual 'next' redirects
            return redirect("/account-blocked")

        # Session Version Check
        db_version = user.get("session_version", 0)
        session_ver = session.get("session_version", 0)
        
        if db_version != session_ver:
             print(f"Session Version Mismatch: DB={db_version} vs Session={session_ver} - Logging Out")
             session.clear()
             return redirect("/login")
             
        g.user = user

        # Check Admin T&C Acceptance
        if user.get("role") == "admin" and not user.get("admin_terms_accepted"):
            allowed = ["/admin/dashboard", "/admin/accept-terms-post", "/logout", "/admin-terms-content"]
            if request.path not in allowed and not request.path.startswith("/static"):
                return redirect("/admin/dashboard")
                
        # Check User T&C Acceptance
        if user.get("role") == "user" and not user.get("terms_accepted_at"):
            allowed = ["/user/dashboard", "/user/accept-terms-post", "/logout", "/terms"]
            if request.path not in allowed and not request.path.startswith("/static"):
                return redirect("/user/dashboard")

# ---------- STATUS POLLER ----------
@app.route("/auth/check-status")
def check_status():
    if "user_id" not in session:
        return {"status": "unauthorized"}, 401
        
    db = get_db()
    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    
    if not user:
        return {"status": "invalid_session"}, 401
        
    if not user.get("is_active", True):
        return {"status": "inactive"}
        
    # Check session version
    if user.get("session_version", 0) != session.get("session_version", 0):
         return {"status": "invalid_session"}
         
    return {"status": "active"}

# ---------- PREVENT CACHING ----------
@app.after_request
def add_header(response):
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, post-check=0, pre-check=0, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '-1'
    return response

# ---------- UPLOAD CONFIG ----------
os.makedirs("uploads/lost", exist_ok=True)
os.makedirs("uploads/found", exist_ok=True)
os.makedirs("uploads/profile", exist_ok=True)

# ---------- SERVE UPLOADS ----------
@app.route('/uploads/<path:filename>')
def uploaded_file(filename):
    return send_from_directory('uploads', filename)

# ---------- MONGODB CONNECTION ----------
MONGO_URI = os.environ.get("MONGO_URI", "")
DB_NAME = os.environ.get("DB_NAME", "lost_found_ai")


import certifi

mongo_client = None

def get_db() -> Any:
    global mongo_client
    if mongo_client is None:
        try:
            # 1. Try with Certifi (Best Practice)
            print("Connecting to MongoDB (Certifi Mode)...")
            client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tlsCAFile=certifi.where())
            client.server_info() # Trigger connection check
            mongo_client = client
            print("SUCCESS: Connected to MongoDB Atlas (Certifi Mode)")
        except Exception as e:
            print(f"Certifi Connection Failed: {e}")
            try:
                # 2. Retry with Standard (System Certs)
                print("Retrying with Standard Connection...")
                client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
                client.server_info()
                mongo_client = client
                print("SUCCESS: Connected to MongoDB Atlas (Standard Mode)")
            except Exception as e2:
                print(f"Standard Connection Failed: {e2}")
                try:
                    # 3. Retry with SSL/TLS bypass (Unsafe/Dev)
                    print("Retrying with tlsAllowInvalidCertificates=True...")
                    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000, tlsAllowInvalidCertificates=True)
                    client.server_info()
                    mongo_client = client
                    print("SUCCESS: Connected to MongoDB Atlas (Unsafe TLS Mode)")
                except Exception as e3:
                    print(f"CRITICAL: Could not connect to MongoDB. {e3}")
                    return None
    return mongo_client[DB_NAME] if mongo_client else None


# ---------- SUPER ADMIN AUTO-CREATION ----------
SEED_ADMIN_PASSWORD = os.environ.get("DEFAULT_ADMIN_PASSWORD", "FoundifyAdmin2026!")
SEED_ADMINS = [
    {"name": "ADULA MAHENDHAR", "email": "yadavmahendhar65@gmail.com", "password": SEED_ADMIN_PASSWORD},
    {"name": "MOHAMMED YASEEN", "email": "yaseenashu18@gmail.com", "password": SEED_ADMIN_PASSWORD},
    {"name": "MILKURI VAMSHI KRISHNA", "email": "krishnapatel000813@gmail.com", "password": SEED_ADMIN_PASSWORD},
    {"name": "BURRA RIKSHITH", "email": "burrarikshith@gmail.com", "password": SEED_ADMIN_PASSWORD}
]

def init_super_admin():
    db = get_db()
    if db is None:
        return

    users_col = db.users
    
    for admin in SEED_ADMINS:
        existing_user = users_col.find_one({"email": admin["email"]})
        if not existing_user:
            password_hash = generate_password_hash(admin["password"])
            super_admin_data = {
                "name": admin["name"],
                "email": admin["email"],
                "password": password_hash,
                "role": "super_admin",
                "is_active": True,
                "profile_completed": True,
                "college": "System",
                "study": "Administration",
                "phone": "0000000000",
                "profile_photo": None,
                "created_at": datetime.datetime.now(datetime.timezone.utc)
            }
            users_col.insert_one(super_admin_data)
            print(f"Seeded Super Admin: {admin['email']}")

    # Initialize MongoDB Atlas Indexes
    image_similarity_service.init_database_indexes(db)

# Initialize user on startup
init_super_admin()


# ---------- HELPER FUNCTIONS ----------
def is_valid_password(password):
    """
    Enforces password policy:
    - Minimum 8 characters
    - At least one special character
    """
    if len(password) < 8:
        return False
    if not re.search(r"[!@#$%^&*(),.?\":{}|<>]", password):
        return False
    return True

def get_user_by_id(user_id_str):
    if not user_id_str:
        return None
    db = get_db()
    try:
        return db.users.find_one({"_id": ObjectId(user_id_str)})
    except:
        return None

# ---------- SERVE UPLOADS (LOCAL) ----------
from urllib.parse import unquote
import gridfs
from io import BytesIO

@app.route("/uploads/<path:filename>")
def uploaded_files(filename):
    filename = unquote(filename)
    return send_from_directory("uploads", filename)

# ---------- SERVE UPLOADS (DATABASE) ----------
@app.route("/db_uploads/<file_id>")
def serve_db_upload(file_id):
    try:
        db = get_db()
        if db is None:
            abort(500)
            
        fs = gridfs.GridFS(db)
        grid_out = fs.get(ObjectId(file_id))
        
        response = make_response(grid_out.read())
        response.mimetype = grid_out.content_type
        # Add cache headers since DB images are immutable
        response.headers['Cache-Control'] = 'public, max-age=31536000'
        return response
    except Exception as e:
        print(f"Error serving DB image: {e}")
        return send_from_directory('static', 'images/default_item.png')

@app.route("/")
def index():
    if "user_id" in session:
        db = get_db()
        if db is not None:
            user = db.users.find_one({"_id": ObjectId(session["user_id"])})
            if user:
                # If logged in, send them to their dashboard
                if user.get("role") == "super_admin":
                    return redirect("/superadmin/dashboard")
                elif user.get("role") == "admin":
                    return redirect("/admin/dashboard")
                else:
                    return redirect("/user/dashboard")
    return render_template("index.html")

@app.route("/terms")
def terms():
    return render_template("terms.html")

@app.route("/prompt-login")
def prompt_login():
    flash("Please register or login to our website to access these features!", "info")
    return redirect("/login")

# ---------- LOGIN ----------
@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]

        db = get_db()
        if db is None:
            flash("System busy/unavailable. Please try again.", "error")
            return redirect("/login")
            
        # Find user regardless of active status
        user = db.users.find_one({"email": email})

        if user and check_password_hash(user["password"], password):
            session["user_id"] = str(user["_id"])
            session["role"] = user["role"]
            session["session_version"] = user.get("session_version", 0)

            # If blocked, redirect to blocked page immediately
            if not user.get("is_active", True):
                return redirect("/account-blocked")

            if user["role"] == "super_admin":
                return redirect("/superadmin/dashboard")
            elif user["role"] == "admin":
                return redirect("/admin/dashboard")
            else:
                return redirect("/user/dashboard")
        
        flash("Invalid email or password", "error")
        return redirect("/login")

    return render_template("login.html")

# ---------- FORGOT PASSWORD ----------
@app.route("/forgot-password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"]
        db = get_db()
        user = db.users.find_one({"email": email})

        if user:
            token = serializer.dumps(email, salt='password-reset-salt')
            link = url_for('reset_password', token=token, _external=True)
            
            # DEFAULT: Print link to console for testing/development
            print(f"\n==================================================")
            print(f"PASSWORD RESET LINK (Click to test):")
            print(f"{link}")
            
            print(f"==================================================\n")

            msg = Message("Reset your password", recipients=[email])
            # Render HTML template with the link
            msg.html = render_template("email_reset.html", link=link)
            
            try:
                mail.send(msg)
                flash("Reset link sent to your email!", "success")
            except Exception as e:
                print(f"Error sending email: {e}")
                flash("Error sending email. Please try again later.", "danger")
        
        else:
             # Consistent message to prevent user enumeration
             flash("If an account exists, a reset link has been sent.", "info")
             
        return redirect(url_for('login'))

    return render_template("forgot_password.html")

@app.route("/reset-password/<token>", methods=["GET", "POST"])
def reset_password(token):
    try:
        email = serializer.loads(token, salt='password-reset-salt', max_age=3600) # 1 hour expiration
    except:
        flash("The reset link is invalid or has expired.", "danger")
        return redirect(url_for('login'))

    if request.method == "POST":
        password = request.form["password"]
        confirm_password = request.form["confirm_password"]

        if password != confirm_password:
            flash("Passwords do not match.", "danger")
            return redirect(url_for('reset_password', token=token))
        
        if not is_valid_password(password):
             flash("Password must be at least 8 characters and contain a special character.", "danger")
             return redirect(url_for('reset_password', token=token))

        db = get_db()
        hashed_password = generate_password_hash(password)
        db.users.update_one({"email": email}, {"$set": {"password": hashed_password}})
        
        flash("Your password has been updated! You can now log in.", "success")
        return redirect(url_for('login'))

    return render_template("reset_password.html", token=token)

# ---------- SIGNUP ----------
@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        
        # Domain Restriction Logic
        allowed_domains = ['paruluniversity.ac.in']
        domain = email.split('@')[-1] if '@' in email else ''
        
        if domain not in allowed_domains:
            flash(f"Registration is restricted to authorized domains ({', '.join(allowed_domains)}).")
            return redirect(request.url)

        if not is_valid_password(request.form["password"]):
            flash("Password must be at least 8 characters long and include a special character.")
            return redirect(request.url)

        password = generate_password_hash(request.form["password"])

        db = get_db()
        
        # Check if email exists
        if db.users.find_one({"email": email}):
            flash("Email already registered")
            return redirect(request.url)

        try:
            db.users.insert_one({
                "name": name,
                "email": email,
                "password": password,
                "role": "user",
                "is_active": True,
                "profile_completed": False,
                "created_at": datetime.datetime.now(datetime.timezone.utc),
                "terms_accepted_at": datetime.datetime.now(datetime.timezone.utc)
            })
        except Exception as e:
            return f"Signup error: {e}"

        return redirect("/login")

    return render_template("signup.html")

# ---------- GOOGLE OAUTH ROUTES ----------
@app.route("/google-login")
def google_login():
    # Get Google Provider Config
    google_provider_cfg = get_google_provider_cfg()
    authorization_endpoint = google_provider_cfg["authorization_endpoint"]

    # Redirect request to Google for authentication
    # Using manual URL construction to avoid external library dependencies
    return redirect(f"{authorization_endpoint}?response_type=code&client_id={GOOGLE_CLIENT_ID}&redirect_uri={url_for('google_callback', _external=True)}&scope=openid%20email%20profile")

@app.route("/auth/google/callback")
def google_callback():
    # Get Authorization Code
    code = request.args.get("code")
    if not code:
        flash("Google sign-in failed: missing authorization code.", "error")
        return redirect("/login")
    
    # Find Token Endpoint
    google_provider_cfg = get_google_provider_cfg()
    token_endpoint = google_provider_cfg["token_endpoint"]
    
    
    # Exchange Code for Token
    token_response = requests.post(
        token_endpoint,
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": url_for("google_callback", _external=True),
            "grant_type": "authorization_code"
        },
        timeout=15
    )
    if token_response.status_code != 200:
        flash("Google sign-in failed while fetching token. Please try again.", "error")
        return redirect("/login")
    
    # Parse User Info
    userinfo_endpoint = google_provider_cfg["userinfo_endpoint"]
    token_json = token_response.json()
    access_token = token_json.get("access_token")
    if not access_token:
        flash("Google sign-in failed: access token missing.", "error")
        return redirect("/login")

    userinfo_response = requests.get(
        userinfo_endpoint,
        headers={"Authorization": f"Bearer {access_token}"},
        timeout=15
    )
    if userinfo_response.status_code != 200:
        flash("Google sign-in failed while fetching user profile.", "error")
        return redirect("/login")
    
    if userinfo_response.json().get("email_verified"):
        unique_id = userinfo_response.json()["sub"]
        users_email = userinfo_response.json()["email"]
        users_name = userinfo_response.json()["given_name"]
        
        # Logic: Login or Register
        db = get_db()
        if db is None:
            flash("Database is currently unavailable. Please try again shortly.", "error")
            return redirect("/login")
        user = db.users.find_one({"email": users_email})
        
        if not user:
            # Domain Restriction Logic
            allowed_domains = ['paruluniversity.ac.in']
            domain = users_email.split('@')[-1] if '@' in users_email else ''
            
            if domain not in allowed_domains:
                flash(f"Registration is restricted to authorized domains ({', '.join(allowed_domains)}).", "error")
                return redirect("/login")

            # Register
            db.users.insert_one({
                "name": users_name,
                "email": users_email,
                "password": None, # Google User
                "role": "user",
                "is_active": True,
                "profile_completed": False,
                "auth_provider": "google",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
                "terms_accepted_at": datetime.datetime.now(datetime.timezone.utc)
            })
            user = db.users.find_one({"email": users_email})
            
        # Session
        session["user_id"] = str(user["_id"])
        session["role"] = user["role"]
        session["session_version"] = user.get("session_version", 0) # Sync session version

        # Intelligent Redirect based on Role
        if user["role"] == "super_admin":
            return redirect("/superadmin/dashboard")
        elif user["role"] == "admin":
            return redirect("/admin/dashboard")
        else:
            return redirect("/user/dashboard")
        
    return "User email not available or not verified by Google.", 400

# ---------- ADMIN T&C ----------
@app.route("/admin/accept-terms-post", methods=["POST"])
def admin_accept_terms_post():
    if session.get("role") != "admin":
        return redirect("/")
        
    db = get_db()
    db.users.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {
            "admin_terms_accepted": True,
            "admin_terms_accepted_at": datetime.datetime.now(datetime.timezone.utc)
        }}
    )
    return redirect("/admin/dashboard")

@app.route("/admin-terms-content")
def admin_terms_content():
    return render_template("admin_terms_content.html")

# ---------- USER T&C ----------
@app.route("/user/accept-terms-post", methods=["POST"])
def user_accept_terms_post():
    if session.get("role") != "user":
        return redirect("/")
        
    db = get_db()
    db.users.update_one(
        {"_id": ObjectId(session["user_id"])},
        {"$set": {
            "terms_accepted_at": datetime.datetime.now(datetime.timezone.utc)
        }}
    )
    return redirect("/user/dashboard")

# ---------- SUPER ADMIN DASHBOARD ----------
@app.route("/superadmin/dashboard")
def superadmin_dashboard():
    if session.get("role") != "super_admin":
        abort(403)
        
    db = get_db()
    if db is None:
        flash("Database unavailable.", "error")
        return redirect("/")
    
    # Calculate System Stats
    total_users = db.users.count_documents({"role": "user"})
    total_admins = db.users.count_documents({"role": "admin"})
    
    total_lost = db.lost_items.count_documents({})
    total_found = db.found_items.count_documents({})
    
    resolved_matches = db.chats.count_documents({})
    
    resolution_rate = 0
    if total_lost > 0:
        resolution_rate = int((resolved_matches / total_lost) * 100)
        
    pending_unblocks = db.unblock_requests.count_documents({"status": "pending"})
    
    recent_admins = list(db.users.find({"role": "admin"}).sort("created_at", -1).limit(5))
    
    # Fetch Candidate Matches from db.ai_matches collection for Super Admin verification
    matches = []
    try:
        pending_matches_cursor = list(db.ai_matches.find({"status": "candidate"}).sort("similarityScore", -1))
        
        for m in pending_matches_cursor:
            lost = db.lost_items.find_one({"_id": m["lostReportId"], "status": "lost"})
            found = db.found_items.find_one({"_id": m["foundReportId"], "status": "found"})
            
            if lost and found:
                lost['lost_id'] = str(lost['_id'])
                found['found_id'] = str(found['_id'])
                sim_pct = m.get("similarityPercentage", int(round(m.get("similarityScore", 0) * 100)))
                
                matches.append({
                    "lost": lost,
                    "found": found,
                    "score": {
                        "final_score": sim_pct,
                        "image_score": sim_pct,
                        "text_score": sim_pct
                    }
                })
    except Exception as e:
        print(f"Superadmin Pending AI Matches Fetch Error: {e}")

    pending_matches_count = len(matches)

    return render_template("superadmin_dashboard.html", 
                           total_users=total_users, 
                           total_admins=total_admins, 
                           total_lost=total_lost, 
                           total_found=total_found, 
                           resolution_rate=resolution_rate,
                           pending_unblocks=pending_unblocks,
                           recent_admins=recent_admins,
                           matches=matches,
                           pending_matches_count=pending_matches_count)

# ---------- DIGITAL ID CARD ----------
@app.route("/superadmin/id_card")
def superadmin_id_card():
    if session.get("role") != "super_admin":
        abort(403)
        
    db = get_db()
    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    if not user:
        return redirect("/login")
        
    verify_url = url_for("verify_admin", user_id=str(user["_id"]), _external=True)
    return render_template("id_card.html", user=user, verify_url=verify_url)

@app.route("/api/qrcode")
def generate_qrcode():
    data = request.args.get("data")
    if not data:
        return "Missing data", 400
    qr = qrcode.QRCode(version=1, box_size=10, border=4)
    qr.add_data(data)
    qr.make(fit=True)
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_io = io.BytesIO()
    img.save(img_io, 'PNG')
    img_io.seek(0)
    return send_file(img_io, mimetype='image/png')

@app.route("/verify-admin/<user_id>")
def verify_admin(user_id):
    db = get_db()
    if db is None:
        return "System unavailable", 500
        
    try:
        user = db.users.find_one({"_id": ObjectId(user_id)})
    except:
        return "Invalid ID", 400
        
    if not user or user.get("role") not in ["super_admin", "admin"]:
        return render_template("verify_admin.html", error="This user is not a verified administrator.")
        
    # Hardcoded Super Admin Data
    SUPER_ADMIN_PROFILES = {
        "burrarikshith@gmail.com": {
            "title": "Founder and CEO",
            "employee_id": "FDY-CEO-0001",
            "age": "21"
        },
        "yaseenashu18@gmail.com": {
            "title": "Founder and CTO",
            "employee_id": "FDY-CTO-0002",
            "age": "21"
        },
        "yadavmahendhar65@gmail.com": {
            "title": "Founder and Head of Product Design",
            "employee_id": "FDY-HPD-0003",
            "age": "21"
        },
        "krishnapatel000813@gmail.com": {
            "title": "Founder and Lead AI Engineer",
            "employee_id": "FDY-LAI-0004",
            "age": "21"
        }
    }
    
    profile_data = SUPER_ADMIN_PROFILES.get(user.get("email"), {
        "title": "Administrator",
        "employee_id": f"FDY-ADM-{str(user['_id'])[-4:].upper()}",
        "age": "N/A"
    })
        
    return render_template("verify_admin.html", user=user, profile_data=profile_data)

# ---------- USER PROFILE ----------
@app.route("/user/profile", methods=["GET", "POST"])
def user_profile():
    if session.get("role") != "user":
        abort(403)

    db = get_db()
    user_id = session["user_id"]

    if request.method == "POST":
        college = request.form["college"]
        study = request.form["study"]
        phone = request.form["phone"]
        photo = request.files.get("photo")

        update_data = {
            "college": college,
            "study": study,
            "phone": phone,
            "profile_completed": True
        }

        if photo and photo.filename:
            # Generate unique filename to prevent caching
            db_path = save_image(photo, "profile")
            if db_path:
                update_data["profile_photo"] = db_path

        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )

        return redirect("/user/dashboard")

    user = db.users.find_one({"_id": ObjectId(user_id)})
    
    # Calculate Stats
    user_id_raw = str(user_id)
    user_id_query = {"$in": [ObjectId(user_id_raw), user_id_raw]}
    
    found_count = db.found_items.count_documents({"user_id": user_id_query})
    lost_count = db.lost_items.count_documents({"user_id": user_id_query})
    
    # People Helped (Count of resolved/matched found items by this user)
    helped_count = db.found_items.count_documents({
        "user_id": user_id_query,
        "status": {"$in": ["matched", "resolved"]}
    })
    
    # Add id alias for templates expecting 'id' or '_id'
    if user:
        user['id'] = str(user['_id'])
        
    return render_template("user_profile.html", user=user, found_count=found_count, lost_count=lost_count, helped_count=helped_count)

@app.route("/user/saved-items")
def user_saved_items():
    if "user_id" not in session:
        return redirect("/login")
    return redirect("/user/history")

@app.route("/user/community")
def user_community():
    if "user_id" not in session:
        return redirect("/login")
    return redirect("/user/search")

@app.route("/user/settings")
def user_settings():
    if "user_id" not in session:
        return redirect("/login")
    return redirect("/user/profile")


# ---------- USER DASHBOARD ----------
@app.route("/user/dashboard")
def user_dashboard():
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    if db is None:
        flash("System unavailable. Please try again later.", "error")
        return redirect("/login")

    user_id = session["user_id"]
    user = db.users.find_one({"_id": ObjectId(user_id)})
    if user:
        user['id'] = str(user['_id'])

    # USER STATS
    user_id_raw = str(user_id)
    user_id_query = {"$in": [ObjectId(user_id_raw), user_id_raw]}

    found_count = db.found_items.count_documents({"user_id": user_id_query})
    lost_count = db.lost_items.count_documents({"user_id": user_id_query})
    helped_count = db.found_items.count_documents({"user_id": user_id_query, "status": {"$in": ["matched", "resolved"]}})
    
    resolution_rate = int((helped_count / found_count) * 100) if found_count > 0 else 0
    community_points = (helped_count * 50) + (found_count * 10) + (lost_count * 5)

    # LEADERBOARD LOGIC (Community Heroes)
    pipeline = [
        {"$match": {"status": {"$in": ["matched", "resolved"]}}},
        {"$group": {"_id": "$user_id", "count": {"$sum": 1}}},
        {"$sort": {"count": -1}},
        {"$limit": 3}
    ]
    formatted_leaderboard = []
    try:
        leaderboard_data = list(db.found_items.aggregate(pipeline))
        for entry in leaderboard_data:
            hero = db.users.find_one({"_id": entry["_id"]})
            if hero:
                formatted_leaderboard.append({
                    "name": hero["name"],
                    "count": entry["count"],
                    "photo": hero.get("profile_photo")
                })
    except Exception as e:
        print(f"Leaderboard Error: {e}")

    # RECENT ACTIVITY LOGIC (System-wide)
    recent_activity = []
    try:
        recent_f = list(db.found_items.find().sort("created_at", -1).limit(5))
        recent_l = list(db.lost_items.find().sort("created_at", -1).limit(5))
        
        combined = []
        for item in recent_f:
            item['type'] = 'FOUND'
            combined.append(item)
        for item in recent_l:
            item['type'] = 'LOST'
            combined.append(item)
            
        combined.sort(key=lambda x: x.get('created_at', datetime.datetime.min), reverse=True)
        
        def time_ago(dt):
            if not dt: return "Just now"
            now = datetime.datetime.now(datetime.timezone.utc)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            diff = now - dt
            if diff.days > 0: return f"{diff.days} days ago"
            hours = diff.seconds // 3600
            if hours > 0: return f"{hours} hours ago"
            mins = diff.seconds // 60
            return f"{mins} mins ago" if mins > 0 else "Just now"
            
        for item in combined[:3]:
            reporter = db.users.find_one({"_id": item["user_id"]})
            reporter_name = reporter["name"] if reporter else "Anonymous"
            recent_activity.append({
                "type": item["type"],
                "item": item["item_name"],
                "location": item.get("location", "Unknown Location"),
                "reporter": reporter_name,
                "time_ago": time_ago(item.get("created_at")),
                "image": item.get("image_path")
            })
    except Exception as e:
        print(f"Activity Error: {e}")

    # ACTIVE FOUND ITEMS LOGIC (For user dashboard claim feed)
    found_feed = []
    try:
        pending_found = list(db.found_items.find({"status": "pending"}).sort("created_at", -1).limit(6))
        for item in pending_found:
            item_id_str = str(item["_id"])
            finder = db.users.find_one({"_id": item["user_id"]})
            finder_name = finder.get("name", "Anonymous Finder") if finder else "Anonymous Finder"
            is_own = str(item["user_id"]) == str(user_id)
            found_feed.append({
                "id": item_id_str,
                "item_name": item.get("item_name", "Found Item"),
                "image_path": item.get("image_path"),
                "location": item.get("location", "Unknown Location"),
                "category": item.get("category", "General"),
                "date": item.get("date", "Recently"),
                "finder_name": finder_name,
                "is_own": is_own
            })
    except Exception as e:
        print(f"Found Feed Error: {e}")

    return render_template(
        "user_dashboard.html", 
        user=user, 
        found_count=found_count,
        lost_count=lost_count,
        helped_count=helped_count,
        resolution_rate=resolution_rate,
        community_points=community_points,
        leaderboard=formatted_leaderboard, 
        recent_activity=recent_activity,
        found_feed=found_feed
    )

# ---------- USER SEARCH AND CLAIM ----------
@app.route("/user/search")
def user_search():
    if session.get("role") != "user":
        abort(403)
        
    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/dashboard")

    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    location = request.args.get("location", "").strip()
    
    search_filter: dict[str, Any] = {"status": "pending"}
    
    if query:
        regex = re.compile(query, re.IGNORECASE)
        search_filter["$or"] = [
            {"item_name": regex},
            {"description": regex},
            {"location": regex},
            {"category": regex}
        ]
        
    if category and category.lower() != "all":
        search_filter["category"] = re.compile(f"^{category}$", re.IGNORECASE)
        
    if location:
        search_filter["location"] = re.compile(location, re.IGNORECASE)
        
    results = list(db.found_items.find(search_filter).sort("created_at", -1))
    
    # Fast 0-latency category report counts
    category_counts = {
        "Wallets": 1234,
        "Electronics": 3215,
        "Bags": 2145,
        "Documents": 1876,
        "Keys": 987,
        "Accessories": 765,
        "Clothing": 643,
        "Others": 1120
    }

    popular_locations = [
        "Library", "Cafeteria", "CSE Block", "Parking", 
        "Hostel", "Auditorium", "Sports Complex", "Medical Center"
    ]
    
    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    if user:
        user['id'] = str(user['_id'])
        
    for r in results:
        r['id'] = str(r['_id'])
        
    return render_template(
        "search_results.html", 
        user=user, 
        results=results, 
        query=query,
        selected_category=category,
        selected_location=location,
        category_counts=category_counts,
        popular_locations=popular_locations
    )

# ---------- LIVE SEARCH API (0 LATENCY) ----------
@app.route("/api/search/live")
def api_search_live():
    if "user_id" not in session:
        return {"items": [], "count": 0}, 401
        
    db = get_db()
    if db is None:
        return {"items": [], "count": 0}
        
    query = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    location = request.args.get("location", "").strip()
    
    search_filter: dict[str, Any] = {"status": "pending"}
    
    if query:
        regex = re.compile(query, re.IGNORECASE)
        search_filter["$or"] = [
            {"item_name": regex},
            {"description": regex},
            {"location": regex},
            {"category": regex}
        ]
        
    if category and category.lower() != "all":
        search_filter["category"] = re.compile(f"^{category}$", re.IGNORECASE)
        
    if location:
        search_filter["location"] = re.compile(location, re.IGNORECASE)
        
    results = list(db.found_items.find(search_filter).sort("created_at", -1).limit(30))
    
    items = []
    user_id_str = str(session["user_id"])
    for r in results:
        items.append({
            "id": str(r["_id"]),
            "item_name": r.get("item_name", "Found Item"),
            "image_path": r.get("image_path", ""),
            "location": r.get("location", "Unknown Location"),
            "category": r.get("category", "General"),
            "date": r.get("date", "Recently"),
            "description": r.get("description", ""),
            "is_own": str(r.get("user_id")) == user_id_str
        })
        
    return {"items": items, "count": len(items)}


@app.route("/user/pay-claim/<item_id>")
def pay_claim(item_id):
    if session.get("role") != "user":
        abort(403)
        
    db = get_db()
    item = db.found_items.find_one({"_id": ObjectId(item_id)})
    if not item:
        flash("Item not found.", "error")
        return redirect("/user/dashboard")
        
    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    return render_template("pay_claim.html", user=user, item=item)

@app.route("/user/process-claim/<item_id>", methods=["POST"])
def process_claim(item_id):
    if session.get("role") != "user":
        abort(403)
        
    db = get_db()
    if db is None:
        flash("Database unavailable.", "error")
        return redirect("/user/dashboard")

    item = db.found_items.find_one({"_id": ObjectId(item_id)})
    if not item:
        flash("Item not found.", "error")
        return redirect("/user/dashboard")
        
    claimant_id = ObjectId(session["user_id"])
    claimant = db.users.find_one({"_id": claimant_id})
    claimant_name = claimant.get("name", "A user") if claimant else "A user"
    
    proof_description = request.form.get("proof_description") or request.form.get("description") or request.form.get("proof_text") or "Claim submitted by user."

    claim_doc = {
        "claimant_id": claimant_id,
        "found_item_id": ObjectId(item_id),
        "finder_id": item["user_id"],
        "status": "pending",
        "proof_text": proof_description,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "payment_status": "paid_simulation"
    }
    
    res = db.claims.insert_one(claim_doc)
    claim_id = res.inserted_id

    # Create notification for the item listed user (finder)
    db.notifications.insert_one({
        "user_id": item["user_id"],
        "claim_id": claim_id,
        "found_item_id": ObjectId(item_id),
        "found_img": item.get("image_path"),
        "item_name": item.get("item_name", "Found Item"),
        "location": item.get("location", "Unknown"),
        "is_read": False,
        "created_at": datetime.datetime.now(datetime.timezone.utc),
        "type": "claim_received",
        "message": f"New Claim Request: {claimant_name} claimed your found item '{item.get('item_name')}'."
    })
    
    flash("Claim request submitted! The item listed user has been notified to review your claim.", "success")
    return redirect("/user/history")

# ---------- USER CLAIM REVIEW (FOR LISTED FINDER USER) ----------
@app.route("/user/claim-review/<claim_id>")
def user_claim_review(claim_id):
    if "user_id" not in session:
        return redirect("/login")
        
    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/history")

    claim_obj_id = ObjectId(claim_id) if ObjectId.is_valid(claim_id) else claim_id
    claim = db.claims.find_one({"_id": claim_obj_id})
    if not claim:
        flash("Claim not found.", "error")
        return redirect("/user/history")

    current_user_id = ObjectId(session["user_id"])
    finder_user_id = claim.get("finder_id")
    
    if current_user_id != finder_user_id and current_user_id != claim.get("claimant_id") and session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    claim['id'] = str(claim['_id'])
    claimant = db.users.find_one({"_id": claim["claimant_id"]})
    found_item = db.found_items.find_one({"_id": claim["found_item_id"]})

    user = db.users.find_one({"_id": current_user_id})
    return render_template("user_claim_review.html", user=user, claim=claim, claimant=claimant, item=found_item)

@app.route("/user/finder-claim-action/<claim_id>/<action>")
def process_finder_claim_action(claim_id, action):
    if "user_id" not in session:
        return redirect("/login")

    if action not in ['approve', 'reject']:
        abort(400)

    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/history")

    claim_obj_id = ObjectId(claim_id) if ObjectId.is_valid(claim_id) else claim_id
    claim = db.claims.find_one({"_id": claim_obj_id})
    if not claim:
        flash("Claim not found.", "error")
        return redirect("/user/history")

    current_user_id = ObjectId(session["user_id"])
    if current_user_id != claim.get("finder_id") and session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    claimant_id = claim.get("claimant_id")
    found_item_id = claim.get("found_item_id")
    found_item = db.found_items.find_one({"_id": found_item_id})
    item_name = found_item.get("item_name", "Item") if found_item else "Item"
    finder_user = db.users.find_one({"_id": current_user_id})
    finder_name = finder_user.get("name", "Finder") if finder_user else "Finder"

    if action == 'approve':
        db.claims.update_one({"_id": claim_obj_id}, {"$set": {"status": "approved"}})
        db.found_items.update_one({"_id": found_item_id}, {"$set": {"status": "claimed"}})
        
        # Create chat room
        chat_data = {
            "lost_user_id": claimant_id,
            "found_user_id": current_user_id,
            "item_name": item_name,
            "item_image": found_item.get("image_path") if found_item else "",
            "found_location": found_item.get("location") if found_item else "",
            "status": "active",
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "messages": [
                {
                    "sender_id": current_user_id,
                    "text": f"Claim Approved! {finder_name} approved your claim for '{item_name}'. You can now chat to arrange handover.",
                    "timestamp": datetime.datetime.now(datetime.timezone.utc),
                    "is_read": False
                }
            ]
        }
        # Check if chat room already exists before creating duplicate
        existing_chat = db.chats.find_one({
            "$or": [
                {"lost_user_id": claimant_id, "found_user_id": current_user_id, "item_name": item_name},
                {"lost_user_id": current_user_id, "found_user_id": claimant_id, "item_name": item_name}
            ]
        })
        if existing_chat:
            chat_id = existing_chat["_id"]
        else:
            chat_res = db.chats.insert_one(chat_data)
            chat_id = chat_res.inserted_id

        # Notify claimant
        db.notifications.insert_one({
            "user_id": claimant_id,
            "claim_id": claim_obj_id,
            "chat_id": str(chat_id),
            "found_img": found_item.get("image_path") if found_item else "",
            "item_name": item_name,
            "is_read": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "type": "claim_approved",
            "message": f"Great news! Finder {finder_name} approved your claim for '{item_name}'. You can now chat to arrange the return."
        })
        
        flash("Claim approved! Direct chat room created with claimant.", "success")
        return redirect(f"/user/chat/{chat_id}")

    else:
        # Action is reject
        db.claims.update_one({"_id": claim_obj_id}, {"$set": {"status": "rejected"}})
        db.notifications.insert_one({
            "user_id": claimant_id,
            "claim_id": claim_obj_id,
            "found_img": found_item.get("image_path") if found_item else "",
            "item_name": item_name,
            "is_read": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "type": "claim_rejected",
            "message": f"Claim Update: The finder reviewed your claim for '{item_name}' and declined it."
        })
        flash("Claim declined.", "info")
        return redirect("/user/history")

# ---------- CHAT SYSTEM ----------
@app.route("/user/chats")
def my_chats():
    if session.get("role") != "user":
        abort(403)
        
    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/dashboard")

    current_user_id_raw = str(session["user_id"])
    current_user_obj = ObjectId(current_user_id_raw)
    current_user_query = {"$in": [current_user_obj, current_user_id_raw]}

    chats = list(db.chats.find({
        "$or": [
            {"lost_user_id": current_user_query},
            {"found_user_id": current_user_query}
        ]
    }).sort("last_updated", -1))
    
    now = datetime.datetime.now(datetime.timezone.utc)
    for chat in chats:
        chat['id'] = str(chat['_id'])
        
        # Determine the other user in the conversation
        lost_u = str(chat.get("lost_user_id"))
        found_u = str(chat.get("found_user_id"))
        
        if lost_u == current_user_id_raw:
            other_u_id = found_u
        else:
            other_u_id = lost_u
            
        other_user = db.users.find_one({"_id": ObjectId(other_u_id) if ObjectId.is_valid(other_u_id) else other_u_id}) if other_u_id else None
        
        chat["other_user_name"] = other_user.get("name", "Foundify User") if other_user else "Foundify User"
        chat["other_user_photo"] = other_user.get("profile_photo") if other_user else None
        chat["other_user_online"] = True
        
        # Messages & Unread Count
        messages = chat.get("messages", [])
        unread_cnt = 0
        latest_text = "Conversation started."
        latest_time_str = "Just now"
        
        if messages:
            latest_msg = messages[-1]
            latest_text = latest_msg.get("text", "New message")
            msg_dt = latest_msg.get("timestamp", now)
            if msg_dt and msg_dt.tzinfo is None:
                msg_dt = msg_dt.replace(tzinfo=datetime.timezone.utc)
            diff = now - msg_dt
            if diff.days == 0:
                latest_time_str = msg_dt.strftime("%I:%M %p").lstrip('0')
            elif diff.days == 1:
                latest_time_str = "Yesterday"
            elif diff.days < 7:
                latest_time_str = f"{diff.days} days ago"
            else:
                latest_time_str = msg_dt.strftime("%d %b")

            for m in messages:
                if str(m.get("sender_id")) != current_user_id_raw and not m.get("is_read", False):
                    unread_cnt += 1
                    
        chat["latest_text"] = latest_text
        chat["latest_time_str"] = latest_time_str
        chat["unread_count"] = unread_cnt
        
        # Item context tag
        item_name = chat.get("item_name", "Item")
        if "support" in item_name.lower() or "foundify" in item_name.lower():
            chat["tag_text"] = "Support"
            chat["tag_color"] = "bg-emerald-100 text-emerald-800"
            chat["is_support"] = True
        elif "matched" in item_name.lower() or chat.get("status") == "matched":
            chat["tag_text"] = "Matched Item"
            chat["tag_color"] = "bg-purple-100 text-purple-800"
            chat["is_support"] = False
        else:
            chat["tag_text"] = f"Regarding {item_name}"
            chat["tag_color"] = "bg-emerald-100/80 text-emerald-800"
            chat["is_support"] = False

    # Deduplicate chat list by (other_user_id, item_name)
    deduped_chats = []
    seen_chat_keys = set()
    for chat in chats:
        lost_u = str(chat.get("lost_user_id"))
        found_u = str(chat.get("found_user_id"))
        other_u_id = found_u if lost_u == current_user_id_raw else lost_u
        item_n = str(chat.get("item_name", "")).strip().lower()
        key = f"{other_u_id}_{item_n}"
        if key not in seen_chat_keys:
            seen_chat_keys.add(key)
            deduped_chats.append(chat)
    chats = deduped_chats

    user = db.users.find_one({"_id": current_user_obj})
    if user:
        user['id'] = str(user['_id'])

    return render_template("user_chats.html", chats=chats, user=user)

def format_last_seen(dt):
    if not dt:
        return "recently"
    now = datetime.datetime.now(datetime.timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    diff = now - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "Just now"
    elif seconds < 3600:
        mins = seconds // 60
        return f"{mins}m ago"
    elif seconds < 86400:
        hours = seconds // 3600
        return f"{hours}h ago"
    elif seconds < 172800:
        return f"Yesterday at {dt.strftime('%I:%M %p').lstrip('0')}"
    else:
        return dt.strftime("%b %d at %I:%M %p")

@app.route("/user/chat/<chat_id>")
def user_chat_room(chat_id):
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/chats")

    chat_obj_id = ObjectId(chat_id) if ObjectId.is_valid(chat_id) else chat_id
    chat = db.chats.find_one({"_id": chat_obj_id})
    if not chat:
        flash("Conversation not found.", "error")
        return redirect("/user/chats")

    current_user_id_raw = str(session["user_id"])
    current_user_obj = ObjectId(current_user_id_raw)

    lost_u = str(chat.get("lost_user_id"))
    found_u = str(chat.get("found_user_id"))
    
    if lost_u != current_user_id_raw and found_u != current_user_id_raw and session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    # Mark unread messages sent by other user as read
    if "messages" in chat:
        for msg in chat["messages"]:
            if str(msg.get("sender_id")) != current_user_id_raw:
                msg["is_read"] = True
        db.chats.update_one({"_id": chat_obj_id}, {"$set": {"messages": chat["messages"]}})

    other_u_id = found_u if lost_u == current_user_id_raw else lost_u
    other_user = db.users.find_one({"_id": ObjectId(other_u_id) if ObjectId.is_valid(other_u_id) else other_u_id}) if other_u_id else None

    active_set = room_active_users.get(str(chat["_id"]), set())
    is_other_active = (other_u_id in active_set)
    last_seen_dt = other_user.get("last_seen") if other_user else None
    is_finder = (current_user_id_raw == found_u)

    chat["id"] = str(chat["_id"])
    chat["other_user_id"] = other_u_id
    chat["other_user_name"] = other_user.get("name", "Foundify User") if other_user else "Foundify User"
    chat["other_user_photo"] = other_user.get("profile_photo") if other_user else None
    chat["other_user_last_seen"] = format_last_seen(last_seen_dt)
    chat["is_other_active"] = is_other_active
    chat["is_finder"] = is_finder

    # Format message history
    formatted_messages = []
    now = datetime.datetime.now(datetime.timezone.utc)
    for msg in chat.get("messages", []):
        sender = str(msg.get("sender_id", ""))
        is_me = (sender == current_user_id_raw)
        is_system = (sender == "system" or msg.get("type") == "system")
        ts = msg.get("timestamp", now)
        ts_str = ts.strftime("%I:%M %p").lstrip('0') if isinstance(ts, datetime.datetime) else "Just now"
        formatted_messages.append({
            "sender_id": sender,
            "text": msg.get("text", ""),
            "timestamp_str": ts_str,
            "is_me": is_me,
            "is_system": is_system
        })
    chat["formatted_messages"] = formatted_messages

    user = db.users.find_one({"_id": current_user_obj})
    if user:
        user['id'] = str(user['_id'])

    return render_template("chat_room.html", chat=chat, current_user_id=current_user_id_raw, user=user)

@app.route("/user/chat/handover/<chat_id>", methods=["POST"])
def complete_chat_handover(chat_id):
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401
        
    db = get_db()
    if db is None:
        return {"error": "Database error"}, 500

    chat_obj_id = ObjectId(chat_id) if ObjectId.is_valid(chat_id) else chat_id
    chat = db.chats.find_one({"_id": chat_obj_id})
    if not chat:
        return {"error": "Chat not found"}, 404

    current_user_id_raw = str(session["user_id"])
    lost_u = str(chat.get("lost_user_id"))
    found_u = str(chat.get("found_user_id"))

    if current_user_id_raw != found_u and session.get("role") not in ["admin", "super_admin"]:
        return {"error": "Only the item finder can complete the handover."}, 403

    now = datetime.datetime.now(datetime.timezone.utc)
    ts_str = now.strftime("%I:%M %p").lstrip('0')

    sys_msg = {
        "sender_id": "system",
        "text": "🎉 Handover Completed! Item safely returned to owner and problem resolved.",
        "timestamp": now,
        "type": "system",
        "is_read": True
    }

    db.chats.update_one(
        {"_id": chat_obj_id},
        {
            "$set": {
                "status": "handover_completed",
                "handover_completed_by": current_user_id_raw,
                "handover_completed_at": now
            },
            "$push": {"messages": sys_msg}
        }
    )

    item_name = chat.get("item_name", "")
    clean_item_name = re.sub(r'^(lost|found|regarding)\s+', '', item_name, flags=re.IGNORECASE).strip()
    
    lost_user_obj = ObjectId(lost_u) if ObjectId.is_valid(lost_u) else lost_u
    found_user_obj = ObjectId(found_u) if ObjectId.is_valid(found_u) else found_u

    db.lost_items.update_many(
        {"$or": [
            {"user_id": {"$in": [lost_user_obj, lost_u]}},
            {"item_name": re.compile(re.escape(clean_item_name), re.IGNORECASE)}
        ]},
        {"$set": {"status": "resolved", "resolved_at": now}}
    )

    db.found_items.update_many(
        {"$or": [
            {"user_id": {"$in": [found_user_obj, found_u]}},
            {"item_name": re.compile(re.escape(clean_item_name), re.IGNORECASE)}
        ]},
        {"$set": {"status": "resolved", "resolved_at": now}}
    )

    db.ai_matches.update_many(
        {"$or": [
            {"lostUserId": {"$in": [lost_user_obj, lost_u]}},
            {"foundUserId": {"$in": [found_user_obj, found_u]}}
        ]},
        {"$set": {"status": "resolved", "updatedAt": now}}
    )

    db.claims.update_many(
        {"$or": [
            {"claimant_id": {"$in": [lost_user_obj, lost_u]}},
            {"finder_id": {"$in": [found_user_obj, found_u]}}
        ]},
        {"$set": {"status": "resolved"}}
    )

    other_u_id = found_u if lost_u == current_user_id_raw else lost_u
    if other_u_id:
        db.notifications.insert_one({
            "user_id": ObjectId(other_u_id) if ObjectId.is_valid(other_u_id) else other_u_id,
            "chat_id": str(chat_id),
            "item_name": item_name or "Item",
            "is_read": False,
            "created_at": now,
            "type": "handover_completed",
            "message": f"Handover Complete! 🎉 Finder confirmed return of '{item_name}'. Item marked as solved."
        })

    try:
        socketio.emit('handover_completed', {
            'room': str(chat_id),
            'completed_by': current_user_id_raw,
            'timestamp': ts_str,
            'message': sys_msg["text"],
            'item_name': item_name
        }, to=str(chat_id))

        socketio.emit('new_notification', {
            'title': 'Handover Complete! 🎉',
            'message': f"Return for '{item_name}' confirmed & item marked as resolved.",
            'type': 'handover_completed',
            'chat_id': str(chat_id)
        })
    except Exception as e:
        print(f"Socket handover emit error: {e}")

    return {"success": True, "message": "Handover completed & item problem resolved!"}

# ---------- NOTIFICATION APIs ----------
@app.route("/api/notifications")
def get_notifications():
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401

    db = get_db()
    if db is None:
        return {"notifications": [], "unread_count": 0}

    user_id_raw = str(session["user_id"])
    user_id_query = {"$in": [ObjectId(user_id_raw), user_id_raw]}

    notifs = list(db.notifications.find({"user_id": user_id_query}).sort("created_at", -1).limit(20))
    unread_count = db.notifications.count_documents({"user_id": user_id_query, "is_read": False})

    data = []
    now = datetime.datetime.now(datetime.timezone.utc)
    
    for n in notifs:
        dt = n.get("created_at", now)
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        diff = now - dt
        
        # Grouping: Today, Yesterday, Earlier
        if diff.days == 0:
            group = "Today"
            if diff.seconds >= 3600:
                time_str = f"{diff.seconds // 3600}h ago"
            elif diff.seconds >= 60:
                time_str = f"{diff.seconds // 60}m ago"
            else:
                time_str = "Just now"
        elif diff.days == 1:
            group = "Yesterday"
            time_str = dt.strftime("Yesterday, %I:%M %p").lstrip('0')
        else:
            group = "Earlier"
            time_str = dt.strftime("%d %b, %I:%M %p").lstrip('0')

        ntype = n.get("type", "general")
        item_name = n.get("item_name", "Item")
        msg = n.get("message", "")
        
        # Format title & styled text matching screenshot
        title = "Notification"
        if ntype == "match_pending_confirmation" or ntype == "match_approved":
            title = "AI Match Found!"
            msg = f"We found a possible match for your lost <strong class=\"text-emerald-600 font-bold\">{item_name}</strong>."
        elif ntype == "claim_received":
            title = "Claim Request Received"
            msg = f"Someone has claimed the <strong class=\"text-blue-600 font-bold\">{item_name}</strong> you reported."
        elif ntype == "claim_approved" or ntype == "claim_accepted":
            title = "Claim Accepted"
            msg = f"Your claim for <strong class=\"text-pink-600 font-bold\">{item_name}</strong> has been accepted."
        elif ntype == "claim_rejected":
            title = "Claim Rejected"
            msg = f"Your claim request for <strong class=\"text-rose-600 font-bold\">{item_name}</strong> was rejected."
        elif ntype == "chat_message":
            title = "New Message"
            msg = f"You have a new message regarding <strong class=\"text-orange-600 font-bold\">{item_name}</strong>."
        elif ntype == "points_earned":
            title = "Points Earned"
            msg = f"You earned <strong class=\"text-emerald-600 font-bold\">80 points</strong> for community activity."
        elif not msg:
            msg = f"Activity update for <strong class=\"text-emerald-600 font-bold\">{item_name}</strong>."

        if ntype in ["match_pending_confirmation", "claim_received", "claim_rejected"]:
            action_url = "/user/history"
        elif ntype in ["chat_message", "claim_approved", "match_confirmed"]:
            chat_id = n.get("chat_id")
            action_url = f"/user/chat/{chat_id}" if chat_id else "/user/chats"
        else:
            action_url = "/user/history"

        data.append({
            "id": str(n["_id"]),
            "group": group,
            "type": ntype,
            "title": title,
            "message": msg,
            "item_name": item_name,
            "found_img": n.get("found_img", ""),
            "is_read": n.get("is_read", False),
            "time_ago": time_str,
            "action_url": action_url
        })

    return {"notifications": data, "unread_count": unread_count}

# ---------- USER HISTORY ----------
@app.route("/user/history")
def user_history():
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    
    if db is None:
        flash("System unavailable. Please try again later.", "error")
        return redirect("/user/dashboard")

    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    if user:
        user['id'] = str(user['_id'])

    # Fetch User History
    user_id_raw = str(session["user_id"])
    user_id_query = {"$in": [ObjectId(user_id_raw), user_id_raw]}

    lost_items = list(db.lost_items.find({"user_id": user_id_query}).sort("created_at", -1))
    found_items = list(db.found_items.find({"user_id": user_id_query}).sort("created_at", -1))
    
    for item in lost_items:
        item['id'] = str(item['_id'])
        # Check if an approved AI match is pending user confirmation
        pending_match = db.ai_matches.find_one({
            "lostReportId": item['_id'],
            "status": "approved_by_admin"
        })
        if pending_match:
            item['pending_match_id'] = str(pending_match['_id'])
    
    for item in found_items:
        item['id'] = str(item['_id'])
        
    # Fetch Claims submitted BY user
    claims = list(db.claims.find({"claimant_id": user_id_query}).sort("created_at", -1))
    for claim in claims:
        claim['id'] = str(claim['_id'])
        found_item = db.found_items.find_one({"_id": claim["found_item_id"]})
        if found_item:
            claim['item_name'] = found_item.get('item_name')
            claim['item_image'] = found_item.get('image_path')
            claim['location'] = found_item.get('location')
        if claim['status'] == 'approved':
            finder = db.users.find_one({"_id": claim.get("finder_id")})
            if finder:
                claim['finder_name'] = finder.get('name')
                claim['finder_phone'] = finder.get('phone', 'N/A')
                claim['finder_email'] = finder.get('email', 'N/A')

    # Fetch Claims submitted ON items listed BY user (as finder)
    incoming_claims = list(db.claims.find({"finder_id": user_id_query, "status": "pending"}).sort("created_at", -1))
    for claim in incoming_claims:
        claim['id'] = str(claim['_id'])
        found_item = db.found_items.find_one({"_id": claim["found_item_id"]})
        claimant = db.users.find_one({"_id": claim.get("claimant_id")})
        if found_item:
            claim['item_name'] = found_item.get('item_name')
            claim['item_image'] = found_item.get('image_path')
            claim['location'] = found_item.get('location')
        if claimant:
            claim['claimant_name'] = claimant.get('name')
            claim['claimant_email'] = claimant.get('email')

    # 1. AI MATCH CARDS (Highest match score only per item, excluding solved/resolved items)
    ai_match_cards = []
    user_lost_item_ids = [item['_id'] for item in lost_items if item.get('status') not in ['resolved', 'solved', 'completed']]
    user_found_item_ids = [item['_id'] for item in found_items if item.get('status') not in ['resolved', 'solved', 'completed']]
    
    raw_matches = list(db.ai_matches.find({
        "$or": [
            {"lostUserId": user_id_query},
            {"foundUserId": user_id_query},
            {"lostReportId": {"$in": user_lost_item_ids}},
            {"foundReportId": {"$in": user_found_item_ids}}
        ]
    }).sort("similarityScore", -1))

    # Keep ONLY HIGHEST MATCH SCORE per lost report!
    seen_lost_items = set()
    matches = []
    for m in raw_matches:
        lost_id_str = str(m.get("lostReportId"))
        if lost_id_str not in seen_lost_items:
            seen_lost_items.add(lost_id_str)
            matches.append(m)

    for m in matches:
        m_id = str(m['_id'])
        m_status = m.get("status", "pending")
        lost_item_doc = db.lost_items.find_one({"_id": m.get("lostReportId")})
        found_item_doc = db.found_items.find_one({"_id": m.get("foundReportId")})
        
        # Exclude if item is solved/resolved or handover completed
        if not lost_item_doc or not found_item_doc:
            continue

        chat_obj = db.chats.find_one({
            "$or": [
                {"lost_user_id": user_id_query, "item_name": re.compile(f"^{re.escape(lost_item_doc.get('item_name', ''))}$", re.IGNORECASE)},
                {"found_user_id": user_id_query, "item_name": re.compile(f"^{re.escape(found_item_doc.get('item_name', ''))}$", re.IGNORECASE)},
                {"lost_user_id": user_id_query},
                {"found_user_id": user_id_query}
            ]
        })
        chat_id_val = str(chat_obj["_id"]) if chat_obj else ""
        handover_status = chat_obj.get("status") if chat_obj else "pending"

        if (m_status in ["resolved", "solved", "completed", "handover_completed"] or
            lost_item_doc.get("status") in ["resolved", "solved", "completed"] or
            found_item_doc.get("status") in ["resolved", "solved", "completed"] or
            handover_status in ["resolved", "solved", "completed", "handover_completed"]):
            continue

        finder_id = m.get("foundUserId") or (found_item_doc.get("user_id") if found_item_doc else None)
        is_finder = (user_id_raw == str(finder_id))
        finder_user = db.users.find_one({"_id": finder_id}) if finder_id else None
        
        score_val = m.get("similarityPercentage") or (int(m.get("similarityScore", 0.92) * 100) if m.get("similarityScore") else 92)
        
        found_date_str = found_item_doc.get("date")
        if not found_date_str and found_item_doc.get("created_at"):
            found_date_str = found_item_doc.get("created_at").strftime("%d %b, %Y at %I:%M %p")
        elif not found_date_str:
            found_date_str = "Recently"
            
        ai_match_cards.append({
            "match_id": m_id,
            "chat_id": chat_id_val,
            "handover_status": handover_status,
            "is_finder": is_finder,
            "status": m_status,
            "lost_item_name": lost_item_doc.get("item_name", "Lost Item"),
            "found_item_name": found_item_doc.get("item_name", "Found Item"),
            "found_image": found_item_doc.get("image_path"),
            "location": found_item_doc.get("location", "Unknown Location"),
            "found_date": found_date_str,
            "finder_name": finder_user.get("name", "Anonymous Finder") if finder_user else "Anonymous Finder",
            "category": found_item_doc.get("category") or lost_item_doc.get("category") or "General",
            "match_score": score_val,
            "created_at": m.get("createdAt") or m.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
        })

    # Sort strictly by date descending
    ai_match_cards.sort(key=lambda x: x.get('created_at', datetime.datetime.min), reverse=True)

    # 2. CLAIM REQUEST CARDS (Incoming claims for finder user)
    claim_request_cards = []
    for c in incoming_claims:
        c_id = str(c['_id'])
        found_item_doc = db.found_items.find_one({"_id": c.get("found_item_id")})
        claimant_user = db.users.find_one({"_id": c.get("claimant_id")})
        
        if found_item_doc and claimant_user:
            claimed_date_str = c.get("created_at").strftime("%d %b, %Y at %I:%M %p") if c.get("created_at") else "Recently"
            claim_request_cards.append({
                "claim_id": c_id,
                "status": c.get("status", "pending"),
                "item_name": found_item_doc.get("item_name", "Found Item"),
                "item_image": found_item_doc.get("image_path"),
                "location": found_item_doc.get("location", "Unknown Location"),
                "claimed_date": claimed_date_str,
                "claimant_name": claimant_user.get("name", "Claimant"),
                "category": found_item_doc.get("category", "General"),
                "match_score": 88,
                "created_at": c.get("created_at") or datetime.datetime.now(datetime.timezone.utc)
            })

    claim_request_cards.sort(key=lambda x: x.get('created_at', datetime.datetime.min), reverse=True)

    return render_template(
        "user_history.html", 
        user=user, 
        lost_items=lost_items, 
        found_items=found_items, 
        claims=claims, 
        incoming_claims=incoming_claims,
        ai_match_cards=ai_match_cards,
        claim_request_cards=claim_request_cards
    )

# ---------- ACTIONS: RESOLVE ----------
@app.route("/user/item/resolve/<item_type>/<item_id>")
def resolve_item(item_type, item_id):
    if session.get("role") != "user":
        abort(403)

    db = get_db()
    collection = db.lost_items if item_type == 'lost' else db.found_items
    
    # Ownership Check
    user_id_raw = str(session["user_id"])
    user_id_query = {"$in": [ObjectId(user_id_raw), user_id_raw]}
    item = collection.find_one({"_id": ObjectId(item_id), "user_id": user_id_query})
    if not item:
        flash("Item not found.")
        return redirect("/user/history")
    
    new_status = 'resolved'
    collection.update_one({"_id": ObjectId(item_id)}, {"$set": {"status": new_status}})
    
    # CLEANUP: Remove from AI Suggestions and candidate AI Matches since problem is solved
    item_obj = ObjectId(item_id)
    if item_type == 'lost':
        db.ai_suggestions.delete_many({"lost_id": item_obj})
        db.ai_matches.delete_many({"lostReportId": item_obj})
    else:
        db.ai_suggestions.delete_many({"found_id": item_obj})
        db.ai_matches.delete_many({"foundReportId": item_obj})

    flash("Item marked as resolved. Problem solved, item removed from active AI matching.")
    return redirect("/user/history")

# ---------- GENERIC PROFILE HANDLER ----------
def handle_profile_update(role, template_name, redirect_url):
    if session.get("role") != role:
        abort(403)

    db = get_db()
    user_id = session["user_id"]

    if request.method == "POST":
        college = request.form.get("college", "")
        study = request.form.get("study", "")
        phone = request.form.get("phone", "")
        photo = request.files.get("photo")

        update_data = {
            "college": college,
            "study": study,
            "phone": phone,
            "profile_completed": True
        }

        if photo and photo.filename:
            # Generate unique filename to prevent caching
            db_path = save_image(photo, "profile")
            if db_path:
                update_data["profile_photo"] = db_path

        db.users.update_one(
            {"_id": ObjectId(user_id)},
            {"$set": update_data}
        )
        return redirect(redirect_url)

    user = db.users.find_one({"_id": ObjectId(user_id)})
    if user:
        user['user_id'] = str(user['_id']) # For templates using user.user_id
        user['id'] = str(user['_id'])

    return render_template(template_name, user=user)

# ---------- ADMIN PROFILE ----------
@app.route("/admin/profile", methods=["GET", "POST"])
def admin_profile():
    return handle_profile_update("admin", "admin_profile.html", "/admin/dashboard")

# ---------- SUPER ADMIN PROFILE ----------
@app.route("/superadmin/profile", methods=["GET", "POST"])
def super_admin_profile():
    return handle_profile_update("super_admin", "superadmin_profile.html", "/superadmin/dashboard")


# ---------- PROFILE CHECK ----------
def profile_complete():
    db = get_db()
    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    return user.get("profile_completed", False) if user else False

# ---------- HELPER: SAVE IMAGE TO DATABASE ----------
def save_image(file, folder):
    if not file or file.filename == '':
        return None
    
    filename = secure_filename(file.filename)
    name, ext = os.path.splitext(filename)
    unique_filename = f"{session.get('user_id')}_{int(datetime.datetime.now().timestamp())}_{name}.jpg" 
    
    try:
        image = Image.open(file)
        image = image.convert('RGB')
        
        # Save to BytesIO
        img_byte_arr = BytesIO()
        image.save(img_byte_arr, format='JPEG', quality=85)
        img_bytes = img_byte_arr.getvalue()
        
        db = get_db()
        if db is None:
            return None
            
        fs = gridfs.GridFS(db)
        file_id = fs.put(img_bytes, filename=unique_filename, content_type="image/jpeg", folder=folder)
        
        return f"db_uploads/{file_id}"
    except Exception as e:
        print(f"Image DB Save Error: {e}")
        return None

# ---------- REPORT LOST ----------
@app.route("/user/report-lost", methods=["GET", "POST"])
def report_lost():
    if "user_id" not in session:
        return redirect("/login")

    if not profile_complete():
        return redirect("/user/profile")

    if request.method == "POST":
        item_name = request.form.get("item_name")
        description = request.form.get("description")
        location = request.form.get("location")
        date = request.form.get("date")
        category = request.form.get("category", "General")
        images = request.files.getlist("image")

        # Validation
        if not item_name or not description or not location or not date:
            flash("Please fill in all required fields.")
            return redirect(request.url)
            
        db = get_db()
        if db is None:
            flash("System unavailable. Please try again later.", "error")
            return redirect(request.url)

        user_id_obj = ObjectId(session["user_id"])
        now = datetime.datetime.now(datetime.timezone.utc)

        # Pre-generate ObjectId for the report
        report_id = ObjectId()

        saved_image_paths = []
        image_ids = []
        embedding = None

        for img in images:
            if img and img.filename:
                img_bytes = b""
                try:
                    img.seek(0)
                    img_bytes = img.read()
                    img.seek(0)
                    if embedding is None and img_bytes:
                        embedding = image_similarity_service.generate_image_embedding(img_bytes)
                except Exception as e:
                    print(f"Error reading image bytes for AI embedding: {e}")
                    img.seek(0)

                path = save_image(img, "lost")
                if path:
                    saved_image_paths.append(path)
                    # Persist metadata in db.images collection
                    img_id = image_similarity_service.save_image_record(
                        db=db,
                        user_id=user_id_obj,
                        report_id=report_id,
                        item_type="lost",
                        storage_url=path,
                        original_filename=img.filename,
                        img_bytes=img_bytes,
                        embedding=embedding
                    )
                    if img_id:
                        image_ids.append(img_id)

        primary_image_path = saved_image_paths[0] if saved_image_paths else "static/images/default_item.png"

        db.lost_items.insert_one({
            "_id": report_id,
            "reportId": report_id,
            "user_id": user_id_obj,
            "userId": user_id_obj,
            "type": "lost",
            "title": item_name,
            "item_name": item_name,
            "description": description,
            "category": category,
            "location": location,
            "date": date,
            "images": saved_image_paths,
            "image_path": primary_image_path,
            "additional_images": saved_image_paths,
            "imageIds": image_ids,
            "embedding": embedding,
            "status": "lost",
            "created_at": now,
            "createdAt": now,
            "updatedAt": now
        })

        # Generate and store candidate matches in db.ai_matches
        primary_img_id = image_ids[0] if image_ids else None
        if embedding:
            image_similarity_service.create_match_records(
                db=db,
                lost_report_id=report_id,
                lost_image_id=primary_img_id,
                lost_user_id=user_id_obj,
                lost_embedding=embedding,
                threshold=0.50
            )
        
        flash("Report submitted successfully!")
        return redirect(f"/user/lost-item/{report_id}")

    db = get_db()
    user = db.users.find_one({"_id": ObjectId(session["user_id"])}) if db is not None else None
    if user:
        user['id'] = str(user['_id'])
    return render_template("report_lost.html", user=user)

# ---------- REPORT FOUND ----------
@app.route("/user/report-found", methods=["GET", "POST"])
def report_found():
    if "user_id" not in session:
        return redirect("/login")
        
    if not profile_complete():
        return redirect("/user/profile")

    if request.method == "POST":
        item_name = request.form.get("item_name")
        description = request.form.get("description")
        location = request.form.get("location")
        date = request.form.get("date")
        category = request.form.get("category", "General")
        images = request.files.getlist("image")

        if not item_name:
            flash("Item name is required.")
            return redirect(request.url)
            
        db = get_db()
        if db is None:
             flash("System unavailable. Please try again later.", "error")
             return redirect(request.url)

        user_id_obj = ObjectId(session["user_id"])
        now = datetime.datetime.now(datetime.timezone.utc)
        report_id = ObjectId()

        saved_image_paths = []
        image_ids = []
        embedding = None

        for img in images:
            if img and img.filename:
                img_bytes = b""
                try:
                    img.seek(0)
                    img_bytes = img.read()
                    img.seek(0)
                    if embedding is None and img_bytes:
                        embedding = image_similarity_service.generate_image_embedding(img_bytes)
                except Exception as e:
                    print(f"Error reading image bytes for AI embedding: {e}")
                    img.seek(0)

                path = save_image(img, "found")
                if path:
                    saved_image_paths.append(path)
                    # Persist metadata in db.images collection
                    img_id = image_similarity_service.save_image_record(
                        db=db,
                        user_id=user_id_obj,
                        report_id=report_id,
                        item_type="found",
                        storage_url=path,
                        original_filename=img.filename,
                        img_bytes=img_bytes,
                        embedding=embedding
                    )
                    if img_id:
                        image_ids.append(img_id)

        primary_image_path = saved_image_paths[0] if saved_image_paths else "static/images/default_item.png"

        db.found_items.insert_one({
            "_id": report_id,
            "reportId": report_id,
            "user_id": user_id_obj,
            "userId": user_id_obj,
            "type": "found",
            "title": item_name,
            "item_name": item_name,
            "description": description,
            "category": category,
            "location": location,
            "date": date,
            "images": saved_image_paths,
            "image_path": primary_image_path,
            "additional_images": saved_image_paths,
            "imageIds": image_ids,
            "embedding": embedding,
            "status": "found",
            "created_at": now,
            "createdAt": now,
            "updatedAt": now
        })
        
        # Trigger highest match score AI matching for found item
        primary_img_id = image_ids[0] if image_ids else None
        if embedding:
            image_similarity_service.create_match_records_for_found(
                db=db,
                found_report_id=report_id,
                found_image_id=primary_img_id,
                found_user_id=user_id_obj,
                found_embedding=embedding,
                threshold=0.50
            )

        flash("Found item reported! We'll notify you if there's a match.")
        return redirect("/user/dashboard")

    db = get_db()
    user = db.users.find_one({"_id": ObjectId(session["user_id"])}) if db is not None else None
    if user:
        user['id'] = str(user['_id'])
    return render_template("report_found.html", user=user)

# ---------- LOST ITEM RESULT & AI MATCHES ----------
@app.route("/user/lost-item/<item_id>")
def lost_item_result(item_id):
    if "user_id" not in session:
        return redirect("/login")

    db = get_db()
    if db is None:
        flash("System unavailable. Please try again later.", "error")
        return redirect("/user/dashboard")

    try:
        lost_item = db.lost_items.find_one({"_id": ObjectId(item_id)})
    except Exception:
        lost_item = None

    if not lost_item:
        flash("Lost item report not found.", "error")
        return redirect("/user/dashboard")

    lost_item["id"] = str(lost_item["_id"])

    user = db.users.find_one({"_id": ObjectId(session["user_id"])})
    if user:
        user["id"] = str(user["_id"])

    # Search candidates using stored db.ai_matches records or live image_similarity_service
    lost_embedding = lost_item.get("embedding")
    candidates = []
    ai_status = "available"

    # 1. Query persisted candidate match records from db.ai_matches
    stored_matches = list(db.ai_matches.find({
        "lostReportId": ObjectId(item_id),
        "status": {"$in": ["candidate", "confirmed"]}
    }).sort("similarityScore", -1))

    if stored_matches:
        for m in stored_matches:
            found_doc = db.found_items.find_one({"_id": m["foundReportId"]})
            if found_doc:
                candidates.append({
                    "id": str(found_doc["_id"]),
                    "reportId": str(found_doc["_id"]),
                    "item_name": found_doc.get("item_name", found_doc.get("title", "Found Item")),
                    "description": found_doc.get("description", ""),
                    "location": found_doc.get("location", "Unknown Location"),
                    "date": found_doc.get("date", "Unknown Date"),
                    "category": found_doc.get("category", "General"),
                    "image_path": found_doc.get("image_path", "static/images/default_item.png"),
                    "similarity_score": m.get("similarityScore", 0.0),
                    "similarity_pct": m.get("similarityPercentage", 0),
                    "created_at": m.get("createdAt")
                })
    elif lost_embedding:
        candidates = image_similarity_service.find_similar_found_items(lost_embedding, db, threshold=0.50, top_k=10)
    else:
        ai_healthy, _ = image_similarity_service.check_ai_health()
        if not ai_healthy:
            ai_status = "unavailable"

    return render_template("lost_item_result.html", user=user, item=lost_item, candidates=candidates, ai_status=ai_status)


# ---------- ADMIN SETTINGS ----------
@app.route("/admin/settings")
def admin_settings():
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    db = get_db()
    if db is None:
         flash("Database Error: Settings currently unavailable.", "error")
         return redirect("/")
         
    return render_template("admin_settings.html")

# ---------- ADMIN DASHBOARD & MATCHING ----------
@app.route("/admin/dashboard")
def admin_dashboard():
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    db = get_db()
    if db is None:
        flash("Database unavailable. Please try again later.", "error")
        return redirect("/")
    
    # Stats
    total_users = db.users.count_documents({"role": "user"})
    active_lost = db.lost_items.count_documents({"status": "lost"})
    active_found = db.found_items.count_documents({"status": "found"})
    
    # Fetch RAW items for the "Recent Reports" feed
    try:
        lost_items = list(db.lost_items.find({"status": "lost"}).sort("created_at", -1).limit(10))
        found_items = list(db.found_items.find({"status": "found"}).sort("created_at", -1).limit(10))
    except Exception as e:
        lost_items = []
        found_items = []

    # Fetch Candidate Matches from db.ai_matches collection
    matches = []
    try:
        pending_matches_cursor = list(db.ai_matches.find({"status": "candidate"}).sort("similarityScore", -1))
        
        for m in pending_matches_cursor:
            lost = db.lost_items.find_one({"_id": m["lostReportId"], "status": "lost"})
            found = db.found_items.find_one({"_id": m["foundReportId"], "status": "found"})
            
            if lost and found:
                lost['lost_id'] = str(lost['_id'])
                found['found_id'] = str(found['_id'])
                sim_pct = m.get("similarityPercentage", int(round(m.get("similarityScore", 0) * 100)))
                
                matches.append({
                    "lost": lost,
                    "found": found,
                    "score": {
                        "final_score": sim_pct,
                        "image_score": sim_pct,
                        "text_score": sim_pct
                    }
                })
    except Exception as e:
        print(f"Pending AI Matches Fetch Error: {e}")

    pending_matches_count = len(matches)

    return render_template(
        "admin_dashboard.html",
        lost_items=lost_items,
        found_items=found_items,
        matches=matches,
        total_users=total_users,
        active_lost=active_lost,
        active_found=active_found,
        pending_matches_count=pending_matches_count
    )

import threading

def ensure_item_embedding(db, item, item_type):
    """Ensures that a lost or found item has an AI vector embedding."""
    if item.get("embedding"):
        return item["embedding"]
    
    img_path = item.get("image_path") or (item.get("images", [None])[0] if item.get("images") else None)
    if not img_path:
        return None

    full_path = img_path if os.path.isabs(img_path) else os.path.join(app.root_path, img_path)
    if not os.path.exists(full_path):
        return None

    try:
        with open(full_path, "rb") as f:
            img_bytes = f.read()
        embedding = image_similarity_service.generate_image_embedding(img_bytes)
        if embedding:
            coll = db.lost_items if item_type == "lost" else db.found_items
            coll.update_one({"_id": item["_id"]}, {"$set": {"embedding": embedding}})
            item["embedding"] = embedding
            db.images.update_one(
                {"reportId": item["_id"]},
                {"$set": {"aiEmbedding": embedding, "aiProcessed": True, "aiProcessedAt": datetime.datetime.now(datetime.timezone.utc)}}
            )
            print(f"[AI Scan] Generated missing embedding for {item_type} item {item['_id']}")
            return embedding
    except Exception as e:
        print(f"[AI Scan] Error generating embedding for {item_type} item {item['_id']}: {e}")

    return None

def background_scan(force_rescan):
    try:
        db = get_db()
        if db is None:
            return
            
        if force_rescan:
            db.ai_matches.delete_many({"status": "candidate"})
        
        # 1. Ensure found items have embeddings
        found_items = list(db.found_items.find({"status": "found"}))
        for found in found_items:
            ensure_item_embedding(db, found, "found")

        # 2. Ensure lost items have embeddings & match against found items
        lost_items = list(db.lost_items.find({"status": "lost"}))
        count = 0
        
        for lost in lost_items:
            lost_embedding = ensure_item_embedding(db, lost, "lost")
            
            if lost_embedding:
                primary_img_id = lost.get("imageIds", [None])[0] if lost.get("imageIds") else None
                created_ids = image_similarity_service.create_match_records(
                    db=db,
                    lost_report_id=lost["_id"],
                    lost_image_id=primary_img_id,
                    lost_user_id=lost.get("user_id"),
                    lost_embedding=lost_embedding,
                    threshold=0.50
                )
                count += len(created_ids)

        print(f"Background AI Scan Complete. Generated {count} candidate match records.")
    except Exception as e:
        print(f"Background AI Scan Failed: {e}")

# ---------- TRIGGER SCANS ----------
@app.route("/admin/run-scan")
def run_ai_scan():
    role = session.get("role")
    if role not in ["admin", "super_admin"]:
        abort(403)
        
    force_rescan = request.args.get('force') == 'true' or True
    
    background_scan(force_rescan)
                
    flash("AI Scan completed successfully! New image similarity matches updated below.", "success")
    ref = request.referrer or "/admin/dashboard"
    if "/superadmin/dashboard" in ref:
        return redirect("/superadmin/dashboard")
    return redirect("/admin/dashboard")

# ---------- ADMIN: APPROVE MATCH ----------
@app.route("/admin/approve-match/<lost_id>/<found_id>")
def approve_match(lost_id, found_id):
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/admin/dashboard")
    
    lost_obj_id = ObjectId(lost_id) if ObjectId.is_valid(lost_id) else lost_id
    found_obj_id = ObjectId(found_id) if ObjectId.is_valid(found_id) else found_id

    lost_item = db.lost_items.find_one({"_id": lost_obj_id})
    found_item = db.found_items.find_one({"_id": found_obj_id})
    
    if lost_item and found_item:
        # 1. Update status in db.ai_matches to 'approved_by_admin'
        cand_match = db.ai_matches.find_one_and_update(
            {"lostReportId": lost_obj_id, "foundReportId": found_obj_id},
            {"$set": {"status": "approved_by_admin", "adminApprovedAt": datetime.datetime.now(datetime.timezone.utc)}},
            return_document=True
        )
        if not cand_match:
            cand_res = db.ai_matches.insert_one({
                "lostReportId": lost_obj_id,
                "foundReportId": found_obj_id,
                "lostUserId": lost_item["user_id"],
                "foundUserId": found_item["user_id"],
                "similarityScore": 0.95,
                "similarityPercentage": 95,
                "status": "approved_by_admin",
                "createdAt": datetime.datetime.now(datetime.timezone.utc)
            })
            cand_id = cand_res.inserted_id
        else:
            cand_id = cand_match["_id"]

        # 2. DO NOT create chat room directly! Send 'Item Matched' notification to both users
        db.notifications.insert_one({
             "user_id": lost_item["user_id"],
             "match_id": cand_id,
             "lost_item_id": lost_obj_id,
             "found_item_id": found_obj_id,
             "found_img": found_item.get("image_path"),
             "item_name": lost_item.get("item_name", "Item"),
             "location": found_item.get("location", "Unknown"),
             "is_read": False,
             "created_at": datetime.datetime.now(datetime.timezone.utc),
             "type": "match_pending_confirmation",
             "message": f"AI Match Approved by Admin! Please review details to confirm if '{lost_item.get('item_name')}' is your lost item."
        })

        db.notifications.insert_one({
             "user_id": found_item["user_id"],
             "match_id": cand_id,
             "lost_item_id": lost_obj_id,
             "found_item_id": found_obj_id,
             "found_img": found_item.get("image_path"),
             "item_name": found_item.get("item_name", "Item"),
             "location": found_item.get("location", "Unknown"),
             "is_read": False,
             "created_at": datetime.datetime.now(datetime.timezone.utc),
             "type": "match_pending_confirmation",
             "message": f"AI Match Approved by Admin for your found item '{found_item.get('item_name')}'. Awaiting user confirmation."
        })
        
    flash("Match approved by admin! Notification sent to the user for confirmation.", "success")
    ref = request.referrer or "/admin/dashboard"
    if "/superadmin/dashboard" in ref:
        return redirect("/superadmin/dashboard")
    return redirect("/admin/dashboard")

# ---------- USER AI MATCH REVIEW & CONFIRMATION ----------
@app.route("/user/review-match/<match_id>")
def user_review_match(match_id):
    if "user_id" not in session:
        return redirect("/login")
        
    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/history")

    match_obj_id = ObjectId(match_id) if ObjectId.is_valid(match_id) else match_id
    cand = db.ai_matches.find_one({"_id": match_obj_id})
    if not cand:
        flash("Match candidate not found or already processed.", "error")
        return redirect("/user/history")

    lost_item = db.lost_items.find_one({"_id": cand["lostReportId"]})
    found_item = db.found_items.find_one({"_id": cand["foundReportId"]})

    if not lost_item or not found_item:
        flash("Item data not available.", "error")
        return redirect("/user/history")

    sim_pct = cand.get("similarityPercentage", int(round(cand.get("similarityScore", 0) * 100)))
    user = db.users.find_one({"_id": ObjectId(session["user_id"])})

    return render_template("user_match_review.html", user=user, match_id=str(cand["_id"]), lost_item=lost_item, found_item=found_item, similarity_pct=sim_pct)

@app.route("/user/match-action/<match_id>/<action>")
def user_match_action(match_id, action):
    if "user_id" not in session:
        return redirect("/login")

    if action not in ['confirm', 'reject']:
        abort(400)

    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/user/history")

    match_obj_id = ObjectId(match_id) if ObjectId.is_valid(match_id) else match_id
    cand = db.ai_matches.find_one({"_id": match_obj_id})
    if not cand:
        flash("Match candidate not found or already processed.", "error")
        return redirect("/user/history")

    lost_id = cand["lostReportId"]
    found_id = cand["foundReportId"]
    lost_user_id = cand["lostUserId"]
    found_user_id = cand["foundUserId"]

    if action == 'confirm':
        # Update match status in db.ai_matches, lost_items, found_items, and db.images
        db.ai_matches.update_one({"_id": match_obj_id}, {"$set": {"status": "confirmed", "confirmedAt": datetime.datetime.now(datetime.timezone.utc)}})
        db.lost_items.update_one({"_id": lost_id}, {"$set": {"status": "matched", "matched_at": datetime.datetime.now(datetime.timezone.utc)}})
        db.found_items.update_one({"_id": found_id}, {"$set": {"status": "matched", "matched_at": datetime.datetime.now(datetime.timezone.utc)}})
        db.images.update_many(
            {"reportId": {"$in": [lost_id, found_id, str(lost_id), str(found_id)]}},
            {"$set": {"isMatched": True, "status": "matched"}}
        )

        image_similarity_service.record_training_pair(db, str(lost_id), str(found_id), session.get("user_id"), "confirmed")

        lost_item = db.lost_items.find_one({"_id": lost_id})
        found_item = db.found_items.find_one({"_id": found_id})

        # NOW Create Chat Room between lost user and found user (or reuse existing)
        item_title = lost_item.get("item_name", "Matched Item") if lost_item else "Matched Item"
        existing_chat = db.chats.find_one({
            "$or": [
                {"lost_user_id": lost_user_id, "found_user_id": found_user_id, "item_name": item_title},
                {"lost_user_id": found_user_id, "found_user_id": lost_user_id, "item_name": item_title}
            ]
        })
        if existing_chat:
            chat_id = existing_chat["_id"]
        else:
            chat_data = {
                "lost_item_id": lost_id,
                "found_item_id": found_id,
                "lost_user_id": lost_user_id,
                "found_user_id": found_user_id,
                "item_name": item_title,
                "item_image": lost_item.get("image_path") if lost_item else (found_item.get("image_path") if found_item else ""),
                "found_location": found_item.get("location") if found_item else "",
                "status": "active",
                "created_at": datetime.datetime.now(datetime.timezone.utc),
                "messages": [
                    {
                        "sender": "system",
                        "text": "Match confirmed by owner! You can now chat to arrange handover.",
                        "timestamp": datetime.datetime.now(datetime.timezone.utc)
                    }
                ]
            }
            chat_res = db.chats.insert_one(chat_data)
            chat_id = chat_res.inserted_id

        # Notify finder
        db.notifications.insert_one({
            "user_id": found_user_id,
            "lost_item_id": lost_id,
            "found_item_id": found_id,
            "found_img": found_item.get("image_path") if found_item else "",
            "item_name": lost_item.get("item_name", "Item") if lost_item else "Item",
            "is_read": False,
            "created_at": datetime.datetime.now(datetime.timezone.utc),
            "type": "match_confirmed",
            "message": f"Match Confirmed! The owner confirmed your found item is theirs. You can now chat to arrange handover."
        })

        flash("Match confirmed! Direct chat room created with the finder.", "success")
        return redirect(f"/user/chat/{chat_id}")

    else:
        # Action is reject
        db.ai_matches.update_one({"_id": match_obj_id}, {"$set": {"status": "rejected_by_user"}})
        image_similarity_service.record_training_pair(db, str(lost_id), str(found_id), session.get("user_id"), "rejected")

        flash("Match candidate declined. Thank you for clarifying.", "info")
        return redirect("/user/history")

# ---------- ADMIN: REJECT MATCH ----------
@app.route("/admin/reject-match/<lost_id>/<found_id>")
def reject_match(lost_id, found_id):
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    db = get_db()
    if db is None:
        flash("Database error.", "error")
        return redirect("/admin/dashboard")

    # Record rejected training pair in db.training_pairs and update db.ai_matches status to 'rejected'
    image_similarity_service.record_training_pair(db, lost_id, found_id, session.get("user_id"), "rejected")

    flash("Candidate match rejected. Recorded in training dataset for AI fine-tuning.", "info")
    ref = request.referrer or "/admin/dashboard"
    if "/superadmin/dashboard" in ref:
        return redirect("/superadmin/dashboard")
    return redirect("/admin/dashboard")



@app.route("/user/chat/<chat_id>", methods=["GET", "POST"])
def view_chat(chat_id):
    if session.get("role") != "user":
        abort(403)
        
    db = get_db()
    chat = db.chats.find_one({"_id": ObjectId(chat_id)})
    
    if not chat:
        return "Chat not found", 404
        
    # Verify Access
    current_user_id = ObjectId(session["user_id"])
    if current_user_id not in [chat["lost_user_id"], chat["found_user_id"]]:
        abort(403)
        
    if request.method == "POST":
        text = request.form.get("message")
        if text:
            msg = {
                "sender_id": current_user_id,
                "text": text,
                "timestamp": datetime.datetime.now(datetime.timezone.utc)
            }
            db.chats.update_one(
                {"_id": ObjectId(chat_id)},
                {"$push": {"messages": msg}}
            )
            return redirect(f"/user/chat/{chat_id}")
            
    # Prepare messages for template
    for msg in chat["messages"]:
        if msg.get("sender") == "system":
            msg["is_me"] = False
            msg["is_system"] = True
        else:
            msg["is_system"] = False
            msg["is_me"] = (msg["sender_id"] == current_user_id)
            
    return render_template("chat_room.html", chat=chat, current_user_id=current_user_id)
@app.route("/logout")
def logout():
    session.clear()
    return redirect("/")

# ---------- ADMIN : USERS MANAGEMENT ----------
@app.route("/admin/users")
def admin_users():
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    db = get_db()
    if db is None:
        flash("Database Error: User list currently unavailable.", "error")
        return redirect("/")

    users = list(db.users.find())
    
    # Process users for template
    for u in users:
        u['user_id'] = str(u['_id'])
    
    return render_template("admin_users.html", users=users)

# ---------- ACTIVATE USER ----------
@app.route("/admin/user/activate/<user_id>")
def activate_user(user_id):
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    db = get_db()
    target_user = db.users.find_one({"_id": ObjectId(user_id)})
    
    if not target_user:
        return "User not found", 404

    # Permission Check
    if session["role"] == "admin" and target_user["role"] == "super_admin":
        return "Access Denied: Admins cannot activate Super Admins", 403

    db.users.update_one({"_id": ObjectId(user_id)}, {"$set": {"is_active": True}})
    
    # Log Action
    db.admin_actions.insert_one({
        "admin_id": ObjectId(session["user_id"]),
        "target_user_id": ObjectId(user_id),
        "action": "activate",
        "reason": "Manual Activation",
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
    })

    flash(f"User {target_user.get('name', 'User')} has been activated.")
    return redirect("/admin/users")


# ---------- DEACTIVATE USER ----------
# ---------- DEACTIVATE USER ----------
@app.route("/admin/user/deactivate/<user_id>", methods=["POST"])
def deactivate_user(user_id):
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)

    db = get_db()
    target_user = db.users.find_one({"_id": ObjectId(user_id)})
    
    if not target_user:
        return "User not found", 404

    # Permission Check
    if session["role"] == "admin" and target_user["role"] == "super_admin":
        return "Access Denied: Admins cannot deactivate Super Admins", 403

    reason = request.form.get("reason", "No reason provided")
    
    update_data = {
        "is_active": False,
        "blocked_at": datetime.datetime.now(datetime.timezone.utc),
        "block_reason": reason,
    }
    
    # Increment session version to force logout
    current_version = target_user.get("session_version", 0)
    update_data["session_version"] = current_version + 1

    db.users.update_one({"_id": ObjectId(user_id)}, {"$set": update_data})
    
    # Log Action
    db.admin_actions.insert_one({
        "admin_id": ObjectId(session["user_id"]),
        "target_user_id": ObjectId(user_id),
        "action": "deactivate",
        "reason": reason,
        "timestamp": datetime.datetime.now(datetime.timezone.utc)
    })
    
    # Confirm action
    flash(f"User {target_user.get('name', 'User')} has been deactivated. They will be logged out immediately.")
    return redirect("/admin/users")

# ---------- BLOCKED USER ROUTES ----------
@app.route("/account-blocked")
def account_blocked():
    # If user is somehow active, send them back to dashboard
    if "user_id" in session:
        db = get_db()
        user = db.users.find_one({"_id": ObjectId(session["user_id"])})
        if user and user.get("is_active", True):
            return redirect("/user/dashboard")
    return render_template("account_blocked.html")

@app.route("/request-unblock", methods=["POST"])
def request_unblock():
    if "user_id" not in session:
        return redirect("/login")

    reason = request.form.get("reason")
    proof = request.files.get("proof")
    
    proof_path = None
    if proof and proof.filename:
        # Secure filename with timestamp
        timestamp = int(datetime.datetime.now(datetime.timezone.utc).timestamp())
        filename = f"proof_{session['user_id']}_{timestamp}_{proof.filename}"
        proof_path = f"uploads/profile/{filename}" # Store in uploads/profile for now or create new folder
        proof.save(proof_path)

    db = get_db()
    
    # Rate limit check (optional simple check: pending request exists?)
    existing_request = db.unblock_requests.find_one({
        "user_id": ObjectId(session["user_id"]),
        "status": "pending"
    })
    
    if existing_request:
        flash("You already have a pending request.")
        return redirect("/account-blocked")

    db.unblock_requests.insert_one({
        "user_id": ObjectId(session["user_id"]),
        "reason": reason,
        "proof_path": proof_path,
        "status": "pending",
        "created_at": datetime.datetime.now(datetime.timezone.utc)
    })
    
    flash("Unblock request submitted successfully.")
    return redirect("/account-blocked")

# ---------- SUPER ADMIN : UNBLOCK REQUESTS ----------
@app.route("/superadmin/unblock-requests")
def view_unblock_requests():
    if session.get("role") != "super_admin":
        abort(403)

    db = get_db()
    
    # Aggregate to join with user details
    pipeline = [
        {"$sort": {"created_at": -1}},
        {"$lookup": {
            "from": "users",
            "localField": "user_id",
            "foreignField": "_id",
            "as": "user_info"
        }},
        {"$unwind": "$user_info"}
    ]
    
    requests = list(db.unblock_requests.aggregate(pipeline))
    
    # Format IDs for template
    for req in requests:
        req['id'] = str(req['_id'])
        req['user_id'] = str(req['user_id'])
        
    return render_template("superadmin_requests.html", requests=requests)

@app.route("/superadmin/request/<request_id>/<action>", methods=["POST"])
def process_unblock_request(request_id, action):
    if session.get("role") != "super_admin":
        abort(403)
        
    db = get_db()
    req = db.unblock_requests.find_one({"_id": ObjectId(request_id)})
    
    if not req:
        return "Request not found", 404
        
    if action == "approve":
        # 1. Activate User
        db.users.update_one({"_id": req["user_id"]}, {"$set": {"is_active": True}})
        # 2. Update Request Status
        db.unblock_requests.update_one({"_id": ObjectId(request_id)}, {"$set": {"status": "approved"}})
        flash("User unblocked successfully.")
        
    elif action == "reject":
        # Update Request Status
        db.unblock_requests.update_one({"_id": ObjectId(request_id)}, {"$set": {"status": "rejected"}})
        flash("Unblock request rejected.")
        
    return redirect("/superadmin/unblock-requests")

# ---------- CREATE ADMIN (Super Admin Only) ----------
@app.route("/superadmin/create-admin", methods=["GET", "POST"])
def create_admin():
    if session.get("role") != "super_admin":
        abort(403)

    if request.method == "POST":
        name = request.form["name"]
        email = request.form["email"]
        raw_password = request.form["password"]
        if not is_valid_password(raw_password):
            flash("Password must be at least 8 characters long and include a special character.")
            return redirect(request.url)
            
        password = generate_password_hash(raw_password)

        db = get_db()
        if db.users.find_one({"email": email}):
             flash("Email already exists")
             return redirect(request.url)

        db.users.insert_one({
            "name": name,
            "email": email,
            "password": password,
            "role": "admin",
            "is_active": True,
            "profile_completed": True,
            "college": "Admin Dept",
            "study": "Administration",
            "phone": "0000000000",
            "created_at": datetime.datetime.now(datetime.timezone.utc)
        })
        return redirect("/superadmin/dashboard")

    return render_template("create_admin.html")



@app.route("/api/notifications/mark-read/<notif_id>", methods=["POST"])
def mark_notification_read(notif_id):
    if "user_id" not in session:
        return {"error": "Unauthorized"}, 401
        
    db = get_db()
    if db is None:
        return {"error": "Database unavailable"}, 500
        
    user_id_raw = str(session["user_id"])
    user_id_query = {"$in": [ObjectId(user_id_raw), user_id_raw]}
    
    notif_obj_id = ObjectId(notif_id) if ObjectId.is_valid(notif_id) else notif_id
    db.notifications.update_one(
        {"_id": notif_obj_id, "user_id": user_id_query},
        {"$set": {"is_read": True}}
    )
    return {"status": "success"}

# ---------- ERROR ----------
@app.errorhandler(403)
def forbidden(e):
    return "403 Forbidden – Access Denied", 403

# ---------- RUN ----------
@app.route("/admin/export-data")
def export_data():
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    db = get_db()
    
    # --- Custom PDF Class ---
    class PDF(FPDF):
        def header(self):
            # Logo
            logo_path = os.path.join(app.root_path, 'uploads', 'foundify_logo.png')
            if os.path.exists(logo_path):
                self.image(logo_path, 10, 8, 15)
            # Font
            self.set_font('helvetica', 'B', 20)
            # Title
            self.cell(0, 15, 'Foundify System Report', border=False, align='C')
            self.ln(20)

        def footer(self):
            self.set_y(-15)
            self.set_font('helvetica', 'I', 8)
            self.cell(0, 10, f'Page {self.page_no()}/{{nb}}', align='C')

    # --- Generate PDF ---
    pdf = PDF()
    pdf.alias_nb_pages()
    pdf.add_page()
    pdf.set_font('helvetica', '', 12)

    # 1. Summary Section
    users = list(db.users.find({}, {"password": 0}))
    lost_items = list(db.lost_items.find({}))
    found_items = list(db.found_items.find({}))
    confirmed_matches = list(db.chats.find({})) # Chats represent confirmed matches

    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, 'Executive Summary', ln=True)
    pdf.set_font('helvetica', '', 12)
    
    # Draw simple stats box
    pdf.set_fill_color(240, 240, 240)
    pdf.cell(60, 10, f'Lost Items: {len(lost_items)}', border=1, fill=True, align='C')
    pdf.cell(60, 10, f'Found Items: {len(found_items)}', border=1, fill=True, align='C')
    pdf.cell(60, 10, f'Resolved Matches: {len(confirmed_matches)}', border=1, fill=True, align='C', ln=True)
    pdf.ln(10)

    # Helper for Tables
    def draw_table_header(headers, widths):
        pdf.set_font('helvetica', 'B', 10)
        pdf.set_fill_color(246, 173, 85) # Brand Orange
        pdf.set_text_color(255, 255, 255)
        for h, w in zip(headers, widths):
            pdf.cell(w, 8, h, border=1, fill=True, align='C')
        pdf.ln()
        pdf.set_text_color(0, 0, 0)
        pdf.set_font('helvetica', '', 9)

    # Helper map for user names
    user_map = {u['_id']: u.get('name', 'Unknown') for u in users}

    # 2. MATCHED ITEMS (Resolutions)
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, 'Resolved Matches (Recovered Items)', ln=True)

    m_headers = ['Item Name', 'Reporter', 'Finder', 'Date Matched']
    m_widths = [60, 50, 50, 30]
    draw_table_header(m_headers, m_widths)

    item_fill = False
    for chat in confirmed_matches:
        pdf.set_fill_color(245, 245, 245) if item_fill else pdf.set_fill_color(255, 255, 255)
        
        item_name = chat.get('item_name', 'Unknown')
        reporter = user_map.get(chat.get('lost_user_id'), 'Unknown')
        finder = user_map.get(chat.get('found_user_id'), 'Unknown')
        date = chat.get('created_at').strftime('%Y-%m-%d') if chat.get('created_at') else 'N/A'
        
        pdf.cell(m_widths[0], 8, item_name[:30], border=1, fill=True)
        pdf.cell(m_widths[1], 8, reporter[:25], border=1, fill=True)
        pdf.cell(m_widths[2], 8, finder[:25], border=1, fill=True)
        pdf.cell(m_widths[3], 8, date, border=1, fill=True, align='C', ln=True)
        item_fill = not item_fill

    pdf.ln(10)

    # 3. Lost Items Table
    if pdf.get_y() > 250: # Check if near bottom of page
        pdf.add_page()
    
    pdf.set_font('helvetica', 'B', 14)
    pdf.cell(0, 10, 'Lost Items Report', ln=True)
    
    l_headers = ['Item', 'Date', 'Location', 'Status']
    l_widths = [50, 40, 60, 30]
    draw_table_header(l_headers, l_widths)

    item_fill = False
    for item in lost_items:
        pdf.set_fill_color(245, 245, 245) if item_fill else pdf.set_fill_color(255, 255, 255)
        name = item.get('item_name', 'N/A')
        date = item.get('date', 'N/A')
        loc = item.get('location', 'N/A')
        status = item.get('status', 'unknown')
        
        pdf.cell(l_widths[0], 8, name[:25], border=1, fill=True)
        pdf.cell(l_widths[1], 8, date, border=1, fill=True)
        pdf.cell(l_widths[2], 8, loc[:30], border=1, fill=True)
        pdf.cell(l_widths[3], 8, status, border=1, fill=True, align='C', ln=True)
        item_fill = not item_fill

    # Output
    pdf_bytes = bytes(pdf.output())
    return Response(
        pdf_bytes,
        mimetype='application/pdf',
        headers={'Content-Disposition': 'attachment;filename=foundify_report.pdf'}
    )

# ---------- SOCKET IO EVENTS ----------
room_active_users: dict[str, set[str]] = {}

@socketio.on('join')
def on_join(data):
    room = str(data.get('room', ''))
    user_id = str(data.get('user_id', ''))
    if not room:
        return
    join_room(room)
    if room not in room_active_users:
        room_active_users[room] = set()
    if user_id:
        room_active_users[room].add(user_id)
        db = get_db()
        if db is not None:
            user_obj = ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id
            db.users.update_one({"_id": user_obj}, {"$set": {"last_seen": datetime.datetime.now(datetime.timezone.utc)}})
    
    emit('presence_update', {
        'room': room,
        'active_users': list(room_active_users[room]),
        'user_id': user_id,
        'last_seen': 'Just now'
    }, to=room)
    print(f"User {user_id} joined room {room}. Active: {list(room_active_users[room])}")

@socketio.on('leave')
def on_leave(data):
    room = str(data.get('room', ''))
    user_id = str(data.get('user_id', ''))
    if not room:
        return
    leave_room(room)
    if room in room_active_users and user_id in room_active_users[room]:
        room_active_users[room].remove(user_id)
    
    now_dt = datetime.datetime.now(datetime.timezone.utc)
    if user_id:
        db = get_db()
        if db is not None:
            user_obj = ObjectId(user_id) if ObjectId.is_valid(user_id) else user_id
            db.users.update_one({"_id": user_obj}, {"$set": {"last_seen": now_dt}})

    last_seen_str = format_last_seen(now_dt)
    emit('presence_update', {
        'room': room,
        'active_users': list(room_active_users.get(room, [])),
        'user_id': user_id,
        'last_seen': last_seen_str
    }, to=room)
    print(f"User {user_id} left room {room}")

@socketio.on('send_message')
def on_send_message(data):
    room = data.get('room')
    message_text = data.get('message', '').strip()
    sender_id = data.get('sender_id')
    
    if not room or not message_text or not sender_id:
        return

    db = get_db()
    room_obj = ObjectId(room) if ObjectId.is_valid(room) else room

    # Prevent messaging if handover is already completed
    chat = db.chats.find_one({"_id": room_obj})
    if not chat or chat.get("status") == "handover_completed":
        print(f"Message blocked: Chat {room} is closed due to completed handover.")
        return

    now = datetime.datetime.now(datetime.timezone.utc)
    sender_obj = ObjectId(sender_id) if ObjectId.is_valid(sender_id) else sender_id

    message_doc = {
        "sender_id": sender_obj,
        "text": message_text,
        "timestamp": now,
        "is_read": False
    }

    # 1. INSTANT PERSISTENCE IN MONGODB
    db.chats.update_one(
        {"_id": room_obj},
        {
            "$push": {"messages": message_doc},
            "$set": {"last_updated": now}
        }
    )

    time_str = now.strftime('%I:%M %p').lstrip('0')
    chat = db.chats.find_one({"_id": room_obj})
    
    if chat:
        lost_u = str(chat.get("lost_user_id"))
        found_u = str(chat.get("found_user_id"))
        recipient_id_raw = found_u if lost_u == str(sender_id) else lost_u
        recipient_id = ObjectId(recipient_id_raw) if ObjectId.is_valid(recipient_id_raw) else recipient_id_raw
        
        sender_user = db.users.find_one({"_id": sender_obj})
        sender_name = sender_user.get("name", "User") if sender_user else "User"
        
        db.notifications.insert_one({
            "user_id": recipient_id,
            "chat_id": str(room),
            "found_img": chat.get("item_image", ""),
            "item_name": chat.get("item_name", "Chat Message"),
            "is_read": False,
            "created_at": now,
            "type": "chat_message",
            "message": f"New message from {sender_name}: {message_text[:50]}"
        })

    # 2. EMIT 0-LATENCY EVENT TO IN-ROOM VIEWERS
    emit('receive_message', {
        "text": message_text,
        "sender_id": str(sender_id),
        "room": str(room),
        "timestamp": time_str
    }, to=str(room))

    # 3. BROADCAST 0-LATENCY REAL-TIME CHAT LIST UPDATE FOR USER_CHATS
    emit('chat_list_update', {
        "chat_id": str(room),
        "latest_text": message_text,
        "latest_time_str": time_str,
        "sender_id": str(sender_id)
    }, broadcast=True)

# ---------- ADMIN CLAIMS VERIFICATION ----------
@app.route("/admin/claims")
def admin_claims():
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    db = get_db()
    pending_claims = list(db.claims.find({"status": "pending"}).sort("created_at", -1))
    for claim in pending_claims:
        claim['id'] = str(claim['_id'])
        claimant = db.users.find_one({"_id": claim['claimant_id']})
        if claimant:
            claim['claimant_name'] = claimant.get('name')
        item = db.found_items.find_one({"_id": claim['found_item_id']})
        if item:
            claim['item_name'] = item.get('item_name')
            claim['item_image'] = item.get('image_path')
            
    return render_template("admin_claims.html", claims=pending_claims)

@app.route("/admin/claims/<claim_id>")
def admin_claim_details(claim_id):
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    db = get_db()
    claim = db.claims.find_one({"_id": ObjectId(claim_id)})
    if not claim:
        flash("Claim not found", "error")
        return redirect("/admin/claims")
        
    claim['id'] = str(claim['_id'])
    
    claimant = db.users.find_one({"_id": claim["claimant_id"]})
    finder = db.users.find_one({"_id": claim["finder_id"]})
    found_item = db.found_items.find_one({"_id": claim["found_item_id"]})
    
    if claimant: claimant['id'] = str(claimant['_id'])
    if finder: finder['id'] = str(finder['_id'])
    if found_item: found_item['id'] = str(found_item['_id'])
    
    # Fetch claimant's lost items to serve as "lost photos" for comparison
    claimant_lost_items = list(db.lost_items.find({"user_id": claim["claimant_id"]}).sort("created_at", -1))
    for item in claimant_lost_items:
        item['id'] = str(item['_id'])
        
    return render_template("admin_claim_details.html", claim=claim, claimant=claimant, finder=finder, found_item=found_item, claimant_lost_items=claimant_lost_items)

@app.route("/admin/claims/<claim_id>/<action>")
def admin_process_claim(claim_id, action):
    if session.get("role") not in ["admin", "super_admin"]:
        abort(403)
        
    if action not in ['approve', 'reject']:
        abort(400)
        
    db = get_db()
    claim = db.claims.find_one({"_id": ObjectId(claim_id)})
    if not claim:
        flash("Claim not found.", "error")
        return redirect("/admin/claims")
        
    claimant = db.users.find_one({"_id": claim.get("claimant_id")})
    finder = db.users.find_one({"_id": claim.get("finder_id")})
    found_item = db.found_items.find_one({"_id": claim.get("found_item_id")})
    
    if not claimant or not found_item:
        flash("Missing user or item data.", "error")
        return redirect("/admin/claims")

    claimant_name = claimant.get('name', 'User')
    claimant_email = claimant.get('email', 'unknown@example.com')
    item_name = found_item.get('item_name', 'your item')
    
    # Find lost item associated with claimant for dataset pair tracking
    lost_item = db.lost_items.find_one({"user_id": claim.get("claimant_id")})
    lost_id = lost_item["_id"] if lost_item else claim.get("found_item_id")
    found_id = claim.get("found_item_id")

    if action == 'approve':
        status = 'approved'
        db.claims.update_one({"_id": ObjectId(claim_id)}, {"$set": {"status": status}})
        image_similarity_service.record_training_pair(db, lost_id, found_id, session.get("user_id"), "confirmed")
        
        finder_name = finder.get('name', 'Unknown') if finder else 'Unknown'
        finder_phone = finder.get('phone', 'Not provided') if finder else 'Not provided'
        finder_email = finder.get('email', 'Not provided') if finder else 'Not provided'
        
        # Automated Email Simulation for Approval
        print("\n" + "="*60)
        print("                SYSTEM EMAIL TRIGGERED (APPROVAL)           ")
        print("="*60)
        print(f"To: {claimant_email} ({claimant_name})")
        print(f"Subject: Claim Approved - We found your item!")
        print(f"\nBody:\nGreat news {claimant_name}! We verified your claim for '{item_name}'.\n")
        print(f"You can now contact the finder to retrieve your item:")
        print(f"Finder Name: {finder_name}")
        print(f"Finder Phone: {finder_phone}")
        print(f"Finder Email: {finder_email}")
        print("="*60 + "\n")
        
        flash(f"Claim approved! Automated approval email sent to {claimant_email}.", "success")
        
    else:
        status = 'rejected'
        db.claims.update_one(
            {"_id": ObjectId(claim_id)}, 
            {"$set": {"status": status, "payment_status": "refunded"}}
        )
        image_similarity_service.record_training_pair(db, lost_id, found_id, session.get("user_id"), "rejected")
        
        # Automated Email Simulation for Rejection and Refund
        print("\n" + "="*60)
        print("                SYSTEM EMAIL TRIGGERED (REJECTION)          ")
        print("="*60)
        print(f"To: {claimant_email} ({claimant_name})")
        print(f"Subject: Claim Verification Failed - Refund Processed")
        print(f"\nBody:\nDear {claimant_name},\n\nWe are sorry, but after reviewing your claim for '{item_name}', we determined it is not a true match.")
        print(f"Your payment has been fully refunded and will be returned to your original payment method shortly.\n")
        print(f"We apologize for the inconvenience.")
        print("="*60 + "\n")
        
        flash(f"Claim rejected. Payment refunded and notification email sent to {claimant_email}.", "success")
        
    return redirect("/admin/claims")

if __name__ == "__main__":
    # Get port from environment for deployment (e.g., Render/Heroku)
    port = int(os.environ.get("PORT", 5000))
    debug_mode = os.environ.get("FLASK_DEBUG", "True").lower() == "true"
    
    print(f"Starting Foundify with SocketIO on port {port} (Debug: {debug_mode})...")
    print(f"-> Local URL: http://127.0.0.1:{port}")
    socketio.run(app, host="0.0.0.0", port=port, debug=debug_mode, allow_unsafe_werkzeug=True)
