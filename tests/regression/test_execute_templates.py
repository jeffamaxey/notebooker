import datetime
import uuid

import pytest

from notebooker.execute_notebook import _run_checks

from ..utils import all_templates


@pytest.mark.parametrize("template_name", all_templates())
def test_execution_of_templates(template_name, template_dir, output_dir, flask_app):
    flask_app.config["PY_TEMPLATE_DIR"] = ""
    with flask_app.app_context():
        _run_checks(
            f"job_id_{str(uuid.uuid4())[:6]}",
            datetime.datetime.now(),
            template_name,
            template_name,
            output_dir,
            template_dir,
            {},
            generate_pdf_output=False,
        )
