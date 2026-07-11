import os
import sys
import threading
import time
import json
import requests
import fitz  # PyMuPDF
import uvicorn
import firebase_admin
from firebase_admin import credentials, firestore, auth
from groq import Groq
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.responses import JSONResponse, HTMLResponse

# --- LOCAL DOTENV PARSER ---
def load_dotenv(filepath=".env"):
    if os.path.exists(filepath):
        with open(filepath, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip("'").strip('"')
                    os.environ[k] = v

# --- SECRETS RETRIEVAL HELPER ---
def get_secret(key):
    # 1. Check system environment
    val = os.environ.get(key)
    if val:
        return val
    # 2. Check Streamlit secrets if running inside streamlit
    try:
        import streamlit as st
        if key in st.secrets:
            # Streamlit secrets can be dict-like or string
            secret_val = st.secrets[key]
            if isinstance(secret_val, str):
                return secret_val
            else:
                return dict(secret_val)
    except Exception:
        pass
    return None

# --- CLIENTS INITIALIZATION ---
db_client = None
firebase_initialized = False
groq_initialized = False

def init_services():
    global db_client, firebase_initialized, groq_initialized
    
    firebase_secret = get_secret("FIREBASE_SECRET")
    firebase_web_key = get_secret("FIREBASE_WEB_API_KEY")
    groq_key = get_secret("GROQ_API_KEY")
    
    if firebase_secret:
        try:
            if not firebase_admin._apps:
                # Handle dictionary configuration or string path
                if isinstance(firebase_secret, dict):
                    cred = credentials.Certificate(firebase_secret)
                elif isinstance(firebase_secret, str) and firebase_secret.strip().startswith("{"):
                    cred_dict = json.loads(firebase_secret)
                    cred = credentials.Certificate(cred_dict)
                else:
                    cred = credentials.Certificate(firebase_secret)
                firebase_admin.initialize_app(cred)
            db_client = firestore.client()
            firebase_initialized = True
            print("Firebase Admin successfully initialized.")
        except Exception as e:
            print(f"Warning: Firebase Admin initialization failed: {e}")
            firebase_initialized = False
            
    if groq_key:
        try:
            # Check Groq client initialization
            client = Groq(api_key=groq_key)
            groq_initialized = True
            print("Groq client successfully initialized.")
        except Exception as e:
            print(f"Warning: Groq initialization failed: {e}")
            groq_initialized = False

# --- WEB ENDPOINTS HANDLERS ---
async def serve_index(request):
    try:
        with open("index.html", "r", encoding="utf-8") as f:
            html = f.read()
        return HTMLResponse(content=html)
    except Exception as e:
        return HTMLResponse(content=f"<h3>Error loading frontend file index.html</h3><p>{e}</p>", status_code=500)

async def get_config(request):
    firebase_secret = get_secret("FIREBASE_SECRET")
    firebase_web_key = get_secret("FIREBASE_WEB_API_KEY")
    groq_key = get_secret("GROQ_API_KEY")
    
    has_credentials = bool(firebase_secret and firebase_web_key and groq_key)
    return JSONResponse({
        "sandbox_mode": not has_credentials
    })

async def handle_login(request):
    try:
        body = await request.json()
        email = body.get("email")
        password = body.get("password")
        
        api_key = get_secret("FIREBASE_WEB_API_KEY")
        if not api_key:
            return JSONResponse({"detail": "Firebase API Key not configured"}, status_code=500)
            
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
        response = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True})
        
        if response.status_code == 200:
            res_json = response.json()
            user_record = auth.get_user(res_json["localId"])
            return JSONResponse({
                "success": True,
                "username": user_record.display_name
            })
        else:
            err_msg = response.json().get("error", {}).get("message", "Incorrect credentials").replace("_", " ")
            return JSONResponse({"detail": err_msg}, status_code=401)
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

async def handle_register(request):
    global firebase_initialized, db_client
    if not firebase_initialized:
        return JSONResponse({"detail": "Firebase not initialized"}, status_code=503)
    try:
        body = await request.json()
        email = body.get("email")
        password = body.get("password")
        username = body.get("username")
        
        user_doc = db_client.collection("users").document(username).get()
        if user_doc.exists:
            return JSONResponse({"detail": "Username already registered inside database node."}, status_code=400)
            
        user = auth.create_user(email=email, password=password, display_name=username)
        db_client.collection("users").document(username).set({
            "tasks": [],
            "study_plan": [],
            "email": email,
            "created_at": firestore.SERVER_TIMESTAMP
        })
        return JSONResponse({
            "success": True,
            "username": username
        })
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

async def get_schedule(request):
    global firebase_initialized, db_client
    if not firebase_initialized:
        return JSONResponse({"detail": "Firebase not initialized"}, status_code=503)
    try:
        username = request.query_params.get("username")
        if not username:
            return JSONResponse({"detail": "Missing username"}, status_code=400)
            
        doc = db_client.collection("users").document(username).get()
        if doc.exists:
            data = doc.to_dict()
            return JSONResponse({
                "tasks": data.get("tasks", []),
                "study_plan": data.get("study_plan", [])
            })
        return JSONResponse({"tasks": [], "study_plan": []})
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

async def save_schedule(request):
    global firebase_initialized, db_client
    if not firebase_initialized:
        return JSONResponse({"detail": "Firebase not initialized"}, status_code=503)
    try:
        body = await request.json()
        username = body.get("username")
        tasks_list = body.get("tasks", [])
        plan_list = body.get("study_plan", [])
        
        db_client.collection("users").document(username).update({
            "tasks": tasks_list,
            "study_plan": plan_list,
            "last_updated": firestore.SERVER_TIMESTAMP
        })
        return JSONResponse({"success": True})
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

async def handle_syllabus_upload(request):
    global firebase_initialized, db_client
    form = await request.form()
    file_upload = form.get("file")
    study_hours = int(form.get("study_hours", 3))
    intensity = form.get("intensity", "Balanced")
    skip_weekends = form.get("skip_weekends") == "true"
    start_time = form.get("start_time", "17:30")
    end_time = form.get("end_time", "21:30")
    username = form.get("username", "student")

    if not file_upload:
        return JSONResponse({"detail": "No file uploaded"}, status_code=400)
    
    try:
        file_bytes = await file_upload.read()
        
        # 1. Parse PDF using PyMuPDF (fitz)
        doc = fitz.open(stream=file_bytes, filetype="pdf")
        page_count = len(doc)
        full_text = ""
        for page in doc:
            full_text += page.get_text()
            
        # Filter lines
        filtered_lines = []
        keywords = ["week", "unit", "chapter", "topic", "assignment", "exam", "quiz", "test", "project", "lab", "module", "semester", "subject"]
        for line in full_text.split("\n"):
            clean = line.strip()
            if any(k in clean.lower() for k in keywords) or (len(clean) > 12 and any(c.isdigit() for c in clean)):
                filtered_lines.append(clean)
                
        condensed_text = "\n".join(filtered_lines)[:14000]
        
        # 2. Call Groq
        groq_api_key = get_secret("GROQ_API_KEY")
        if not groq_api_key:
            return JSONResponse({"detail": "Groq API key not configured"}, status_code=500)
            
        client = Groq(api_key=groq_api_key)
        
        prompt = f"""
        Analyze this syllabus text and output a valid JSON string object with exactly two arrays: 'tasks' and 'study_plan'.
        Pattern:
        {{
            "tasks": [{{"task_name": "Name", "due_date": "YYYY-MM-DD"}}],
            "study_plan": [{{"Status": false, "Scheduled Date": "YYYY-MM-DD", "Time Slot": "{start_time} - {end_time}", "Focus Topic": "Topic Text", "Suggested Action": "Action", "Hours Allocated": {study_hours}}}]
        }}
        Read chronologically from top to bottom. Extract specific technical subtopics and chapters. Never emit generic loops. Generate 80-120 items. Year is 2026.
        Text:\n{condensed_text}
        """
        
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a structured database parser. Output valid JSON matching the exact explicit schema array keys. Maximize roadmap precision up to 120 unique cells."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=8192
        )
        
        ai_data = json.loads(response.choices[0].message.content)
        
        raw_tasks = ai_data.get("tasks", [])
        mapped_tasks = [{"task_name": t.get("task_name", "Milestone"), "due_date": t.get("due_date", "2026-06-15")} for t in raw_tasks]
        
        raw_plan = ai_data.get("study_plan", [])
        mapped_plan = [{
            "Status": False,
            "Scheduled Date": i.get("Scheduled Date", i.get("scheduled_date", "2026-06-15")),
            "Time Slot": i.get("Time Slot", i.get("time_slot", f"{start_time} - {end_time}")),
            "Focus Topic": i.get("Focus Topic", i.get("focus_topic", "Concept Review")),
            "Suggested Action": i.get("Suggested Action", i.get("suggested_action", "Practice items")),
            "Hours Allocated": int(i.get("Hours Allocated", i.get("hours_allocated", study_hours)))
        } for i in raw_plan]
        
        # 3. Synchronize with Firebase
        if firebase_initialized and db_client:
            try:
                db_client.collection("users").document(username).update({
                    "tasks": mapped_tasks,
                    "study_plan": mapped_plan,
                    "last_updated": firestore.SERVER_TIMESTAMP
                })
            except Exception:
                db_client.collection("users").document(username).set({
                    "tasks": mapped_tasks,
                    "study_plan": mapped_plan,
                    "last_updated": firestore.SERVER_TIMESTAMP
                })
                
        return JSONResponse({
            "tasks": mapped_tasks,
            "study_plan": mapped_plan,
            "page_count": page_count
        })
        
    except Exception as e:
        return JSONResponse({"detail": str(e)}, status_code=500)

# --- STARLETTE APPLICATION INSTANTIATION ---
routes = [
    Route("/", serve_index),
    Route("/api/config", get_config),
    Route("/api/auth/login", handle_login, methods=["POST"]),
    Route("/api/auth/register", handle_register, methods=["POST"]),
    Route("/api/schedule", get_schedule, methods=["GET"]),
    Route("/api/schedule", save_schedule, methods=["POST"]),
    Route("/api/syllabus/upload", handle_syllabus_upload, methods=["POST"])
]
app = Starlette(debug=True, routes=routes)

# --- EXECUTION ENGINE ROUTING DISPATCHER ---
def is_running_in_streamlit():
    try:
        import streamlit.runtime as st_runtime
        if st_runtime.exists():
            return True
    except ImportError:
        pass
    return False

# Initialize configuration
load_dotenv()
init_services()

if is_running_in_streamlit():
    # --- STREAMLIT WRAPPER RUNNER ---
    import streamlit as st
    
    def start_bg_server():
        print("Starting background Starlette api proxy server on port 8000...")
        uvicorn.run(app, host="127.0.0.1", port=8000, log_level="warning")
        
    if not hasattr(sys, "_starlette_server_running"):
        sys._starlette_server_running = True
        t = threading.Thread(target=start_bg_server, daemon=True)
        t.start()
        time.sleep(0.5)
        
    st.set_page_config(page_title="Study Sync", page_icon="📅", layout="wide")
    
    # Hide Streamlit elements and overlay iframe
    st.html("""
    <style>
        header, footer, [data-testid="stSidebar"], [data-testid="stHeader"] {
            display: none !important;
            visibility: hidden !important;
            height: 0 !important;
        }
        .stApp {
            margin: 0px !important;
            padding: 0px !important;
            background-color: #0B0F19 !important;
        }
        iframe {
            position: fixed;
            top: 0;
            left: 0;
            width: 100vw;
            height: 100vh;
            border: none;
            z-index: 999999;
        }
    </style>
    """)
    
    # Embed Starlette server frame inside Streamlit page
    st.components.v1.html("""
    <iframe src="http://localhost:8000" allow="clipboard-write"></iframe>
    """, height=9999)

else:
    # --- DIRECT PRODUCTION TERMINAL EXECUTION ---
    if __name__ == "__main__":
        print("Starting direct Starlette production web server on port 8501...")
        uvicorn.run(app, host="127.0.0.1", port=8501)