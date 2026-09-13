from enum import StrEnum

from fastapi import FastAPI, WebSocket, WebSocketDisconnect

from app.services.estigia import (
    EstigiaExtractionError,
    extract_information_from_estigia_orden_despacho,
)

app = FastAPI()


class ConnectionManager:
    def __init__(self):
        self.active_connections: dict[str, list[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, room_id: str):
        await websocket.accept()
        if room_id not in self.active_connections:
            self.active_connections[room_id] = []
        self.active_connections[room_id].append(websocket)

    def disconnect(self, websocket: WebSocket, room_id: str):
        self.active_connections[room_id].remove(websocket)
        if not self.active_connections[room_id]:
            del self.active_connections[room_id]

    async def send_personal_message(self, message: str, websocket: WebSocket):
        await websocket.send_json(message)

    async def broadcast(self, message: str):
        for room_id in self.active_connections:
            for connection in self.active_connections[room_id]:
                await connection.send_json(message)

    async def broadcast_to_room(self, room_id: str, message: str):
        if room_id in self.active_connections:
            for connection in self.active_connections[room_id]:
                await connection.send_json(message)


manager = ConnectionManager()


class ActionType(StrEnum):
    EXTRACT = "extract_information_from_estigia_orden_despacho"
    CLIENT_DISCONNECTED = "client_disconnected"


def is_valid_action_type(value: str) -> bool:
    try:
        ActionType(value)
        return True
    except ValueError:
        return False


def _response_message(action: str, data: dict) -> dict:
    return {
        "action": action,
        "status": "success",
        "data": data,
    }


def _error_response(action: str | None, code: str, message: str) -> dict:
    return {
        "action": action,
        "status": "error",
        "error": {"code": code, "message": message},
    }


@app.websocket("/ws/{room_id}")
async def websocket_endpoint(websocket: WebSocket, room_id: str):
    await manager.connect(websocket, room_id)
    try:
        while True:
            data = await websocket.receive_json()
            if not isinstance(data, dict):
                await manager.send_personal_message(
                    _error_response(
                        None,
                        "INVALID_REQUEST",
                        "The WebSocket request is invalid.",
                    ),
                    websocket,
                )
                continue

            action = data.get("action")
            if not is_valid_action_type(action):
                await manager.send_personal_message(
                    _error_response(
                        action if isinstance(action, str) else None,
                        "INVALID_REQUEST",
                        "The requested action is invalid.",
                    ),
                    websocket,
                )
                continue

            if action == ActionType.EXTRACT:
                url = data.get("data").get("estigiaOrdenDespachoUrl")
                if not isinstance(url, str):
                    await manager.send_personal_message(
                        _error_response(
                            ActionType.EXTRACT,
                            "INVALID_REQUEST",
                            "estigiaOrdenDespachoUrl must be a string.",
                        ),
                        websocket,
                    )
                    continue

                try:
                    extracted_data = (
                        await extract_information_from_estigia_orden_despacho(url)
                    )
                except EstigiaExtractionError as error:
                    response = _error_response(
                        ActionType.EXTRACT,
                        error.code,
                        error.message,
                    )
                else:
                    response = _response_message(ActionType.EXTRACT, extracted_data)

                await manager.broadcast_to_room(room_id, response)
    except WebSocketDisconnect:
        manager.disconnect(websocket, room_id)
        response = _response_message(
            ActionType.CLIENT_DISCONNECTED,
            {
                "message": f"Client #{room_id} left the chat",
            },
        )
        await manager.broadcast_to_room(room_id, response)


app.frontend("/", directory="frontend/dist")
