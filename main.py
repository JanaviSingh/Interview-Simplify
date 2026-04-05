import os
import asyncio
import base64
from datetime import datetime
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, WebSocket, WebSocketDisconnect, BackgroundTasks
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

# Custom modules
from parsers import extract_text
from ai_engine import parse_jd, parse_resume, generate_question_pool, evaluate_candidate_answer, generate_interview_report
from database import jobs_collection, candidates_collection, interviews_collection
from audio_utils import synthesize_speech
from stt_utils import StreamingAudioProcessor
from email_automation import send_interview_email

# ==========================================
# APP INITIALIZATION & CONFIG
# ==========================================
app = FastAPI(title="Hiringhood AI Interview Platform")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Use absolute paths to prevent static 404 errors (like webrtc.js missing)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "frontend", "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


# ==========================================
# HTML VIEW ROUTES & REDIRECTS
# ==========================================

# Helper function to serve HTML files cleanly
def serve_html(folder: str, filename: str) -> HTMLResponse:
    file_path = os.path.join(BASE_DIR, folder, filename)
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            return HTMLResponse(content=file.read())
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail=f"File {filename} not found.")

@app.get("/", include_in_schema=False)
async def root_redirect():
    """Auto-redirects the base URL straight to the recruiter campaign builder."""
    return RedirectResponse(url="/admin/create-job")

@app.get("/admin/login", response_class=HTMLResponse)
async def view_admin_login(): return serve_html("recruiter", "login.html")

@app.get("/admin/dashboard", response_class=HTMLResponse)
async def view_admin_dashboard(): return serve_html("recruiter", "dashboard.html")

@app.get("/admin/create-job", response_class=HTMLResponse)
async def view_admin_create_job(): return serve_html("recruiter", "create-job.html")

@app.get("/admin/report/{session_id}", response_class=HTMLResponse)
async def view_admin_report(session_id: str): return serve_html("recruiter", "report.html")

@app.get("/interview/{session_id}", response_class=HTMLResponse)
async def view_candidate_interview(session_id: str):
    file_path = os.path.join(BASE_DIR, "candidate", "candidate-app.html")
    try:
        with open(file_path, "r", encoding="utf-8") as file:
            html_content = file.read().replace("SESSION_ID_PLACEHOLDER", session_id)
        return HTMLResponse(content=html_content)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Candidate app file not found.")


# ==========================================
# CORE API ENDPOINTS
# ==========================================

@app.post("/prepare-interview/")
async def prepare_interview_session(
    background_tasks: BackgroundTasks,
    js_id: str = Form(...),
    contest_id: str = Form(...),
    recruiter_id: str = Form(...),
    resume_file: UploadFile = File(...),
    jd_file: UploadFile = File(...)
):
    try:
        resume_bytes, jd_bytes = await resume_file.read(), await jd_file.read()
        resume_text = await extract_text(resume_bytes, resume_file.filename)
        jd_text = await extract_text(jd_bytes, jd_file.filename)
        
        parsed_jd = parse_jd(jd_text, contest_id)
        jobs_collection.update_one({"_id": contest_id}, {"$set": {"recruiter_id": recruiter_id, "jdContent": parsed_jd, "raw_jd_text": jd_text}}, upsert=True)
        
        parsed_resume = parse_resume(resume_text, js_id)
        candidates_collection.update_one({"_id": js_id}, {"$set": {"profile": parsed_resume, "raw_resume_text": resume_text}}, upsert=True)

        generated_questions = generate_question_pool(parsed_jd, parsed_resume)
        session_id = f"{js_id}_{contest_id}"
        interview_link = f"http://localhost:8000/interview/{session_id}"

        candidate_name = parsed_resume.get("candidate_profile", {}).get("name", "Candidate")
        candidate_email = parsed_resume.get("candidate_profile", {}).get("email", "")

        interviews_collection.update_one(
            {"sessionId": session_id},
            {"$set": {
                "job_id": contest_id, "candidate_id": js_id, "candidate_name": candidate_name, "candidate_email": candidate_email,
                "generatedQuestions": generated_questions, "asked_question_ids": [], "transcript": [], "answers": [], "scores": [], "status": "pending", "email_sent": False
            }}, upsert=True
        )

        if candidate_email and "@" in candidate_email:
            background_tasks.add_task(
                send_interview_email, candidate_name=candidate_name, candidate_email=candidate_email,
                interview_link=interview_link, session_id=session_id, db_collection=interviews_collection
            )

        return {
            "status": "success",
            "message": "Interview prepared successfully.",
            "data": {"session_id": session_id, "interview_link": interview_link, "candidate_email": candidate_email, "total_questions_generated": len(generated_questions)}
        }
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/api/dashboard")
async def get_dashboard_data():
    try:
        interviews = list(interviews_collection.find({}, {"_id": 0}))
        dashboard_data = []
        now = datetime.now()

        for inv in interviews:
            status = inv.get("status", "pending")
            expires_at_str = inv.get("expires_at")

            if status == "pending" and expires_at_str:
                try:
                    if now > datetime.fromisoformat(expires_at_str):
                        status = "expired"
                        interviews_collection.update_one({"sessionId": inv["sessionId"]}, {"$set": {"status": "expired"}})
                except ValueError: pass 

            dashboard_data.append({
                "session_id": inv.get("sessionId"), "candidate_name": inv.get("candidate_name", "Unknown"), "candidate_email": inv.get("candidate_email", "N/A"),
                "email_sent": inv.get("email_sent", False), "status": status, "expires_at": expires_at_str, "email_sent_at": inv.get("email_sent_at")
            })
        return {"status": "success", "interviews": dashboard_data}
    except Exception as e:
        raise HTTPException(status_code=500, detail="Internal Server Error")

@app.get("/api/interview-session/{session_id}")
async def get_interview_session(session_id: str):
    try:
        doc = interviews_collection.find_one({"sessionId": session_id})
        if not doc: raise HTTPException(status_code=404, detail="Session not found.")
        
        doc.pop('_id', None)
        
        # Generate Final Report if completed but missing
        if doc.get("status") == "completed" and "final_report" not in doc:
            jd_context = jobs_collection.find_one({"_id": doc.get("job_id")}).get("jdContent", {})
            candidate_profile = candidates_collection.find_one({"_id": doc.get("candidate_id")}).get("profile", {})
            
            raw_transcript, answers_data = doc.get("transcript", []), doc.get("answers", [])
            qa_pairs, scores, current_q, answer_index = [], [], None, 0
            
            for entry in raw_transcript:
                if entry.get("speaker") == "interviewer": current_q = entry.get("text")
                elif entry.get("speaker") == "candidate" and current_q:
                    score = answers_data[answer_index].get("score", 0) if answer_index < len(answers_data) else 0
                    q_origin = "Dynamic Follow-up" if answer_index < len(answers_data) and str(answers_data[answer_index].get("question_id", "")).startswith("dyn_") else "Pre-planned"
                    
                    scores.append(score)
                    qa_pairs.append({"question": current_q, "answer": entry.get("text"), "score": score, "origin": q_origin})
                    answer_index += 1
                    current_q = None 
                
            avg_score = round(sum(scores) / len(scores), 2) if scores else 0.0
            ai_analysis = generate_interview_report(jd_context, candidate_profile, qa_pairs, avg_score)
            
            final_report = {
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "interviewer": "Tara (Senior Technical Interviewer)",
                "interview_statistics": {"total_questions": len(qa_pairs), "overall_score": avg_score},
                "ai_analysis": ai_analysis, "detailed_qa": qa_pairs 
            }
            interviews_collection.update_one({"sessionId": session_id}, {"$set": {"final_report": final_report}})
            doc["final_report"] = final_report

        return {"status": "success", "data": doc}
    except HTTPException: raise
    except Exception as e: raise HTTPException(status_code=500, detail=str(e))

# ==========================================
# WEBSOCKET (LIVE AUDIO STREAMING)
# ==========================================
active_connections = {}
MAX_QUESTIONS = 10

@app.websocket("/ws/interview/{session_id}")
async def interview_websocket(websocket: WebSocket, session_id: str):
    await websocket.accept()
    active_connections[session_id] = websocket
    loop = asyncio.get_running_loop()
    
    interview_doc = interviews_collection.find_one({"sessionId": session_id})
    if not interview_doc:
        await websocket.send_json({"type": "error", "message": "Session not found."})
        return await websocket.close()

    pool = interview_doc.get("generatedQuestions", [])
    asked_ids = interview_doc.get("asked_question_ids", [])
    current_question = pool[0] if not asked_ids else next((q for q in pool if q.get("id") == asked_ids[-1]), pool[0])
    
    processor, predictive_task, last_interim_length = None, None, 0

    async def on_interim(text: str): await websocket.send_json({"type": "interim_transcript", "text": text})

    async def process_final_answer(user_answer: str):
        nonlocal current_question, predictive_task, last_interim_length
        if not user_answer:
            return await websocket.send_json({"type": "info", "message": "I didn't hear anything clearly. Could you try answering again?"})
            
        await websocket.send_json({"type": "transcript_success", "text": "Answer logged securely. Evaluating..."})
        current_doc = interviews_collection.find_one({"sessionId": session_id})
        current_asked_ids = current_doc.get("asked_question_ids", [])
        
        # FAST REPEAT INTERCEPT
        is_repeat = any(p in user_answer.lower() for p in ["repeat", "pardon", "say that again", "didn't catch"]) and len(user_answer.split()) < 15
        if is_repeat:
            repeat_msg = f"Sure, I can repeat that for you. {current_question['question_text']}"
            asyncio.create_task(asyncio.to_thread(interviews_collection.update_one, {"sessionId": session_id}, {"$push": {"transcript": {"$each": [{"speaker": "candidate", "text": user_answer, "timestamp": datetime.utcnow().isoformat()}, {"speaker": "interviewer", "text": repeat_msg, "timestamp": datetime.utcnow().isoformat()}]}}}))
            await websocket.send_json({"type": "ai_question", "text": repeat_msg, "current_q_num": len(current_asked_ids), "total_q_num": MAX_QUESTIONS, "audio_base64": await asyncio.to_thread(synthesize_speech, repeat_msg)})
            return

        pool_ids_asked = [i for i in current_asked_ids if not str(i).startswith("dyn_")]
        available_questions = [q for q in pool if q.get("id") not in pool_ids_asked]
        recent_context = "\n".join([f"{msg['speaker'].upper()}: {msg['text']}" for msg in current_doc.get("transcript", [])[-4:]])

        evaluation = await evaluate_candidate_answer(current_question, user_answer, available_questions, recent_context)
        score_val = int(evaluation.get("score", 5))

        asyncio.create_task(asyncio.to_thread(interviews_collection.update_one, {"sessionId": session_id}, {"$push": {"answers": {"question_id": current_question.get("id"), "text": user_answer, "score": score_val}, "transcript": {"speaker": "candidate", "text": user_answer, "timestamp": datetime.utcnow().isoformat()}}}))

        if len(current_asked_ids) >= MAX_QUESTIONS or not available_questions:
            closing_msg = "Thank you for your time. Your answers were insightful. Have a great day!"
            await asyncio.to_thread(interviews_collection.update_one, {"sessionId": session_id}, {"$set": {"status": "completed"}})
            return await websocket.send_json({"type": "interview_complete", "text": closing_msg, "audio_base64": await asyncio.to_thread(synthesize_speech, closing_msg)})

        next_q_id = evaluation.get("next_question_id", "follow_up")
        if next_q_id == "follow_up":
            actual_id, full_spoken_text = f"dyn_followup_{len(current_asked_ids)}", f"{evaluation.get('transition', 'I see.')} Could you elaborate a bit more on your role in that specific process?"
            ideal_rubric = []
        else:
            actual_id = next_q_id
            orig = next((q for q in pool if q.get("id") == next_q_id), available_questions[0])
            full_spoken_text, ideal_rubric = f"{evaluation.get('transition', 'Got it.')} {orig.get('question_text')}", orig.get("ideal_answer_rubric", [])

        current_question = {"id": actual_id, "question_text": full_spoken_text, "ideal_answer_rubric": ideal_rubric}
        asyncio.create_task(asyncio.to_thread(interviews_collection.update_one, {"sessionId": session_id}, {"$push": {"asked_question_ids": actual_id, "transcript": {"speaker": "interviewer", "text": current_question["question_text"]}}}))
        await websocket.send_json({"type": "ai_question", "text": current_question["question_text"], "current_q_num": len(current_asked_ids) + 1, "total_q_num": MAX_QUESTIONS, "audio_base64": await asyncio.to_thread(synthesize_speech, current_question["question_text"])})
        
    if not asked_ids:
        await asyncio.to_thread(interviews_collection.update_one, {"sessionId": session_id}, {"$push": {"asked_question_ids": current_question["id"], "transcript": {"speaker": "interviewer", "text": current_question["question_text"], "timestamp": datetime.utcnow().isoformat()}}})
        await websocket.send_json({"type": "ai_question", "text": current_question["question_text"], "current_q_num": 1, "total_q_num": MAX_QUESTIONS, "audio_base64": await asyncio.to_thread(synthesize_speech, current_question["question_text"])})
        
    try:
        while True:
            data = await websocket.receive_json()
            if data["type"] == "start_recording":
                if processor: processor.stop_and_submit() 
                processor = StreamingAudioProcessor(session_id, loop, on_interim, process_final_answer)
                processor.start()
            elif data["type"] == "audio_chunk" and processor: processor.add_audio(base64.b64decode(data["audio"]))
            elif data["type"] == "stop_recording" and processor:
                processor.stop_and_submit()
                processor = None
    except WebSocketDisconnect:
        if processor: processor.stop_and_submit()
        active_connections.pop(session_id, None)