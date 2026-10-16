from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from .git_sync import verify_github_signature
from .service import BrainError, BrainService

router = APIRouter(tags=["webhooks"])


def _service(request: Request) -> BrainService:
    return request.app.state.brain


@router.post("/webhooks/{project_id}/github")
async def github_webhook(project_id: str, request: Request) -> JSONResponse:
    service = _service(request)
    try:
        project = await service.get_project(project_id)
    except BrainError as error:
        return JSONResponse(error.to_dict(), status_code=error.status)
    if not project.webhook_secret:
        return JSONResponse(
            {"error": "webhook secret not configured for this project"}, status_code=404
        )
    body = await request.body()
    if not verify_github_signature(
        project.webhook_secret, body, request.headers.get("x-hub-signature-256")
    ):
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    event = request.headers.get("x-github-event", "")
    if event == "ping":
        return JSONResponse({"ok": True, "pong": True})
    if event != "push":
        return JSONResponse({"ok": True, "ignored": event})
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return JSONResponse({"error": "payload is not valid JSON"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "payload must be a JSON object"}, status_code=400)
    result = await service.ingest_github_push(
        project, request.headers.get("x-github-delivery"), payload
    )
    return JSONResponse(result)
