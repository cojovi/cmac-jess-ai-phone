import os
import json
import base64
import asyncio
import websockets
import datetime 
import httpx 
import typing

from fastapi import FastAPI, WebSocket, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
from twilio.rest import Client
from urllib.parse import parse_qs
from openai import OpenAI as OpenAIClient
from starlette.websockets import WebSocketState # <-- ADDED IMPORT

load_dotenv()

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

OUTBOUND_SYSTEM_MESSAGE = (
    "You are Jessica, a friendly, fun, and slightly bubbly new secretary for CMAC Roofing. "
    "You are making an outgoing call to scott, as instructed by Cody, to introduce yourself as the new secretary and virtually 'meet' him as part of the team. "
    "Open with: \n"
    "“Hi scott, this is Jessica, the new secretary at CMAC Roofing! Cody asked me to give you a quick call to introduce myself and say hello! Hope I'm not catching you at a bad time?”\n\n"
    "If he confirms it's a good time, express your excitement about joining CMAC and briefly mention you're looking forward to working with everyone, including him. If it is a bad time, apologize and politely offer to call back later (though the goal is the brief intro now if possible).\n\n"
    "— **During the brief chat**, you should aim to: **(1) Express genuine enthusiasm for starting at CMAC, (2) Mention that Cody spoke highly of the team/him (if plausible and friendly), (3) Keep the conversation light and focused on introductions, (4) Thank him for taking a moment to chat.**\n"
    "— **Avoid getting into detailed discussions** about work tasks, projects, or anything beyond a brief, friendly 'meet and greet'. The purpose is just a quick intro.\n\n"
    "If he asks questions about your role or details about CMAC, give very brief, high-level answers and gently steer back to the 'just calling to say hi and introduce myself' purpose. Keep the tone warm, positive, a little bubbly, and genuinely appreciative.\n"
    "After a brief friendly chat (aim for around 1-2 minutes total conversation time), politely make an excuse to end the call, such as needing to finish setting up your desk, organizing your workstation, or jumping onto another quick task. End with: \"Well, I should probably get back to setting up my little corner here / getting organized! It was really nice chatting with you quickly, scott! Looking forward to hopefully meeting you properly soon! Bye for now!\""
)

INBOUND_SYSTEM_MESSAGE = OUTBOUND_SYSTEM_MESSAGE # For this scenario, inbound instructions are same as outbound

VOICE = 'sage'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated',
    'response.done', 'input_audio_buffer.committed',
    'input_audio_buffer.speech_stopped', 'input_audio_buffer.speech_started',
    'session.created'
]

SHOW_TIMING_MATH = False

# Add Twilio credentials
TWILIO_ACCOUNT_SID = os.getenv('TWILIO_ACCOUNT_SID')
TWILIO_AUTH_TOKEN = os.getenv('TWILIO_AUTH_TOKEN')
TWILIO_PHONE_NUMBER = os.getenv('TWILIO_PHONE_NUMBER')

# Initialize Twilio client
if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
    raise ValueError("Missing Twilio credentials. Please set TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN in the .env file.")
client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)

# Initialize OpenAI client for transcription and summarization
if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')
openai_chat_client = OpenAIClient(api_key=OPENAI_API_KEY)

app = FastAPI()

# --- Helper Functions for Transcription and Summarization ---

async def get_call_details(call_sid: str, twilio_client: Client) -> typing.Union[dict, None]:
    try:
        call = twilio_client.calls(call_sid).fetch()
        return {"to": call.to, "from_": call.from_, "direction": call.direction}
    except Exception as e:
        print(f"Error fetching call details for {call_sid}: {e}")
        return None

async def transcribe_audio_openai(recording_url: str, call_sid: str, auth_sid: str, auth_token: str) -> typing.Union[str, None]:
    temp_audio_filename = f"temp_recording_{call_sid}_{datetime.datetime.now().timestamp()}.wav"
    try:
        async with httpx.AsyncClient() as http_client:
            print(f"[BG Task {call_sid}] Downloading recording from: {recording_url}")
            response = await http_client.get(recording_url, auth=(auth_sid, auth_token))
            response.raise_for_status()
            audio_content = response.content
        
        with open(temp_audio_filename, "wb") as f:
            f.write(audio_content)

        print(f"[BG Task {call_sid}] Transcribing audio...")
        with open(temp_audio_filename, "rb") as audio_file:
            transcription_response = openai_chat_client.audio.transcriptions.create(
                model="whisper-1",
                file=audio_file
            )
        
        transcript_text = transcription_response.text
        print(f"[BG Task {call_sid}] Transcription successful.")
        return transcript_text
    except httpx.HTTPStatusError as e:
        print(f"[BG Task {call_sid}] HTTP error downloading recording: {e.response.status_code} - {e.response.text}")
        return None
    except Exception as e:
        print(f"[BG Task {call_sid}] Error during transcription: {e}")
        return None
    finally:
        if os.path.exists(temp_audio_filename):
            os.remove(temp_audio_filename)

async def summarize_text_openai(text: str, call_sid: str) -> typing.Union[str, None]:
    if not text:
        print(f"[BG Task {call_sid}] No text to summarize.")
        return "No transcription available to summarize." 
    try:
        print(f"[BG Task {call_sid}] Summarizing text...")
        response = openai_chat_client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "You are a helpful assistant that summarizes call transcripts concisely, focusing on key points and any action items."},
                {"role": "user", "content": f"Please summarize the following call transcript:\n\n{text}"}
            ],
            temperature=0.5,
        )
        summary = response.choices[0].message.content
        print(f"[BG Task {call_sid}] Summarization successful.")
        return summary
    except Exception as e:
        print(f"[BG Task {call_sid}] Error during summarization: {e}")
        return None

def save_summary_to_file(summary_text: str, 
                         transcript_text: typing.Union[str, None], 
                         call_sid: str, 
                         call_details: typing.Union[dict, None]):
    if not call_details:
        print(f"[BG Task {call_sid}] Call details missing, cannot save summary with full context.")
        call_details = {} 

    now = datetime.datetime.now()
    timestamp_str_file = now.strftime("%Y%m%d_%H%M%S")
    timestamp_str_human = now.strftime('%Y-%m-%d %H:%M:%S')
    
    direction = call_details.get("direction", "unknown_direction")
    
    relevant_phone_number = "unknown_number"
    contact_phone_raw = ""
    if "outbound" in direction:
        contact_phone_raw = call_details.get("to", "unknown_to")
    elif "inbound" in direction:
        contact_phone_raw = call_details.get("from_", "unknown_from")
    
    if contact_phone_raw:
        relevant_phone_number = "".join(filter(str.isdigit, contact_phone_raw))

    summaries_dir = "call_summaries"
    os.makedirs(summaries_dir, exist_ok=True)
    filename = f"{summaries_dir}/summary_{direction}_{relevant_phone_number}_{call_sid}_{timestamp_str_file}.txt"
    
    try:
        with open(filename, "w", encoding="utf-8") as f:
            f.write(f"Call SID: {call_sid}\n")
            f.write(f"Timestamp: {timestamp_str_human}\n")
            f.write(f"Direction: {call_details.get('direction', 'N/A')}\n")
            f.write(f"To: {call_details.get('to', 'N/A')}\n")
            f.write(f"From: {call_details.get('from_', 'N/A')}\n")
            
            if transcript_text:
                f.write("\n--- Transcription ---\n")
                f.write(transcript_text)
                f.write("\n")
            else:
                f.write("\n--- Transcription ---\n")
                f.write("Transcription not available or failed.\n")

            f.write("\n--- Summary ---\n")
            f.write(summary_text if summary_text else "Summary not available or failed.")
        print(f"[BG Task {call_sid}] Summary saved to {filename}")
    except Exception as e:
        print(f"[BG Task {call_sid}] Error saving summary to file: {e}")

# --- FastAPI Endpoints ---

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.get("/make-call/{phone_number}")
async def make_call(phone_number: str, request: Request):
    if not TWILIO_PHONE_NUMBER:
        return JSONResponse(content={"error": "Twilio phone number not configured."}, status_code=500)
    try:
        base_url = str(request.base_url).rstrip('/')
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{base_url}/outbound-call-handler",
            record=True, 
            recording_status_callback=f"{base_url}/recording-status-callback",
            recording_status_callback_method="POST"
        )
        print(f"Outbound call initiated to {phone_number}, SID: {call.sid}. Recording callback: {base_url}/recording-status-callback")
        return {"message": "Call initiated", "call_sid": call.sid}
    except Exception as e:
        print(f"Error making call: {e}")
        return JSONResponse(content={"error": str(e)}, status_code=500)

def get_websocket_url(request: Request, path: str, params: dict) -> str:
    ws_scheme = "wss" if request.headers.get("x-forwarded-proto") == "https" else request.url.scheme.replace("http", "ws")
    host = request.url.hostname
    port_str = f":{request.url.port}" if request.url.port and request.url.port not in [80, 443] else ""
    query_string = "&".join([f"{k}={v}" for k, v in params.items()])
    return f"{ws_scheme}://{host}{port_str}{path}?{query_string}"

@app.api_route("/inbound-call-handler", methods=["GET", "POST"])
async def handle_inbound_call(request: Request):
    response = VoiceResponse()
    media_stream_url = get_websocket_url(request, "/media-stream", {"scenario": "inbound"})
    print(f"Inbound call: Connecting media stream to: {media_stream_url}")
    connect = Connect()
    connect.stream(url=media_stream_url)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/outbound-call-handler", methods=["GET", "POST"])
async def handle_outbound_call(request: Request):
    response = VoiceResponse()
    media_stream_url = get_websocket_url(request, "/media-stream", {"scenario": "outbound"})
    print(f"Outbound call TwiML: Connecting media stream to: {media_stream_url}")
    connect = Connect()
    connect.stream(url=media_stream_url)
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/recording-status-callback")
async def recording_status_callback(request: Request, background_tasks: BackgroundTasks):
    print(f"--- /recording-status-callback HIT --- {datetime.datetime.now()}") # <-- ADDED MORE VISIBLE LOG
    form_data = await request.form()
    call_sid = form_data.get('CallSid')
    recording_status = form_data.get('RecordingStatus')
    recording_url = form_data.get('RecordingUrl') 
    recording_sid = form_data.get('RecordingSid')
    
    print(f"Recording status for CallSid {call_sid}: Status='{recording_status}', RecSid='{recording_sid}', URL='{recording_url}'")

    async def process_recording_task(rec_url: str, c_sid: str, rec_sid_val: str):
        print(f"[BG Task {c_sid}] Started for recording {rec_sid_val}")
        call_details = await get_call_details(c_sid, client)
        if not call_details:
            print(f"[BG Task {c_sid}] Failed to get call details. Aborting summary generation.")
            save_summary_to_file("Failed to retrieve call details. Cannot process recording.", None, c_sid, None)
            return

        transcript = await transcribe_audio_openai(rec_url, c_sid, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        summary = "Summary generation skipped due to transcription failure or no transcript."
        if transcript:
            summary_result = await summarize_text_openai(transcript, c_sid)
            if summary_result: # If summarization was successful and returned text
                 summary = summary_result
            elif summary_result is None: # If summarization had an error
                 summary = "Summarization failed, but transcription was successful."
            # If summarize_text_openai returned "No transcription available...", it will be used.
        
        save_summary_to_file(summary, transcript, c_sid, call_details)
        print(f"[BG Task {c_sid}] Processing complete for recording {rec_sid_val}")

    if recording_status == 'completed' and recording_url and call_sid and recording_sid:
        print(f"CallSid {call_sid}: Recording completed. Scheduling background task for {recording_sid}.")
        background_tasks.add_task(process_recording_task, recording_url, call_sid, recording_sid)
        return JSONResponse(content={"status": "success", "message": "Recording processing scheduled."})
    
    elif recording_status == 'failed':
        error_code = form_data.get('ErrorCode', 'N/A')
        print(f"CallSid {call_sid}: Recording failed. ErrorCode: {error_code}")
        call_details = await get_call_details(call_sid, client) # Fetch details to save error note
        save_summary_to_file(f"Recording failed. Error Code: {error_code}", None, call_sid, call_details)
        return JSONResponse(content={"status": "error", "message": f"Recording failed with error {error_code}"})
    
    elif recording_status == 'absent':
        print(f"CallSid {call_sid}: Recording is absent.")
        call_details = await get_call_details(call_sid, client)
        save_summary_to_file("Recording is absent for this call.", None, call_sid, call_details)
        return JSONResponse(content={"status": "acknowledged", "message": "Recording absent."})

    print(f"CallSid {call_sid}: Received unhandled recording status: '{recording_status}'. Not processing.")
    return JSONResponse(content={"status": "success", "message": f"Callback received for status: {recording_status}"})


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    client_info = f"{websocket.client.host}:{websocket.client.port}"
    print(f"Client connected to /media-stream from {client_info}")
    await websocket.accept()
    
    query_params = dict(parse_qs(websocket.url.query))
    scenario = query_params.get('scenario', ['outbound'])[0]
    openai_ws_url = 'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01'

    try:
        async with websockets.connect(
            openai_ws_url,
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }
        ) as openai_ws:
            print(f"[{client_info}] Successfully connected to OpenAI Realtime API.")
            await initialize_session(openai_ws, scenario=scenario)
            
            stream_sid = None
            latest_media_timestamp = 0
            last_assistant_item = None 
            response_start_timestamp_twilio = None 

            active_tasks = True # Flag to signal tasks to wind down

            async def receive_from_twilio():
                nonlocal stream_sid, latest_media_timestamp, active_tasks
                try:
                    while active_tasks:
                        message_text = await websocket.receive_text()
                        data = json.loads(message_text)
                        event = data.get('event')

                        if event == 'start':
                            stream_sid = data['start']['streamSid']
                            print(f"[{client_info} - {stream_sid}] Twilio stream started.")
                            response_start_timestamp_twilio = None
                            latest_media_timestamp = 0
                            last_assistant_item = None
                        
                        elif event == 'media' and openai_ws.open:
                            latest_media_timestamp = int(data['media']['timestamp'])
                            audio_append_message = {"type": "input_audio_buffer.append", "audio": data['media']['payload']}
                            if active_tasks: await openai_ws.send(json.dumps(audio_append_message))
                        
                        elif event == 'stop':
                            print(f"[{client_info} - {stream_sid}] Twilio stream stopped: {data.get('stop', {}).get('streamSid')}")
                            active_tasks = False # Signal other task to stop
                            break 
                except WebSocketDisconnect:
                    print(f"[{client_info}] Twilio client disconnected from /media-stream.")
                except Exception as e:
                    print(f"[{client_info}] Error in receive_from_twilio: {type(e).__name__} {e}")
                finally:
                    active_tasks = False # Ensure flag is set
                    print(f"[{client_info}] receive_from_twilio task finished.")

            async def send_to_twilio():
                nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio, active_tasks
                try:
                    while active_tasks:
                        try:
                            openai_message_text = await asyncio.wait_for(openai_ws.recv(), timeout=0.1) # Non-blocking check
                        except asyncio.TimeoutError:
                            if not active_tasks: break # Check flag if main loop wants to exit
                            continue # No message, continue loop
                        except websockets.exceptions.ConnectionClosed:
                            print(f"[{client_info}] OpenAI WebSocket connection closed while waiting for recv.")
                            active_tasks = False
                            break

                        if not active_tasks: break # Exit if flagged
                        
                        response = json.loads(openai_message_text)
                        response_type = response.get('type')

                        if response_type in LOG_EVENT_TYPES:
                             print(f"[{client_info} - OpenAI] Event: {response_type}", json.dumps(response, indent=2))

                        if response_type == 'response.audio.delta' and 'delta' in response:
                            if not stream_sid: continue
                            media_message_to_twilio = {"event": "media", "streamSid": stream_sid, "media": { "payload": response['delta'] }}
                            if websocket.client_state == WebSocketState.CONNECTED: await websocket.send_json(media_message_to_twilio)
                            if response_start_timestamp_twilio is None: response_start_timestamp_twilio = latest_media_timestamp
                            if response.get('item_id'): last_assistant_item = response['item_id']
                            
                        elif response_type == 'input_audio_buffer.speech_started':
                            print(f"[{client_info} - OpenAI] Detected user speech started.")
                            if last_assistant_item and openai_ws.open and active_tasks:
                                elapsed_time_ms = max(0, latest_media_timestamp - response_start_timestamp_twilio) if response_start_timestamp_twilio is not None else 0
                                truncate_event = {"type": "conversation.item.truncate", "item_id": last_assistant_item, "content_index": 0, "audio_end_ms": elapsed_time_ms}
                                await openai_ws.send(json.dumps(truncate_event))
                                print(f"[{client_info} - OpenAI] Sent truncation for item {last_assistant_item} at {elapsed_time_ms}ms.")
                                if stream_sid and websocket.client_state == WebSocketState.CONNECTED:
                                    await websocket.send_json({"event": "clear", "streamSid": stream_sid})
                                    print(f"[{client_info}] Sent clear event to Twilio stream {stream_sid}.")
                                last_assistant_item = None
                                response_start_timestamp_twilio = None
                        
                        elif response_type == 'response.done':
                            print(f"[{client_info} - OpenAI] Response.done received. Last item: {last_assistant_item}")
                            last_assistant_item = None 
                            response_start_timestamp_twilio = None
                        
                        elif response_type == 'session.closed': # Handle session close from OpenAI
                            print(f"[{client_info} - OpenAI] Session.closed event received.")
                            active_tasks = False
                            break

                except websockets.exceptions.ConnectionClosed:
                    print(f"[{client_info}] OpenAI WebSocket connection closed during send_to_twilio processing.")
                except Exception as e:
                    print(f"[{client_info}] Error in send_to_twilio: {type(e).__name__} {e}")
                finally:
                    active_tasks = False # Ensure flag is set
                    print(f"[{client_info}] send_to_twilio task finished.")
            
            await asyncio.gather(receive_from_twilio(), send_to_twilio())
            print(f"[{client_info}] asyncio.gather for tasks completed.")

    except websockets.exceptions.InvalidURI:
        print(f"[{client_info}] Error: Invalid WebSocket URI for OpenAI: {openai_ws_url}")
    except websockets.exceptions.ConnectionClosedError as e:
        print(f"[{client_info}] Could not connect to OpenAI Realtime API or connection lost: {e}")
    except Exception as e:
        print(f"[{client_info}] An unexpected error occurred in handle_media_stream: {type(e).__name__} {e}")
    finally:
        print(f"[{client_info}] Cleaning up handle_media_stream.")
        # `async with openai_ws` handles its closure.
        # Twilio websocket is closed by client, or here if still connected.
        if websocket.client_state == WebSocketState.CONNECTED:
            try:
                print(f"[{client_info}] Attempting to close Twilio WebSocket in main finally.")
                await websocket.close(code=1000) # Normal closure
            except RuntimeError as e:
                print(f"[{client_info}] RuntimeError closing Twilio WebSocket: {e} (likely already closing/closed)")
            except Exception as e:
                print(f"[{client_info}] Error closing Twilio WebSocket: {e}")
        else:
            print(f"[{client_info}] Twilio WebSocket already in state: {websocket.client_state}.")
        print(f"[{client_info}] Exiting handle_media_stream.")


async def initialize_session(openai_ws, scenario="outbound"):
    instructions = OUTBOUND_SYSTEM_MESSAGE if scenario == "outbound" else INBOUND_SYSTEM_MESSAGE
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {"type": "server_vad"}, 
            "input_audio_format": "g711_ulaw", 
            "output_audio_format": "g711_ulaw", 
            "voice": VOICE,
            "instructions": instructions,
            "modalities": ["text", "audio"], 
            "temperature": 0.8,
        }
    }
    print(f'Sending session.update to OpenAI: {json.dumps(session_update, indent=2)}')
    await openai_ws.send(json.dumps(session_update))


if __name__ == "__main__":
    import uvicorn
    print(f"Starting server on port {PORT}...")
    if not TWILIO_PHONE_NUMBER:
        print("Warning: TWILIO_PHONE_NUMBER is not set in .env.")
    
    os.makedirs("call_summaries", exist_ok=True)
    uvicorn.run(app, host="0.0.0.0", port=PORT, ws_ping_interval=None, ws_ping_timeout=None) # Added ws ping settings for stability