import streamlit as st
import time
import requests
import json
import fitz  # PyMuPDF
import pandas as pd
import uuid  
from datetime import datetime, timedelta, time as dt_time 
from groq import Groq  
import firebase_admin
from firebase_admin import credentials, firestore, auth

# 1. PAGE SETUP & THEME SESSION SYSTEM
st.set_page_config(page_title="Study Sync", page_icon="📅", layout="wide")

if "theme" not in st.session_state:
    st.session_state["theme"] = "Dark"
if "user_authenticated" not in st.session_state:
    st.session_state["user_authenticated"] = False
if "active_username" not in st.session_state:
    st.session_state["active_username"] = ""
if "ai_data" not in st.session_state:
    st.session_state["ai_data"] = None
if "page_count" not in st.session_state:
    st.session_state["page_count"] = 0

# --- DYNAMIC THEME ENGINE (DARK / LIGHT MODE CSS) ---
if st.session_state["theme"] == "Dark":
    st.html("""
    <style>
        .stApp { background-color: #0E1117; color: #FFFFFF; }
        .main-title { font-size: 3.6rem !important; font-weight: 800; background: linear-gradient(90deg, #00C6FF, #0072FF); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .card-box { background: rgba(255, 255, 255, 0.04); border: 1px solid rgba(255, 255, 255, 0.1); border-radius: 12px; padding: 20px; }
        .success-banner { display: flex; align-items: center; background: rgba(0, 198, 255, 0.05); border: 1px solid rgba(0, 198, 255, 0.3); border-radius: 12px; padding: 18px; margin-bottom: 25px; }
        p, h1, h2, h3, h4, h5, h6, label { color: #FFFFFF !important; }
        div.stButton > button:first-child { background: linear-gradient(90deg, #00C6FF, #0072FF) !important; color: white !important; border: none !important; border-radius: 8px !important; padding: 10px 20px !important; font-weight: 600 !important; }
    </style>
    """)
else:
    st.html("""
    <style>
        .stApp { background-color: #F8FAFC; color: #1E293B; }
        .main-title { font-size: 3.6rem !important; font-weight: 800; background: linear-gradient(90deg, #0072FF, #0044AA); -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
        .card-box { background: #FFFFFF; border: 1px solid #E2E8F0; border-radius: 12px; padding: 20px; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05); }
        .success-banner { display: flex; align-items: center; background: #E0F2FE; border: 1px solid #bae6fd; border-radius: 12px; padding: 18px; margin-bottom: 25px; }
        p, h1, h2, h3, h4, h5, h6, label { color: #1E293B !important; }
        div.stButton > button:first-child { background: linear-gradient(90deg, #0072FF, #0044AA) !important; color: white !important; border: none !important; border-radius: 8px !important; padding: 10px 20px !important; font-weight: 600 !important; }
    </style>
    """)

# --- GOOGLE FIREBASE CORE CONNECTION ---
def init_firebase():
    if not firebase_admin._apps:
        fb_credentials = dict(st.secrets["FIREBASE_SECRET"])
        cred = credentials.Certificate(fb_credentials)
        firebase_admin.initialize_app(cred)
    return firestore.client()

db = init_firebase()

# --- SECURITY PROTOCOLS ---
def register_cloud_user(email, password, username):
    try:
        user_doc = db.collection("users").document(username).get()
        if user_doc.exists:
            return False, "Username already registered inside database node."
        user = auth.create_user(email=email, password=password, display_name=username)
        db.collection("users").document(username).set({
            "tasks": [], "study_plan": [], "email": email, "created_at": firestore.SERVER_TIMESTAMP
        })
        return True, "Account registered successfully! Please change to Sign In tab."
    except Exception as e:
        return False, str(e)

def verify_cloud_login(email, password):
    try:
        api_key = st.secrets["FIREBASE_WEB_API_KEY"]
        url = f"https://identitytoolkit.googleapis.com/v1/accounts:signInWithPassword?key={api_key}"
        response = requests.post(url, json={"email": email, "password": password, "returnSecureToken": True})
        if response.status_code == 200:
            user_record = auth.get_user(response.json()["localId"])
            return True, user_record.display_name
        return False, response.json()["error"]["message"].replace("_", " ")
    except Exception as e:
        return False, str(e)

def save_schedule_to_firebase(username, tasks_list, plan_list):
    try:
        db.collection("users").document(username).update({
            "tasks": [{"task_name": t.get("task_name", "Task"), "due_date": t.get("due_date", "")} for t in tasks_list],
            "study_plan": [{k: v for k, v in item.items()} for item in plan_list],
            "last_updated": firestore.SERVER_TIMESTAMP
        })
    except Exception:
        pass

def load_schedule_from_firebase(username):
    try:
        doc = db.collection("users").document(username).get()
        if doc.exists:
            data = doc.to_dict()
            return {"tasks": data.get("tasks", []), "study_plan": data.get("study_plan", [])}
        return None
    except Exception:
        return None

# --- ENGINE ROADMAP TRANSLATION ENGINE ---
def extract_syllabus_with_ai(condensed_text, hours, intensity, no_weekends, start_hr, end_hr):
    try:
        client = Groq(api_key=st.secrets["GROQ_API_KEY"])
        weekend_rule = "STRICT RULE: Do not schedule study blocks on Saturdays or Sundays." if no_weekends else "Use weekends freely."
        prompt = f"""
        Analyze this syllabus text and output a valid JSON string object with exactly two arrays: 'tasks' and 'study_plan'.
        Pattern:
        {{
            "tasks": [{{"task_name": "Name", "due_date": "YYYY-MM-DD"}}],
            "study_plan": [{{"scheduled_date": "YYYY-MM-DD", "time_slot": "{start_hr} - {end_hr}", "focus_topic": "Topic Text", "suggested_action": "Action", "hours_allocated": {hours}}}]
        }}
        Read chronologically from top to bottom. Extract specific technical subtopics and chapters. Never emit generic loops. Generate 80-120 items. Year is 2026.
        Text:\n{condensed_text}
        """
        response = client.chat.completions.create(
            model="llama-3.3-70b-versatile",
            messages=[
                {"role": "system", "content": "You are a structured data extraction engine. You must output a valid JSON object matching the requested schema keys ('tasks' and 'study_plan') exactly. Do not use abbreviated shorthand or single-character keys."},
                {"role": "user", "content": prompt}
            ],
            response_format={"type": "json_object"},
            max_tokens=8192
        )
        return json.loads(response.choices[0].message.content)
    except Exception as e:
        return {"error_mode_active": True, "details": str(e)}

# --- APP ROUTING: GATEWAY WALL ---
if not st.session_state["user_authenticated"]:
    st.markdown('<p class="main-title" style="text-align: center; margin-top: 8vh;">Study Sync</p>', unsafe_allow_html=True)
    st.markdown("<p style='text-align: center; color: #A0AEC0;'>AI-Powered Structural Curriculum Planner Gateway</p>", unsafe_allow_html=True)
    
    with st.container():
        t1, t2 = st.tabs(["🔐 Login Gateway", "📝 Register New Account"])
        with t1:
            e = st.text_input("Account Email:", key="l_em").strip()
            p = st.text_input("Password Hash Card:", type="password", key="l_pw").strip()
            if st.button("Access Application Core", use_container_width=True):
                status, u = verify_cloud_login(e, p)
                if status:
                    st.session_state["user_authenticated"] = True
                    st.session_state["active_username"] = u
                    data = load_schedule_from_firebase(u)
                    if data: st.session_state["ai_data"] = data
                    st.rerun()
                else:
                    st.error(f"Access Refused: {u}")
        with t2:
            un = st.text_input("Choose Username / Roll No:", key="s_un").strip()
            em = st.text_input("Email Workspace Target:", key="s_em").strip()
            pw = st.text_input("Secure Password Key String:", type="password", key="s_pw").strip()
            if st.button("Commit Profile Credentials", use_container_width=True):
                if len(pw) < 6: st.error("Password must be at least 6 characters.")
                else:
                    status, msg = register_cloud_user(em, pw, un)
                    st.success(msg) if status else st.error(msg)
    st.stop()

# --- MAIN DASHBOARD CONTROL INTERFACE ---
user_id = st.session_state["active_username"]
left_panel, right_panel = st.columns([1, 2], gap="large")

with left_panel:
    st.markdown('<p class="main-title">Study Sync</p>', unsafe_allow_html=True)
    
    st.subheader("🎨 Customization Theme")
    current_mode = st.toggle("🌓 Light Theme Active", value=(st.session_state["theme"] == "Light"))
    new_mode = "Light" if current_mode else "Dark"
    if new_mode != st.session_state["theme"]:
        st.session_state["theme"] = new_mode
        st.rerun()
        
    st.subheader("👤 Student Session Node")
    st.info(f"User Active: **{user_id}**")
    if st.button("🚪 Log Out Protocol", use_container_width=True):
        st.session_state["user_authenticated"] = False
        st.session_state["ai_data"] = None
        st.rerun()

    st.subheader("⚙️ Setup Filters")
    with st.container(border=True):
        study_hours = st.slider("Daily Study Velocity (Hours)", 1, 8, 3)
        intensity = st.select_slider("Target Focus Load", options=["Casual", "Balanced", "Intense"])
        skip_weekends = st.toggle("Exclude Weekends")
        
    with st.container(border=True):
        f_from = st.time_input("Free Window Start:", dt_time(17, 30))
        f_until = st.time_input("Free Window End:", dt_time(21, 30))
        s_from, s_until = f_from.strftime("%I:%M %p"), f_until.strftime("%I:%M %p")

with right_panel:
    st.subheader("📥 Target Curricular Ingestion")
    uploaded_file = st.file_uploader("Upload Course Syllabus Document (PDF)", type=["pdf"])

    if uploaded_file is not None:
        st.success(f"Attached target vector data stream: {uploaded_file.name}")
        
        if st.button("Generate Optimized Timeline", use_container_width=True):
            if ((f_until.hour * 60 + f_until.minute) - (f_from.hour * 60 + f_from.minute)) / 60 < study_hours:
                st.error("❌ Setup Limit Conflict: Available hours are narrower than daily targeted capacity slider.")
                st.stop()
                
            p_bar = st.progress(0)
            doc = fitz.open(stream=uploaded_file.read(), filetype="pdf")
            st.session_state["page_count"] = doc.page_count
            
            p_bar.progress(30)
            full_text = "".join([page.get_text() for page in doc])
            
            filtered_lines = []
            keywords = ["week", "unit", "chapter", "topic", "assignment", "exam", "quiz", "test", "project", "lab", "module", "semester", "subject"]
            for line in full_text.split("\n"):
                clean = line.strip()
                if any(k in clean.lower() for k in keywords) or (len(clean) > 12 and any(c.isdigit() for c in clean)):
                    filtered_lines.append(clean)
                    
            condensed = "\n".join(filtered_lines)[:14000]
            
            p_bar.progress(60)
            raw_ai = extract_syllabus_with_ai(condensed, study_hours, intensity, skip_weekends, s_from, s_until)
            
            # --- ✨ CRITICAL INTERCEPTOR FOR SILENT ERRORS ---
            if isinstance(raw_ai, dict) and "error_mode_active" in raw_ai:
                p_bar.empty()
                st.error(f"❌ Groq API Gateway Error: {raw_ai['details']}")
                st.stop()
                
            p_bar.progress(90)
            
            # Polymorphic Fallback Extraction Core
            raw_tasks = []
            if isinstance(raw_ai, dict):
                for k in ["tasks", "tasks_list", "t", "task"]:
                    if k in raw_ai and isinstance(raw_ai[k], list):
                        raw_tasks = raw_ai[k]
                        break
                        
            mapped_tasks = []
            for i in raw_tasks:
                if isinstance(i, dict):
                    mapped_tasks.append({
                        "task_name": i.get("task_name", i.get("n", i.get("task", "Milestone"))),
                        "due_date": i.get("due_date", i.get("d", "2026-06-15"))
                    })
                elif isinstance(i, str):
                    mapped_tasks.append({"task_name": i, "due_date": "2026-06-15"})
            
            raw_plan = []
            if isinstance(raw_ai, dict):
                for k in ["study_plan", "studyPlan", "plan", "s", "p"]:
                    if k in raw_ai and isinstance(raw_ai[k], list):
                        raw_plan = raw_ai[k]
                        break
                        
            mapped_plan = []
            for i in raw_plan:
                if isinstance(i, dict):
                    mapped_plan.append({
                        "Status": False,
                        "Scheduled Date": i.get("scheduled_date", i.get("d", i.get("date", "2026-06-15"))),
                        "Time Slot": i.get("time_slot", i.get("t", f"{s_from} - {s_until}")),
                        "Focus Topic": i.get("focus_topic", i.get("f", "Concept Review")),
                        "Suggested Action": i.get("suggested_action", i.get("a", "Practice items")),
                        "Hours Allocated": int(i.get("hours_allocated", i.get("h", study_hours)))
                    })
            
            st.session_state["ai_data"] = {"tasks": mapped_tasks, "study_plan": mapped_plan}
            save_schedule_to_firebase(user_id, mapped_tasks, mapped_plan)
            p_bar.empty()
            st.rerun()

    if st.session_state["ai_data"] is not None:
        st.markdown(f"""
        <div class="success-banner">
            <div style="font-weight:bold; margin-right:15px; color:#00C6FF;">✓</div>
            <div><strong>Cloud Sync Node Operational:</strong> Roadmap matrix loaded under encrypted handle profile nodes.</div>
        </div>
        """, unsafe_allow_html=True)
        
        t_tasks = len(st.session_state["ai_data"]["tasks"])
        t_rows = len(st.session_state["ai_data"]["study_plan"])
        
        c1, c2, c3 = st.columns(3)
        with c1: st.metric("Ingested Pages", f"{st.session_state['page_count']} Pages")
        with c2: st.metric("Roadmap Cells", f"{t_rows} Core Items")
        with c3: st.metric("Active Terminal Account", user_id)
        
        st.markdown("<br>", unsafe_allow_html=True)
        col_t1, col_t2 = st.columns([1, 2], gap="medium")
        
        with col_t1:
            st.subheader("📅 Deadlines Outlines")
            st.dataframe(st.session_state["ai_data"]["tasks"], use_container_width=True)
            
        with col_t2:
            st.subheader("🔄 Interactive Scheduling Canvas")
            df = pd.DataFrame(st.session_state["ai_data"]["study_plan"])
            
            edited_df = st.data_editor(
                df, use_container_width=True, hide_index=True, key="roadmap_grid",
                disabled=["Scheduled Date", "Time Slot", "Focus Topic", "Suggested Action", "Hours Allocated"]
            )
            
            if not edited_df.equals(df):
                st.session_state["ai_data"]["study_plan"] = edited_df.to_dict(orient="records")
                save_schedule_to_firebase(user_id, st.session_state["ai_data"]["tasks"], st.session_state["ai_data"]["study_plan"])
                st.rerun()