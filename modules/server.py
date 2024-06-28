import asyncio
import base64
import logging
import os.path
import socket
import sys

import uvicorn
from fastapi import FastAPI, WebSocket, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.requests import Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.websockets import WebSocketDisconnect

from env import *
from modules.check_wifi import is_wifi_connected
from modules.control import websocket as control_websocket
from modules.mov.mov_cv2 import MovRecorder
from modules.runpwa import open_as_pwa

if VIDEO_PROVIDER == 'av':
    if IMAGE_PROVIDER == 'pillow':
        from modules.rtsp.av_pil import RTSPClient
    elif IMAGE_PROVIDER == 'cv2':
        from modules.rtsp.av_cv2 import RTSPClient
    else:
        raise ImportError("Wrong image provider for PyAV backend")
elif VIDEO_PROVIDER == 'cv2' and IMAGE_PROVIDER == 'cv2':
    from modules.rtsp.cv2 import RTSPClient
else:
    raise ImportError("Wrong image provider for cv2 backend")

origins = [
    "http://localhost:3000",  # Replace with your frontend app's URL
    "http://localhost:8081",  # Replace with your frontend app's URL
    "http://127.0.0.1:3000",  # Example origin, replace as necessary
    "http://127.0.0.1:8081",  # Example origin, replace as necessary
    "http://localhost:15010",  # Example origin, replace as necessary
    "http://127.0.0.1:15010",  # Example origin, replace as necessary
]

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,  # List of allowed origins
    allow_credentials=True,
    allow_methods=["*"],  # List of allowed methods, e.g., ["GET", "POST"]
    allow_headers=["*"],  # List of allowed headers, e.g., ["Content-Type"]
)

# Serve the static files from the web-build directory
app.mount("/static", StaticFiles(directory=FRONTEND_PATH), name="static")

app.mount("/_expo", StaticFiles(directory=os.path.join(FRONTEND_PATH, "_expo")), name="_expo")
app.mount("/assets", StaticFiles(directory=os.path.join(FRONTEND_PATH, "assets")), name="assets")

RTSP = RTSPClient(TCP_IP, TCP_PORT, RTSP_URI, AV_OPTIONS)
MOV = MovRecorder(RTSP, lambda x: x)

CONNECTIONS_COUNT = 0


# Serve the index.html file at the root URL
@app.get("/")
async def serve_root():
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))


@app.get("/favicon.ico")
async def serve_root():
    return FileResponse(os.path.join(FRONTEND_PATH, "favicon.ico"))


@app.post("/api/media")
async def media(request: Request):
    body = await request.json()
    filename = body.get("filename", None)
    if filename:
        filepath = os.path.join(OUTPUT_DIR, filename)
        if not os.path.exists(filepath):
            raise HTTPException(status_code=400, detail="File does not exist")

        try:
            await open_file_path(filepath)
            return JSONResponse({"status": "ok"})
        except Exception as e:
            return JSONResponse({"error": str(e)}, status_code=500)
    else:
        if not os.path.exists(OUTPUT_DIR):
            raise HTTPException(status_code=400, detail="Media dir not exist")
        await open_output_dir()
        return JSONResponse({"status": "ok"})


@app.post("/api/server/stop")
async def stop_server():
    try:
        await asyncio.sleep(1)
        if CONNECTIONS_COUNT <= 0:
            logging.info("Stop request from client")
            await RTSP.stop()
            uvicorn_server.should_exit = True
            return JSONResponse({})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


# @app.post("/api/server/wifi")
# async def is_wifi_connected():
#     return JSONResponse({"status": is_wifi_connected()})


@app.post("/api/photo")
async def take_photo():
    try:
        filename = await RTSP.shot(await get_output_filename())
        if filename is not None:
            return JSONResponse({"filename": filename})
        raise HTTPException(status_code=400, detail="Can't take a stream")
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/record")
async def toggle_record():
    if not MOV.recording:
        if RTSP.status == RTSP.Status.Running:
            filename = await get_output_filename()
            await MOV.start_async_recording(filename)
            return JSONResponse({})
        raise HTTPException(status_code=400, detail='Stream not running')
    filename, err = await MOV.stop_recording()
    state = "running" if MOV.recording else "stopped"
    response_body = {
        "state": state,
        "error": MOV._error,
        "filename": filename
    }
    MOV._error = None
    return JSONResponse(response_body)


@app.post("/api/record/state")
async def record_state():
    state = "running" if MOV.recording else "stopped"
    response_body = {
        "state": state,
        "error": MOV._error,
        "filename": MOV.filename
    }
    return JSONResponse(response_body)


@app.post("/api/control")
async def control_device(request: Request):
    data = await request.json()
    if (action := data.get("action", None)) is None:
        raise HTTPException(400, detail='Invalid action')

    actions = {
        'zoom': control_websocket.change_zoom,
        'agc': control_websocket.change_agc,
        'scheme': control_websocket.change_color_scheme,
        'ffc': control_websocket.send_trigger_ffc_command
    }

    if (call := actions.get(action, None)) is None:
        raise HTTPException(400, detail='Invalid action')
    await call()
    return JSONResponse({}, 200)


async def get_frames(websocket: WebSocket):
    await websocket.accept()
    while True:
        frame = RTSP.webframe
        if frame is not None:
            b64_frame = base64.b64encode(frame).decode('utf-8')
            await websocket.send_json({'image': b64_frame})
            await asyncio.sleep(1 / RTSP.fps)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    global CONNECTIONS_COUNT
    await websocket.accept()
    try:
        CONNECTIONS_COUNT += 1
        while True:
            frame = RTSP.webframe
            if frame is not None:
                b64_frame = base64.b64encode(frame).decode('utf-8')
                await websocket.send_json({
                    'image': b64_frame,
                    "error": None,
                    "is_wifi": True
                })
                await asyncio.sleep(1 / RTSP.fps)
            else:
                await websocket.send_json({
                    'image': None,
                    "error": "No frame",
                    "is_wifi": is_wifi_connected()
                })
                await asyncio.sleep(0.5)
    except WebSocketDisconnect:
        logging.info("Client disconnected")
        CONNECTIONS_COUNT -= 1
        if CONNECTIONS_COUNT <= 0:
            await RTSP.stop()
            uvicorn_server.should_exit = True
            # uvicorn_server.force_exit()


async def check_port_available(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind((host, port))
        except socket.error as e:
            if e.errno == 10048:
                return False
            else:
                raise
    return True


def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('localhost', 0))
        s.listen(1)
        port = s.getsockname()[1]
    return port


async def run():
    port = CFG['uvicorn'].get('port', find_free_port())
    host = CFG['uvicorn'].get('host', '127.0.0.1')
    pwa_coro = open_as_pwa(f"{host}:{port}")

    config = uvicorn.Config(
        app, host=host, port=port
    )
    global uvicorn_server

    if not await check_port_available(host, port):
        logging.info(f"Port {port} is already in use. Please use a different port.")
        await pwa_coro
        sys.exit(1)

    uvicorn_server = uvicorn.Server(config)
    rtsp_coro = RTSP.run_in_executor()
    serv_coro = uvicorn_server.serve()
    await asyncio.gather(rtsp_coro, serv_coro, pwa_coro)
