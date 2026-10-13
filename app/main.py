"""组装 FastAPI 应用并暴露 PDF 教学工作流接口。"""

from __future__ import annotations

import hashlib
import logging
import re
from pathlib import Path
from time import perf_counter
from typing import Annotated
from urllib.parse import quote, urlparse
from uuid import uuid4

from fastapi import FastAPI, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from app.config import Settings
from app.db import Database
from app.logging_config import configure_logging
from app.pdf_parser import PDFParseError, parse_text_pdf
from app.repository import Repository
from app.schemas import (
    Assessment,
    AttemptRequest,
    AttemptResult,
    DocumentSummary,
    KnowledgePoint,
    LearningPackage,
    LearningStep,
    MasteryRecord,
    NextStepResult,
    PrepareLearningRequest,
    SectionDetail,
    SectionSummary,
)
from app.teaching import AIServiceError, TeachingService, create_teaching_service

logger = logging.getLogger("learning_agent.api")


def create_app(
    settings: Settings | None = None,
    teaching_service: TeachingService | None = None,
) -> FastAPI:
    """创建应用、初始化依赖并注册页面、中间件和 API 路由。"""

    settings = settings or Settings.from_env()
    configure_logging(settings.log_level)
    settings.ensure_directories()
    database = Database(settings.database_path)
    database.initialize()
    repository = Repository(database)
    teaching = teaching_service or create_teaching_service(settings)

    app = FastAPI(
        title="PDF 教学 Agent",
        version="0.1.0",
        description="基于指定 PDF 章节进行讲解、练习、评估和掌握度记录。",
    )
    app.state.settings = settings
    app.state.repository = repository
    app.state.teaching = teaching
    logger.info(
        "application_started database=%s upload_dir=%s model=%s api_host=%s log_level=%s",
        settings.database_path,
        settings.upload_dir,
        settings.llm_model_id,
        (urlparse(settings.llm_api_base).hostname or "unknown"),
        settings.log_level,
    )

    @app.middleware("http")
    async def log_request(request: Request, call_next):
        """为每个请求补充追踪 ID，并记录耗时和结果状态。"""

        request_id = request.headers.get("x-request-id") or uuid4().hex[:12]
        started = perf_counter()
        try:
            response = await call_next(request)
        except Exception:
            logger.exception(
                "request_failed request_id=%s method=%s path=%s duration_ms=%d",
                request_id,
                request.method,
                request.url.path,
                round((perf_counter() - started) * 1000),
            )
            raise
        response.headers["X-Request-ID"] = request_id
        log = logger.warning if response.status_code >= 400 else logger.info
        log(
            "request_completed request_id=%s method=%s path=%s status=%d duration_ms=%d",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            round((perf_counter() - started) * 1000),
        )
        return response

    static_dir = Path(__file__).parent / "static"
    app.mount("/static", StaticFiles(directory=static_dir), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        """返回单页应用入口。"""

        return FileResponse(static_dir / "index.html")

    @app.get("/api/health")
    def health() -> dict[str, str]:
        """提供无需访问外部模型的存活检查。"""

        return {"status": "ok"}

    @app.post("/api/documents", response_model=DocumentSummary)
    async def upload_document(
        file: Annotated[UploadFile, File(description="包含文本层的 PDF")],
    ) -> dict:
        """校验并解析 PDF；相同内容通过哈希直接复用。"""

        filename = Path(file.filename or "document.pdf").name
        if not filename.lower().endswith(".pdf"):
            raise HTTPException(status_code=415, detail="仅支持 PDF 文件。")
        limit = settings.max_upload_size_mb * 1024 * 1024
        data = await file.read(limit + 1)
        if len(data) > limit:
            raise HTTPException(
                status_code=413, detail=f"文件不能超过 {settings.max_upload_size_mb} MB。"
            )
        digest = hashlib.sha256(data).hexdigest()
        existing = repository.find_document_by_hash(digest)
        if existing:
            return existing
        try:
            parsed = await run_in_threadpool(parse_text_pdf, data)
        except PDFParseError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        stored_path = settings.upload_dir / f"{uuid4().hex}.pdf"
        stored_path.write_bytes(data)
        try:
            return repository.create_document(filename, stored_path, digest, parsed)
        except Exception:
            stored_path.unlink(missing_ok=True)
            raise

    @app.get("/api/documents", response_model=list[DocumentSummary])
    def list_documents() -> list[dict]:
        """列出当前已解析的文档。"""

        return repository.list_documents()

    @app.get("/api/documents/{document_id}/sections", response_model=list[SectionSummary])
    def list_sections(document_id: str) -> list[dict]:
        """列出指定文档识别出的章节。"""

        try:
            return repository.list_sections(document_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/sections/{section_id}", response_model=SectionDetail)
    def get_section(section_id: str) -> dict:
        """返回章节元数据和完整正文。"""

        try:
            return repository.get_section(section_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    @app.get("/api/sections/{section_id}/download")
    def download_section(section_id: str) -> Response:
        """以兼容中英文文件名的附件形式下载章节文本。"""

        try:
            section = repository.get_section(section_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        label = " ".join(
            part for part in (section.get("number"), section["title"]) if part
        )
        filename = f"{label or '章节内容'}.txt"
        fallback_label = re.sub(r"[^A-Za-z0-9._-]+", "-", label).strip("-_")
        fallback = f"{fallback_label or 'section'}.txt"
        disposition = (
            f'attachment; filename="{fallback}"; filename*=UTF-8\'\'{quote(filename)}'
        )
        return Response(
            content=section["content"],
            media_type="text/plain",
            headers={"Content-Disposition": disposition},
        )

    @app.post("/api/sections/{section_id}/prepare", response_model=LearningPackage)
    async def prepare_learning(
        section_id: str, request: PrepareLearningRequest
    ) -> LearningPackage:
        """提取或复用知识点，并创建会话的第一步教学内容。"""

        logger.info(
            "prepare_started section_id=%s user_id=%s learner_level=%s difficulty=%d",
            section_id,
            request.user_id,
            request.learner_level,
            request.difficulty,
        )
        try:
            section = repository.get_section(section_id)
        except KeyError as exc:
            logger.warning("prepare_section_not_found section_id=%s", section_id)
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        stage = "load_knowledge_points"
        try:
            points = repository.list_knowledge_points(section_id)
            if not points:
                stage = "extract_knowledge_points"
                raw_points = await run_in_threadpool(
                    teaching.extract_knowledge_points,
                    section,
                    request.learner_level,
                    request.learning_goal,
                )
                points = repository.replace_knowledge_points(section_id, raw_points)
            logger.info(
                "prepare_knowledge_points_ready section_id=%s count=%d cached=%s",
                section_id,
                len(points),
                stage == "load_knowledge_points",
            )
            progress = repository.get_progress(request.user_id, section_id)
            first_point = points[0]
            stage = "create_learning_step"
            raw_step = await run_in_threadpool(
                teaching.create_step,
                section,
                first_point,
                progress,
                request.learner_level,
                request.learning_goal,
                request.difficulty,
                None,
            )
            stage = "create_session"
            session_id = repository.create_session(
                request.user_id,
                section_id,
                request.learner_level,
                request.learning_goal,
            )
            stage = "save_learning_step"
            step = repository.save_learning_step(
                session_id, 1, first_point["id"], raw_step
            )
        except AIServiceError as exc:
            logger.exception(
                "prepare_failed section_id=%s user_id=%s stage=%s error=%s",
                section_id,
                request.user_id,
                stage,
                exc,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        logger.info(
            "prepare_succeeded section_id=%s session_id=%s knowledge_points=%d questions=%d",
            section_id,
            session_id,
            len(points),
            len(step["questions"]),
        )

        return LearningPackage(
            session_id=session_id,
            section=SectionSummary.model_validate(section),
            knowledge_points=[KnowledgePoint.model_validate(point) for point in points],
            current_step=LearningStep.model_validate(step),
            progress=[MasteryRecord.model_validate(item) for item in progress],
        )

    @app.post("/api/sessions/{session_id}/next", response_model=NextStepResult)
    async def next_learning_step(session_id: str) -> NextStepResult:
        """根据最近评估生成下一知识点，或在无剩余内容时结束会话。"""

        logger.info("next_step_started session_id=%s", session_id)
        try:
            session = repository.get_session(session_id)
            active_step = repository.get_active_step(session_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

        if active_step:
            raise HTTPException(status_code=409, detail="请先回答当前知识点的问题。")
        if session["status"] == "completed":
            progress = repository.get_progress(session["user_id"], session["section_id"])
            return NextStepResult(
                progress=[MasteryRecord.model_validate(item) for item in progress],
                completed=True,
            )

        point = repository.get_next_untaught_point(session_id)
        if not point:
            repository.complete_session(session_id)
            progress = repository.get_progress(session["user_id"], session["section_id"])
            return NextStepResult(
                progress=[MasteryRecord.model_validate(item) for item in progress],
                completed=True,
            )

        section = repository.get_section(session["section_id"])
        progress = repository.get_progress(session["user_id"], session["section_id"])
        previous_assessment = repository.get_latest_assessment(session_id)
        latest_step = repository.get_latest_step(session_id)
        difficulty = latest_step["questions"][-1]["difficulty"] if latest_step else 2
        try:
            raw_step = await run_in_threadpool(
                teaching.create_step,
                section,
                point,
                progress,
                session["learner_level"],
                session["learning_goal"],
                difficulty,
                previous_assessment,
            )
            step = repository.save_learning_step(
                session_id,
                repository.next_step_position(session_id),
                point["id"],
                raw_step,
            )
        except AIServiceError as exc:
            logger.exception(
                "next_step_failed session_id=%s knowledge_point_id=%s error=%s",
                session_id,
                point["id"],
                exc,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc

        logger.info(
            "next_step_succeeded session_id=%s step_id=%s position=%d questions=%d",
            session_id,
            step["id"],
            step["position"],
            len(step["questions"]),
        )

        return NextStepResult(
            current_step=LearningStep.model_validate(step),
            progress=[MasteryRecord.model_validate(item) for item in progress],
            completed=False,
        )

    @app.post("/api/questions/{question_id}/attempts", response_model=AttemptResult)
    async def submit_attempt(question_id: str, request: AttemptRequest) -> AttemptResult:
        """评估单次回答、更新掌握度，并在题目答完后推进步骤状态。"""

        logger.info(
            "answer_grading_started question_id=%s user_id=%s used_hint=%s answer_chars=%d",
            question_id,
            request.user_id,
            request.used_hint,
            len(request.answer),
        )
        try:
            question = repository.get_question(question_id)
            step = repository.get_step_by_question(question_id)
            session = repository.get_session(question["session_id"])
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if session["user_id"] != request.user_id:
            raise HTTPException(status_code=403, detail="学习者与当前会话不匹配。")
        if step["status"] != "active":
            raise HTTPException(status_code=409, detail="该学习步骤已经完成。")
        if repository.question_has_attempt(question_id, request.user_id):
            raise HTTPException(status_code=409, detail="该题已经回答过。")
        try:
            raw_assessment = await run_in_threadpool(
                teaching.grade_answer, question, request.answer
            )
            assessment = Assessment.model_validate(raw_assessment)
        except (AIServiceError, ValueError) as exc:
            logger.exception(
                "answer_grading_failed question_id=%s user_id=%s error=%s",
                question_id,
                request.user_id,
                exc,
            )
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        repository.save_attempt_and_update_mastery(
            question,
            request.user_id,
            request.answer,
            request.used_hint,
            assessment.model_dump(),
        )
        try:
            step_completed, remaining_questions = repository.finalize_step_if_ready(
                step["id"], request.user_id
            )
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        progress = repository.get_progress(request.user_id, session["section_id"])
        has_next = False
        completed = False
        if step_completed:
            has_next = repository.get_next_untaught_point(session["id"]) is not None
            completed = not has_next
            if completed:
                repository.complete_session(session["id"])
        logger.info(
            "answer_grading_succeeded question_id=%s user_id=%s score=%.3f "
            "step_completed=%s remaining_questions=%d session_completed=%s",
            question_id,
            request.user_id,
            assessment.score,
            step_completed,
            remaining_questions,
            completed,
        )
        return AttemptResult(
            assessment=assessment,
            progress=[MasteryRecord.model_validate(item) for item in progress],
            step_completed=step_completed,
            remaining_questions=remaining_questions,
            has_next=has_next,
            completed=completed,
        )

    @app.get("/api/users/{user_id}/progress", response_model=list[MasteryRecord])
    def get_progress(
        user_id: str,
        section_id: Annotated[str | None, Query()] = None,
    ) -> list[dict]:
        """返回学习者全部或指定章节的知识点掌握记录。"""

        return repository.get_progress(user_id, section_id)

    return app


app = create_app()

