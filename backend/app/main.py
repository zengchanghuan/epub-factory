import json
import logging
import shutil
import uuid
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .converter import converter
from .models import Job, JobStatus, OutputMode
from .storage import job_store

BASE_DIR = Path(__file__).resolve().parent.parent
UPLOAD_DIR = BASE_DIR / "uploads"
OUTPUT_DIR = BASE_DIR / "outputs"

UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "level": record.levelname,
            "message": record.getMessage(),
            "logger": record.name,
        }
        if hasattr(record, "trace_id"):
            payload["trace_id"] = record.trace_id
        if hasattr(record, "job_id"):
            payload["job_id"] = record.job_id
        return json.dumps(payload, ensure_ascii=False)


logger = logging.getLogger("epub_factory")
handler = logging.StreamHandler()
handler.setFormatter(JsonFormatter())
logger.setLevel(logging.INFO)
logger.handlers = [handler]

app = FastAPI(title="EPUB Factory API", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def process_job(job: Job) -> None:
    logger.info("job started", extra={"trace_id": job.trace_id, "job_id": job.id})
    job_store.update_status(job.id, JobStatus.running, "开始转换")
    try:
        source_name = Path(job.source_filename).stem
        suffix = "横排繁体" if job.output_mode == OutputMode.traditional else "横排简体"
        if job.enable_translation:
            suffix += f"-翻译_{job.target_lang}"
        output_path = OUTPUT_DIR / f"{source_name}-{suffix}.epub"
        converter.convert_file_to_horizontal(
            Path(job.input_path),
            output_path,
            job.output_mode,
            enable_translation=job.enable_translation,
            target_lang=job.target_lang,
        )
        job_store.update_status(
            job.id,
            JobStatus.success,
            "转换成功",
            output_path=str(output_path),
        )
        logger.info("job success", extra={"trace_id": job.trace_id, "job_id": job.id})
    except Exception as exc:
        job_store.update_status(job.id, JobStatus.failed, str(exc), error_code="CONVERT_FAILED")
        logger.exception(
            "job failed",
            extra={"trace_id": job.trace_id, "job_id": job.id},
        )


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@app.post("/api/v1/jobs")
async def create_job(
    background_tasks: BackgroundTasks,
    file: UploadFile = File(...),
    output_mode: OutputMode = Form(OutputMode.traditional),
    enable_translation: bool = Form(False),
    target_lang: str = Form("zh-CN"),
):
    if not (file.filename.lower().endswith(".epub") or file.filename.lower().endswith(".pdf")):
        raise HTTPException(status_code=400, detail="仅支持 .epub 或 .pdf 文件")

    job_id = uuid.uuid4().hex[:12]
    trace_id = uuid.uuid4().hex
    input_path = UPLOAD_DIR / f"{job_id}-{file.filename}"
    with input_path.open("wb") as buffer:
        shutil.copyfileobj(file.file, buffer)

    job = Job(
        id=job_id,
        source_filename=file.filename,
        output_mode=output_mode,
        trace_id=trace_id,
        input_path=str(input_path),
        enable_translation=enable_translation,
        target_lang=target_lang,
    )
    job_store.add(job)
    background_tasks.add_task(process_job, job)
    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "status": job.status,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "message": "任务已创建",
    }


@app.get("/api/v1/jobs/{job_id}")
def get_job(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    return {
        "job_id": job.id,
        "trace_id": job.trace_id,
        "source_filename": job.source_filename,
        "output_mode": job.output_mode,
        "enable_translation": job.enable_translation,
        "target_lang": job.target_lang,
        "status": job.status,
        "message": job.message,
        "error_code": job.error_code,
        "download_url": f"/api/v1/jobs/{job.id}/download" if job.output_path else None,
        "created_at": job.created_at.isoformat(),
        "updated_at": job.updated_at.isoformat(),
    }


@app.get("/api/v1/jobs/{job_id}/download")
def download_result(job_id: str):
    job = job_store.get(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="任务不存在")
    if job.status != JobStatus.success or not job.output_path:
        raise HTTPException(status_code=400, detail="任务未完成，无法下载")
    output_path = Path(job.output_path)
    if not output_path.exists():
        raise HTTPException(status_code=404, detail="结果文件不存在")
    return FileResponse(path=output_path, filename=output_path.name, media_type="application/epub+zip")

