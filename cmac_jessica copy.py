import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect
from dotenv import load_dotenv
from twilio.rest import Client
from urllib.parse import parse_qs

load_dotenv()

#!/usr/bin/env python3

import os

# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

# Example details from the online form submission. In a real application, these 
# would be dynamically populated from the user's submitted data.

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

INBOUND_SYSTEM_MESSAGE = OUTBOUND_SYSTEM_MESSAGE

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

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError('Missing the OpenAI API key. Please set it in the .env file.')

@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}

@app.get("/make-call/{phone_number}")
async def make_call(phone_number: str, request: Request):
    """Initiate an outbound call to the specified phone number."""
    try:
        # Create the call with recording enabled
        call = client.calls.create(
            to=phone_number,
            from_=TWILIO_PHONE_NUMBER,
            url=f"{request.base_url}outbound-call-handler",
            record=True,
            recording_status_callback=f"{request.base_url}recording-status-callback"
        )
        return {"message": "Call initiated", "call_sid": call.sid}
    except Exception as e:
        return {"error": str(e)}

@app.api_route("/inbound-call-handler", methods=["GET", "POST"])
async def handle_inbound_call(request: Request):
    """Handle the inbound call setup and return TwiML."""
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream?scenario=inbound')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.api_route("/outbound-call-handler", methods=["GET", "POST"])
async def handle_outbound_call(request: Request):
    """Handle the outbound call setup and return TwiML."""
    response = VoiceResponse()
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream?scenario=outbound')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")

@app.post("/recording-status-callback")
async def recording_status_callback(request: Request):
    """Handle recording status events."""
    form_data = await request.form()
    recording_status = form_data.get('RecordingStatus')
    recording_url = form_data.get('RecordingUrl')

    # Process the recording status event here, e.g., save to database, log information, etc.
    print(f"Recording status: {recording_status}, Recording URL: {recording_url}")

    return {"status": "success"}

@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()
    
    # Extract scenario from query parameters
    query_params = dict(parse_qs(websocket.url.query))
    scenario = query_params.get('scenario', ['outbound'])[0]

    async with websockets.connect(
        'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview-2024-10-01',
        extra_headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "OpenAI-Beta": "realtime=v1"
        }
    ) as openai_ws:
        await initialize_session(openai_ws, scenario=scenario)
        
        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    if response.get('type') == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(base64.b64decode(response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms")

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    if response.get('type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(f"Interrupting response with id: {last_assistant_item}")
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms")

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms")

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {"name": "responsePart"}
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())

async def initialize_session(openai_ws, scenario="outbound"):
    """Control initial session with OpenAI."""
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
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
    