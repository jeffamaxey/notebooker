from __future__ import unicode_literals

import datetime
import json
import subprocess
import sys
import threading
import time
import uuid
from logging import getLogger
from typing import Any, Dict, List, Tuple, NamedTuple, Optional, AnyStr

import nbformat
import os
from flask import Blueprint, abort, jsonify, render_template, request, url_for, current_app
from nbformat import NotebookNode

from notebooker.constants import JobStatus
from notebooker.serialization.serialization import get_serializer_from_cls
from notebooker.utils.conversion import generate_ipynb_from_py
from notebooker.utils.filesystem import get_template_dir, get_output_dir
from notebooker.utils.templates import _get_parameters_cell_idx, _get_preview
from notebooker.utils.web import convert_report_name_url_to_path, json_to_python, validate_mailto, validate_title
from notebooker.web.handle_overrides import handle_overrides
from notebooker.web.utils import get_serializer, _get_python_template_dir, get_all_possible_templates

try:
    FileNotFoundError
except NameError:
    FileNotFoundError = IOError

run_report_bp = Blueprint("run_report_bp", __name__)
logger = getLogger(__name__)


@run_report_bp.route("/run_report/get_preview/<path:report_name>", methods=["GET"])
def run_report_get_preview(report_name):
    """
    Get a preview of the Notebook Template which is about to be executed.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: An HTML rendering of a notebook template which has been converted from .py -> .ipynb -> .html
    """
    report_name = convert_report_name_url_to_path(report_name)
    # Handle the case where a rendered ipynb asks for "custom.css"
    if ".css" in report_name:
        return ""
    return _get_preview(
        report_name,
        notebooker_disable_git=current_app.config["NOTEBOOKER_DISABLE_GIT"],
        py_template_dir=_get_python_template_dir(),
    )


def get_report_as_nb(relative_report_path: str) -> NotebookNode:
    path = generate_ipynb_from_py(
        current_app.config["TEMPLATE_DIR"],
        relative_report_path,
        current_app.config["NOTEBOOKER_DISABLE_GIT"],
        _get_python_template_dir(),
    )
    return nbformat.read(path, as_version=nbformat.v4.nbformat)


def get_report_parameters_html(relative_report_path: str) -> str:
    nb = get_report_as_nb(relative_report_path)
    metadata_idx = _get_parameters_cell_idx(nb)
    parameters_as_html = ""
    if metadata_idx is not None:
        metadata = nb["cells"][metadata_idx]
        parameters_as_html = metadata["source"].strip()
    return parameters_as_html


@run_report_bp.route("/get_report_parameters/<path:report_name>", methods=["GET"])
def run_report_get_parameters(report_name):
    """
    Get the parameters of the Notebook Template which is about to be executed in Python.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: Get the parameters of the Notebook Template which is about to be executed in Python syntax.
    """
    report_name = convert_report_name_url_to_path(report_name)
    params_as_html = get_report_parameters_html(report_name)
    return jsonify({"result": params_as_html}) if params_as_html else ("", 404)


@run_report_bp.route("/run_report/<path:report_name>", methods=["GET"])
def run_report_http(report_name):
    """
    The "Run Report" interface is generated by this method.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: An HTML template which is the Run Report interface.
    """
    report_name = convert_report_name_url_to_path(report_name)
    json_params = request.args.get("json_params")
    initial_python_parameters = json_to_python(json_params) or ""
    try:
        nb = get_report_as_nb(report_name)
    except FileNotFoundError:
        logger.exception("Report was not found.")
        return render_template(
            "run_report.html",
            report_found=False,
            parameters_as_html="REPORT NOT FOUND",
            has_prefix=False,
            has_suffix=False,
            report_name=report_name,
            all_reports=get_all_possible_templates(),
            initialPythonParameters={},
        )
    metadata_idx = _get_parameters_cell_idx(nb)
    has_prefix = has_suffix = False
    if metadata_idx is not None:
        has_prefix, has_suffix = (bool(nb["cells"][:metadata_idx]), bool(nb["cells"][metadata_idx + 1 :]))
    return render_template(
        "run_report.html",
        parameters_as_html=get_report_parameters_html(report_name),
        report_found=True,
        has_prefix=has_prefix,
        has_suffix=has_suffix,
        report_name=report_name,
        all_reports=get_all_possible_templates(),
        initialPythonParameters=initial_python_parameters,
        default_mailfrom=current_app.config["DEFAULT_MAILFROM"],
    )


def _monitor_stderr(process, job_id, serializer_cls, serializer_args):
    stderr = []
    # Unsure whether flask app contexts are thread-safe; just reinitialise the serializer here.
    result_serializer = get_serializer_from_cls(serializer_cls, **serializer_args)
    while True:
        line = process.stderr.readline().decode("utf-8")
        if line == "" and process.poll() is not None:
            result_serializer.update_stdout(job_id, stderr, replace=True)
            break
        stderr.append(line)
        logger.info(line)  # So that we have it in the log, not just in memory.
        result_serializer.update_stdout(job_id, new_lines=[line])
    return "".join(stderr)


def run_report(
    report_name,
    report_title,
    mailto,
    overrides,
    hide_code=False,
    generate_pdf_output=False,
    prepare_only=False,
    scheduler_job_id=None,
    run_synchronously=False,
    mailfrom=None,
    n_retries=3,
) -> str:
    """
    Actually run the report in earnest.
    Uses a subprocess to execute the report asynchronously, which is identical to the non-webapp entrypoint.
    :param report_name: `str` The report which we are executing
    :param report_title: `str` The user-specified title of the report
    :param mailto: `Optional[str]` Who the results will be emailed to
    :param overrides: `Optional[Dict[str, Any]]` The parameters to be passed into the report
    :param generate_pdf_output: `bool` Whether we're generating a PDF. Defaults to False.
    :param prepare_only: `bool` Whether to do everything except execute the notebook. Useful for testing.
    :param scheduler_job_id: `Optional[str]` if the job was triggered from the scheduler, this is the scheduler's job id
    :param run_synchronously: `bool` If True, then we will join the stderr monitoring thread until the job has completed
    :param mailfrom: `str` if passed, then this string will be used in the from field
    :param n_retries: The number of retries to attempt.
    :return: The unique job_id.
    """
    job_id = str(uuid.uuid4())
    job_start_time = datetime.datetime.now()
    result_serializer = get_serializer()
    result_serializer.save_check_stub(
        job_id,
        report_name,
        report_title=report_title,
        job_start_time=job_start_time,
        status=JobStatus.SUBMITTED,
        overrides=overrides,
        mailto=mailto,
        generate_pdf_output=generate_pdf_output,
        hide_code=hide_code,
        scheduler_job_id=scheduler_job_id,
    )
    app_config = current_app.config
    command = (
        [
            os.path.join(sys.exec_prefix, "bin", "notebooker-cli"),
            "--output-base-dir",
            get_output_dir(),
            "--template-base-dir",
            get_template_dir(),
            "--py-template-base-dir",
            app_config["PY_TEMPLATE_BASE_DIR"],
            "--py-template-subdir",
            app_config["PY_TEMPLATE_SUBDIR"],
            "--default-mailfrom",
            app_config["DEFAULT_MAILFROM"],
        ]
        + (["--notebooker-disable-git"] if app_config["NOTEBOOKER_DISABLE_GIT"] else [])
        + ["--serializer-cls", result_serializer.__class__.__name__]
        + result_serializer.serializer_args_to_cmdline_args()
        + [
            "execute-notebook",
            "--job-id",
            job_id,
            "--report-name",
            report_name,
            "--report-title",
            report_title,
            "--mailto",
            mailto,
            "--overrides-as-json",
            json.dumps(overrides),
            "--pdf-output" if generate_pdf_output else "--no-pdf-output",
            "--hide-code" if hide_code else "--show-code",
            "--n-retries", str(n_retries),
        ]
        + (["--prepare-notebook-only"] if prepare_only else [])
        + ([f"--scheduler-job-id={scheduler_job_id}"] if scheduler_job_id is not None else [])
        + ([f"--mailfrom={mailfrom}"] if mailfrom is not None else [])
    )
    p = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    stderr_thread = threading.Thread(
        target=_monitor_stderr,
        args=(p, job_id, current_app.config["SERIALIZER_CLS"], current_app.config["SERIALIZER_CONFIG"]),
    )
    stderr_thread.daemon = True
    stderr_thread.start()
    if run_synchronously:
        p.wait()
    else:
        time.sleep(1)
        p.poll()
    if p.returncode:
        raise RuntimeError(f"The report execution failed with exit code {p.returncode}")

    return job_id


class RunReportParams(NamedTuple):
    report_title: AnyStr
    mailto: AnyStr
    mailfrom: AnyStr
    generate_pdf_output: bool
    hide_code: bool
    scheduler_job_id: Optional[str]


def validate_run_params(params, issues: List[str]) -> RunReportParams:
    logger.info(f"Validating input params: {params}")
    # Find and cleanse the title of the report
    report_title = validate_title(params.get("report_title"), issues)
    # Get mailto email address
    mailto = validate_mailto(params.get("mailto"), issues)
    mailfrom = validate_mailto(params.get("mailfrom"), issues)
    # "on" comes from HTML, "True" comes from urlencoded JSON params
    generate_pdf_output = params.get("generate_pdf") in ("on", "True")
    hide_code = params.get("hide_code") in ("on", "True")

    out = RunReportParams(
        report_title=report_title,
        mailto=mailto,
        mailfrom=mailfrom,
        generate_pdf_output=generate_pdf_output,
        hide_code=hide_code,
        scheduler_job_id=params.get("scheduler_job_id"),
    )
    logger.info(f"Validated params: {out}")
    return out


def _handle_run_report(
    report_name: str, overrides_dict: Dict[str, Any], issues: List[str]
) -> Tuple[str, int, Dict[str, str]]:
    params = validate_run_params(request.values, issues)
    if issues:
        return jsonify({"status": "Failed", "content": ("\n".join(issues))})
    report_name = convert_report_name_url_to_path(report_name)
    logger.info(f"Handling run report with parameters report_name={report_name} "
                f"report_title={params.report_title}"
                f"mailto={params.mailto} "
                f"overrides_dict={overrides_dict} "
                f"generate_pdf_output={params.generate_pdf_output} "
                f"hide_code={params.hide_code} "
                f"scheduler_job_id={params.scheduler_job_id}"
                f"mailfrom={params.mailfrom}")
    try:
        job_id = run_report(
            report_name,
            params.report_title,
            params.mailto,
            overrides_dict,
            generate_pdf_output=params.generate_pdf_output,
            hide_code=params.hide_code,
            scheduler_job_id=params.scheduler_job_id,
            mailfrom=params.mailfrom,
        )
        return (
            jsonify({"id": job_id}),
            202,  # HTTP Accepted code
            {"Location": url_for("pending_results_bp.task_status", report_name=report_name, job_id=job_id)},
        )
    except RuntimeError as e:
        return jsonify({"status": "Failed", "content": f"The job failed to initialise. Error: {str(e)}"}), 500, {}


@run_report_bp.route("/run_report_json/<path:report_name>", methods=["POST"])
def run_report_json(report_name):
    """
    Execute a notebook from a JSON request.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: 202-redirects to the "task_status" interface.
    """
    issues = []
    # Get JSON overrides
    overrides_dict = json.loads(request.values.get("overrides"))
    return _handle_run_report(report_name, overrides_dict, issues)


@run_report_bp.route("/run_report/<path:report_name>", methods=["POST"])
def run_checks_http(report_name):
    """
    Execute a notebook from an HTTP request.

    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: 202-redirects to the "task_status" interface.
    """
    issues = []
    # Get and process raw python overrides
    overrides_dict = handle_overrides(request.values.get("overrides"), issues)
    return _handle_run_report(report_name, overrides_dict, issues)


def _rerun_report(job_id, prepare_only=False, run_synchronously=False):
    result = get_serializer().get_check_result(job_id)
    if not result:
        abort(404)
    prefix = "Rerun of "
    title = result.report_title if result.report_title.startswith(prefix) else (prefix + result.report_title)
    return run_report(
        result.report_name,
        title,
        result.mailto,
        result.overrides,
        generate_pdf_output=result.generate_pdf_output,
        prepare_only=prepare_only,
        scheduler_job_id=None,  # the scheduler will never call rerun
        run_synchronously=run_synchronously,
    )


@run_report_bp.route("/rerun_report/<job_id>/<path:report_name>", methods=["POST"])
def rerun_report(job_id, report_name):
    """
    Rerun a notebook using its already-existing parameters.

    :param job_id: The Job ID of the report which we are rerunning.
    :param report_name: The parameter here should be a "/"-delimited string which mirrors the directory structure of \
        the notebook templates.

    :returns: 202-redirects to the "task_status" interface.
    """
    new_job_id = _rerun_report(job_id)
    return jsonify(
        {"results_url": url_for("serve_results_bp.task_results", report_name=report_name, job_id=new_job_id)}
    )
