"""定义 API 请求、响应以及教学领域对象的数据契约。"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field


class DocumentSummary(BaseModel):
    """客户端展示文档列表所需的摘要。"""

    id: str
    filename: str
    page_count: int
    status: str
    created_at: datetime


class SectionSummary(BaseModel):
    """不包含正文的章节层级和页码摘要。"""

    id: str
    document_id: str
    parent_id: str | None = None
    level: int
    number: str | None = None
    title: str
    start_page: int
    end_page: int
    confidence: float


class SectionDetail(SectionSummary):
    """附带完整提取文本的章节详情。"""

    content: str


class KnowledgePoint(BaseModel):
    """从章节证据中提取的可学习、可考核知识单元。"""

    id: str
    section_id: str
    name: str
    description: str
    learning_objectives: list[str]
    prerequisites: list[str] = Field(default_factory=list)
    common_misconceptions: list[str] = Field(default_factory=list)
    difficulty: int = Field(ge=1, le=5)
    importance: float = Field(ge=0, le=1)
    source_pages: list[int]


class Lesson(BaseModel):
    """围绕单个知识点生成的一小步讲解。"""

    id: str
    session_id: str
    title: str
    learning_objectives: list[str]
    explanation: str
    example: str
    common_mistakes: list[str]
    source_pages: list[int]


class PrepareLearningRequest(BaseModel):
    """备课接口接收的学习者背景和目标。"""

    user_id: str = Field(default="default-user", min_length=1, max_length=100)
    learner_level: Literal["beginner", "intermediate", "advanced"] = "beginner"
    learning_goal: str = Field(default="理解并能够应用本节知识", max_length=500)
    difficulty: int = Field(default=2, ge=1, le=5)


class AttemptRequest(BaseModel):
    """学习者对一道题目的作答。"""

    user_id: str = Field(default="default-user", min_length=1, max_length=100)
    answer: str = Field(min_length=1, max_length=10000)
    used_hint: bool = False


class Assessment(BaseModel):
    """模型对作答的结构化评估和后续动作建议。"""

    score: float = Field(ge=0, le=1)
    is_correct: bool
    covered_points: list[str]
    missing_points: list[str]
    misconception_codes: list[str]
    feedback: str
    correction: str
    recommended_action: Literal["continue", "retry", "remediate"]


class QuestionAttempt(BaseModel):
    """用于恢复页面的已提交答案及其评估。"""

    answer: str
    used_hint: bool
    assessment: Assessment


class QuestionPublic(BaseModel):
    """返回给学习者且隐藏参考答案和评分标准的题目。"""

    id: str
    session_id: str
    knowledge_point_ids: list[str]
    question_type: str
    difficulty: int
    question: str
    hints: list[str]
    source_pages: list[int]
    attempt: QuestionAttempt | None = None


class MasteryRecord(BaseModel):
    """学习者在单个知识点上的累计掌握状态。"""

    user_id: str
    knowledge_point_id: str
    knowledge_point_name: str
    mastery_score: float
    status: str
    attempt_count: int
    correct_streak: int
    misconceptions: list[str]
    last_reviewed_at: datetime
    next_review_at: datetime | None = None


class LearningStep(BaseModel):
    """一次渐进式教学步骤及其理解检查题。"""

    id: str
    session_id: str
    position: int
    status: Literal["active", "completed"]
    correction: str | None = None
    knowledge_point: KnowledgePoint
    lesson: Lesson
    questions: list[QuestionPublic] = Field(min_length=1)


class LearningPackage(BaseModel):
    """开始章节学习时一次性返回的完整上下文。"""

    session_id: str
    section: SectionSummary
    knowledge_points: list[KnowledgePoint]
    current_step: LearningStep
    progress: list[MasteryRecord] = Field(default_factory=list)
    completed_knowledge_point_ids: list[str] = Field(default_factory=list)
    completed: bool = False


class AttemptResult(BaseModel):
    """提交答案后返回的评估、进度和流程状态。"""

    assessment: Assessment
    progress: list[MasteryRecord]
    step_completed: bool
    remaining_questions: int
    has_next: bool
    completed: bool


class NextStepResult(BaseModel):
    """推进学习会话后返回的新步骤或完成标记。"""

    current_step: LearningStep | None = None
    progress: list[MasteryRecord] = Field(default_factory=list)
    completed: bool


class ErrorResponse(BaseModel):
    """可扩展的统一错误响应结构。"""

    detail: str
    code: str | None = None
    context: dict[str, Any] | None = None

