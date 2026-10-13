"""验证 HTTP API 的完整学习流程和主要错误边界。"""

from __future__ import annotations

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.teaching import TeachingService


class StubTeachingService(TeachingService):
    """用确定性输出替代真实模型，供 API 集成测试使用。"""

    def extract_knowledge_points(
        self, section: dict, learner_level: str, learning_goal: str
    ) -> list[dict]:
        """返回覆盖两步学习流程的固定知识点。"""

        del learner_level, learning_goal
        return [
            {
                "name": "掌握度",
                "description": "掌握意味着学习者能够在不依赖提示时解释并应用概念。",
                "learning_objectives": ["能够解释并应用概念"],
                "prerequisites": [],
                "common_misconceptions": ["能够复述就等于掌握"],
                "difficulty": 2,
                "importance": 0.9,
                "source_pages": [section["start_page"]],
            },
            {
                "name": "延迟复习",
                "description": "延迟复习用于检查知识能否在一段时间后保持可用。",
                "learning_objectives": ["能够说明延迟复习的作用"],
                "prerequisites": ["掌握度"],
                "common_misconceptions": ["刚学会就代表长期记住"],
                "difficulty": 2,
                "importance": 0.8,
                "source_pages": [section["start_page"]],
            },
        ]

    def create_step(
        self,
        section: dict,
        point: dict,
        progress: list[dict],
        learner_level: str,
        learning_goal: str,
        difficulty: int,
        previous_assessment: dict | None,
    ) -> dict:
        """按当前知识点生成可预测的课程和一到两道题。"""

        del section, progress
        if previous_assessment and not previous_assessment["is_correct"]:
            adaptation = "先纠正上一步误区并降低知识跳跃，再"
        elif previous_assessment:
            adaptation = "根据上一步良好表现提高应用性后，"
        else:
            adaptation = ""
        questions = [
            {
                "knowledge_point_ids": [point["id"]],
                "question_type": "short_answer",
                "difficulty": difficulty,
                "question": f"请解释{point['name']}。",
                "reference_answer": point["description"],
                "rubric": ["解释核心含义"],
                "hints": ["考虑它解决了什么问题"],
                "common_errors": point["common_misconceptions"],
                "source_pages": point["source_pages"],
            }
        ]
        if point["name"] == "掌握度":
            questions.append(
                {
                    **questions[0],
                    "question": "请举例说明如何判断是否真正掌握。",
                    "rubric": ["给出不依赖提示的应用例子"],
                }
            )
        return {
            "lesson": {
                "title": f"{point['name']} · {learner_level}",
                "learning_objectives": point["learning_objectives"],
                "explanation": f"{adaptation}围绕目标“{learning_goal}”讲解{point['name']}。",
                "example": f"这是{point['name']}的例子。",
                "common_mistakes": point["common_misconceptions"],
                "source_pages": point["source_pages"],
            },
            "questions": questions,
        }

    def grade_answer(self, question: dict, answer: str) -> dict:
        """通过测试答案中的关键词返回固定评估。"""

        del question
        is_correct = "背下来" not in answer
        return {
            "score": 1.0 if is_correct else 0.2,
            "is_correct": is_correct,
            "covered_points": ["解释核心含义"] if is_correct else [],
            "missing_points": [] if is_correct else ["说明如何独立解释和应用"],
            "misconception_codes": [] if is_correct else ["memorization_equals_mastery"],
            "feedback": "回答正确。" if is_correct else "只记忆还不等于真正掌握。",
            "correction": "掌握要求不依赖提示地解释并应用，而不只是背诵。",
            "recommended_action": "continue" if is_correct else "remediate",
        }


def create_test_client(tmp_path) -> TestClient:
    """创建使用临时存储和模型替身的测试客户端。"""

    settings = Settings(
        database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads",
        openai_api_key=None,
    )
    return TestClient(create_app(settings=settings, teaching_service=StubTeachingService()))


def test_end_to_end_flow(tmp_path, sample_pdf_bytes: bytes) -> None:
    """覆盖上传、备课、多题作答、推进和进度查询的完整链路。"""

    client = create_test_client(tmp_path)

    upload = client.post(
        "/api/documents",
        files={"file": ("course.pdf", sample_pdf_bytes, "application/pdf")},
    )
    assert upload.status_code == 200, upload.text
    document = upload.json()

    sections_response = client.get(f"/api/documents/{document['id']}/sections")
    assert sections_response.status_code == 200
    sections = sections_response.json()
    assert sections

    section_id = next(item["id"] for item in sections if item["number"] == "1.1")
    section_detail = client.get(f"/api/sections/{section_id}").json()
    download = client.get(f"/api/sections/{section_id}/download")
    assert download.status_code == 200
    assert download.headers["content-type"].startswith("text/plain")
    assert "attachment" in download.headers["content-disposition"]
    assert "filename*=UTF-8''" in download.headers["content-disposition"]
    assert download.text == section_detail["content"]

    prepared = client.post(
        f"/api/sections/{section_id}/prepare",
        json={
            "user_id": "student-1",
            "learner_level": "beginner",
            "learning_goal": "理解掌握度",
            "difficulty": 2,
        },
    )
    assert prepared.status_code == 200, prepared.text
    package = prepared.json()
    assert "ai_mode" not in package
    assert package["knowledge_points"]
    assert package["current_step"]["position"] == 1
    assert package["current_step"]["knowledge_point"]["name"] == "掌握度"
    assert len(package["current_step"]["questions"]) == 2
    assert package["current_step"]["status"] == "active"
    assert package["current_step"]["questions"][0]["attempt"] is None
    assert all(
        "reference_answer" not in question
        for question in package["current_step"]["questions"]
    )
    assert "lesson" not in package
    assert "questions" not in package

    latest_session = client.get("/api/users/student-1/sessions/latest")
    assert latest_session.status_code == 200
    assert latest_session.json() == {
        "session_id": package["session_id"],
        "document_id": document["id"],
        "section_id": section_id,
    }

    blocked_next = client.post(f"/api/sessions/{package['session_id']}/next")
    assert blocked_next.status_code == 409

    answer = "只要背下来就是掌握。"
    attempt = client.post(
        f"/api/questions/{package['current_step']['questions'][0]['id']}/attempts",
        json={"user_id": "student-1", "answer": answer, "used_hint": False},
    )
    assert attempt.status_code == 200, attempt.text
    result = attempt.json()
    assert result["assessment"]["score"] < 0.5
    assert result["assessment"]["is_correct"] is False
    assert result["assessment"]["correction"]
    assert result["progress"]
    assert result["step_completed"] is False
    assert result["remaining_questions"] == 1
    assert result["has_next"] is False

    restored = client.get(
        f"/api/sessions/{package['session_id']}", params={"user_id": "student-1"}
    )
    assert restored.status_code == 200, restored.text
    restored_package = restored.json()
    restored_questions = restored_package["current_step"]["questions"]
    assert restored_questions[0]["attempt"]["answer"] == answer
    assert restored_questions[0]["attempt"]["assessment"]["score"] == 0.2
    assert restored_questions[1]["attempt"] is None
    assert restored_package["completed_knowledge_point_ids"] == []
    assert restored_package["completed"] is False

    forbidden_restore = client.get(
        f"/api/sessions/{package['session_id']}", params={"user_id": "other-user"}
    )
    assert forbidden_restore.status_code == 403

    still_blocked = client.post(f"/api/sessions/{package['session_id']}/next")
    assert still_blocked.status_code == 409

    second_attempt = client.post(
        f"/api/questions/{package['current_step']['questions'][1]['id']}/attempts",
        json={"user_id": "student-1", "answer": "能够独立解释并应用。"},
    )
    assert second_attempt.status_code == 200, second_attempt.text
    second_result = second_attempt.json()
    assert second_result["step_completed"] is True
    assert second_result["remaining_questions"] == 0
    assert second_result["has_next"] is True

    restored_step = client.get(
        f"/api/sessions/{package['session_id']}", params={"user_id": "student-1"}
    ).json()
    assert restored_step["current_step"]["status"] == "completed"
    assert restored_step["completed_knowledge_point_ids"] == [
        package["current_step"]["knowledge_point"]["id"]
    ]

    next_response = client.post(f"/api/sessions/{package['session_id']}/next")
    assert next_response.status_code == 200, next_response.text
    next_package = next_response.json()
    assert next_package["current_step"]["position"] == 2
    assert next_package["current_step"]["knowledge_point"]["name"] == "延迟复习"
    assert len(next_package["current_step"]["questions"]) == 1
    assert "先纠正上一步误区" in next_package["current_step"]["lesson"]["explanation"]

    final_attempt = client.post(
        f"/api/questions/{next_package['current_step']['questions'][0]['id']}/attempts",
        json={"user_id": "student-1", "answer": "延迟后再次检查。", "used_hint": False},
    )
    assert final_attempt.status_code == 200, final_attempt.text
    assert final_attempt.json()["completed"] is True

    restored_completed = client.get(
        f"/api/sessions/{package['session_id']}", params={"user_id": "student-1"}
    ).json()
    assert restored_completed["completed"] is True
    assert restored_completed["current_step"]["questions"][0]["attempt"]["answer"] == (
        "延迟后再次检查。"
    )

    progress = client.get("/api/users/student-1/progress")
    assert progress.status_code == 200
    first_point_progress = next(
        item for item in progress.json() if item["knowledge_point_name"] == "掌握度"
    )
    assert first_point_progress["attempt_count"] == 2

    prepared_again = client.post(
        f"/api/sections/{section_id}/prepare",
        json={"user_id": "student-1", "difficulty": 2},
    )
    assert prepared_again.status_code == 200, prepared_again.text
    second_package = prepared_again.json()
    assert second_package["progress"][0]["attempt_count"] == 1
    assert {
        point["id"] for point in second_package["knowledge_points"]
    } == {point["id"] for point in package["knowledge_points"]}


def test_upload_rejects_non_pdf(tmp_path) -> None:
    """非 PDF 扩展名应在解析前被拒绝。"""

    client = create_test_client(tmp_path)

    response = client.post(
        "/api/documents",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )

    assert response.status_code == 415


def test_missing_api_key_prevents_startup(tmp_path) -> None:
    """未注入教学服务时，缺失模型密钥应阻止应用启动。"""

    settings = Settings(
        database_path=tmp_path / "test.db",
        upload_dir=tmp_path / "uploads",
        openai_api_key=None,
    )

    try:
        create_app(settings=settings)
    except RuntimeError as exc:
        assert str(exc) == "缺少 OPENAI_API_KEY，无法启动教学模型服务。"
    else:
        raise AssertionError("缺少模型密钥时应用不应启动")

