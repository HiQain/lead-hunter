import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from . import graph

log = logging.getLogger("lead_hunter")

STEP_MESSAGES = {
    "search_places": ("places", "Found {n} businesses on Google Maps"),
    "split_places": ("candidates", "{n} have no website or a weak one"),
    "scrape_and_extract_email": ("scraped", "{n} have a working email address"),
    "validate_phones": ("phone_checked", "{n} have a valid US phone number"),
    "ai_verify": ("verified", "{n} passed the business check"),
    "dedupe_against_sheet": ("new_leads", "{n} are not already in the sheet"),
    "write_sheet": (None, "Saved to the Google Sheet"),
}
MAX_JOBS = 50

# One worker: searches run one after another so they never share API rate limits.
_executor = ThreadPoolExecutor(max_workers=1)
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def _snapshot(job: dict) -> dict:
    return {**job, "steps": list(job["steps"]), "leads": list(job["leads"])}


def _summary(job: dict) -> dict:
    keys = ("id", "niche", "city", "state", "status", "created", "started", "finished", "error")
    return {**{k: job[k] for k in keys}, "lead_count": len(job["leads"])}


def submit(niche: str, city: str, state: str) -> dict:
    job = {
        "id": uuid.uuid4().hex[:12], "niche": niche, "city": city, "state": state,
        "status": "queued", "created": time.time(), "started": None, "finished": None,
        "error": None, "steps": [], "leads": [],
    }
    with _lock:
        _jobs[job["id"]] = job
        finished = [j for j in _jobs.values() if j["status"] in ("done", "failed")]
        for old in finished[: max(0, len(_jobs) - MAX_JOBS)]:
            _jobs.pop(old["id"], None)
    _executor.submit(_run, job)
    return get(job["id"])


def get(job_id: str) -> dict | None:
    with _lock:
        job = _jobs.get(job_id)
        return _snapshot(job) if job else None


def list_jobs() -> list[dict]:
    with _lock:
        return [_summary(j) for j in sorted(_jobs.values(), key=lambda j: j["created"], reverse=True)]


def _run(job: dict) -> None:
    with _lock:
        job.update(status="running", started=time.time())
    pending: list[dict] = []
    try:
        for node, update in graph.stream_lead_hunter(job["niche"], job["city"], job["state"]):
            key, message = STEP_MESSAGES.get(node, (None, node))
            count = len(update.get(key) or []) if key else None
            if node == "dedupe_against_sheet":
                pending = update.get("new_leads") or []
            with _lock:
                job["steps"].append({"node": node, "message": message.format(n=count), "count": count})
                if node == "write_sheet":
                    job["leads"] = pending
        with _lock:
            job.update(status="done", finished=time.time())
    except Exception as e:
        log.exception("Search %s failed", job["id"])
        with _lock:
            job.update(status="failed", finished=time.time(), error=str(e) or e.__class__.__name__)
