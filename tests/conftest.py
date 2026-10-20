import pytest

from teaching_agent.application import Application
from teaching_agent.demo import DemoGateway, make_pdf
from teaching_agent.storage import Store


@pytest.fixture
def app(tmp_path):
    store = Store(tmp_path / "data")
    store.initialize()
    return Application(store, gateway=DemoGateway())


@pytest.fixture
def draft_id(app, tmp_path):
    path = tmp_path / "教材.pdf"
    make_pdf(path)
    book = app.import_pdf(path, demo=True)
    identifier = app.create_draft(book.id, "s1")
    app.build_draft(identifier)
    return identifier


@pytest.fixture
def published(app, draft_id):
    app.audit(draft_id)
    app.approve(draft_id)
    app.publish(draft_id)
    return draft_id
