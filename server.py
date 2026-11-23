import logging
import os
import secrets
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException  # noqa: E402
from fastapi.responses import FileResponse  # noqa: E402
from pydantic import BaseModel, ConfigDict, Field  # noqa: E402

from lead_hunter import count_leads, jobs, run_lead_hunter  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
INDEX_HTML = Path(__file__).parent / "static" / "index.html"


def require_token(token: str = "", x_webhook_token: str = Header(default="")):
    expected = os.environ.get("WEBHOOK_TOKEN", "")
    if expected and not secrets.compare_digest(expected, token or x_webhook_token):
        raise HTTPException(status_code=401, detail="invalid token")


class RunRequest(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    niche: str = Field(min_length=1, max_length=80)
    city: str = Field(min_length=1, max_length=80)
    state: str = Field(min_length=2, max_length=40)


app = FastAPI(title="Lead Hunter")
api = APIRouter(dependencies=[Depends(require_token)])


@app.get("/", include_in_schema=False)
def index():
    return FileResponse(INDEX_HTML)


@api.get("/api/check")
def check():
    return {"ok": True}


@api.get("/api/count")
def api_count():
    data = count_leads()
    return {"count": data["count"], "rows": data["rows"], "sheet_url": data["sheet_url"]}


@api.post("/api/runs")
def create_run(body: RunRequest):
    return jobs.submit(body.niche, body.city, body.state)


@api.get("/api/runs")
def list_runs():
    return jobs.list_jobs()


@api.get("/api/runs/{job_id}")
def get_run(job_id: str):
    job = jobs.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="search not found")
    return job


@api.get("/webhook/lead-hunter-run")
def lead_hunter_run(niche: str = "", city: str = "", state: str = ""):
    return run_lead_hunter(niche, city, state)


@api.get("/webhook/lead-hunter-count")
def lead_hunter_count():
    return count_leads()


app.include_router(api)
