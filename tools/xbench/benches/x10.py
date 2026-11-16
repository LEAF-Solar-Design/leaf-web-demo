"""X10 first reachable blocker, using only normal project UI controls.

Manifest ``x10`` config: project_name, fixtures (role -> manifest ID), and
optional import_controls/workflow_controls (ordered accessible button/tab names).
Each bound fixture declares its matching role. Required roles are ROLES below.
The oracle JSON has a nonempty ``expected`` object; rates JSON has ``items``
with decimal-string quantity/unit_rate fields. Neither is product evidence.
Customer manifests and fixture contents must remain outside git.
"""

import json
from decimal import Decimal, InvalidOperation, localcontext
from pathlib import Path
import re
from uuid import UUID, uuid4

from xbench_core import Step
from xbench_session import redact, signed_in


LIMITS = ("First-blocker grading slice only. Import/terrain acceptance is incomplete; "
          "downstream checks 2..6 are unimplemented. No PASS is possible yet.")
ROLES = ("drawing", "comparison_drawing", "bom", "terrain", "rates", "oracle")
DOWNSTREAM = (
    "cut/fill quantities against terrain and oracle",
    "BOM reconciliation plus comparison drawing changes",
    "UI rate edit with exact cents against oracle",
    "missing quantities/rates remain unpriced",
    "save/reopen/export hashes",
)


def safe_text(value):
    """Persist bounded plain UI sentences, never transport diagnostics or markup."""
    text = re.sub(r"<[^>]*>", "[markup]", str(value))
    text = re.sub(r"https?://\S+|\b\S+@\S+\b", "[redacted]", text)
    text = re.sub(r"(?i)\b(authorization|bearer|token|secret|password)\b[^\n]*",
                  "[redacted]", text)
    return redact("".join(c for c in text if c in "\n\t" or ord(c) >= 32))[:4000]


def rate_total(rates):
    """Exact fixture arithmetic; this does not assert a product result."""
    if not isinstance(rates, dict) or not isinstance(rates.get("items"), list) or not rates["items"]:
        raise ValueError("rates.items must be a nonempty list")
    values = []
    for item in rates["items"]:
        if not isinstance(item, dict):
            raise ValueError("invalid rate item")
        pair = []
        for key in ("quantity", "unit_rate"):
            value = item.get(key)
            if not isinstance(value, str) or not re.fullmatch(r"\d{1,30}(?:\.\d{1,30})?", value):
                raise ValueError("rates require nonnegative decimal strings")
            pair.append(Decimal(value))
        values.append(pair)
    with localcontext() as context:
        context.prec = 128 + len(str(len(values)))
        return sum((quantity * rate for quantity, rate in values), Decimal(0))


def bindings(ctx):
    config = ctx.manifest.get("x10")
    if not isinstance(config, dict):
        raise ValueError("missing x10 config")
    name = config.get("project_name")
    if not isinstance(name, str) or not name.strip() or len(name) > 100 or "\n" in name:
        raise ValueError("missing or invalid x10.project_name")
    mapping = config.get("fixtures")
    if not isinstance(mapping, dict) or set(mapping) != set(ROLES):
        raise ValueError("x10.fixtures must bind drawing, comparison_drawing, bom, terrain, rates, oracle")
    if not all(isinstance(value, str) for value in mapping.values()) or len(set(mapping.values())) != len(ROLES):
        raise ValueError("fixture bindings must have distinct manifest IDs")
    fixtures = {row["id"]: row for row in ctx.manifest["fixtures"]}
    verified = {row["id"]: row for row in ctx.fixtures}
    bound = {}
    for role, identity in mapping.items():
        row = fixtures.get(identity)
        proof = verified.get(identity)
        if (row is None or row.get("role") != role or proof is None or proof.get("ok") is not True
                or any(proof.get(key) != row.get(key) for key in ("path", "bytes", "sha256"))):
            raise ValueError("missing, wrong-role or unverified fixture binding: " + role)
        bound[role] = Path(row["path"])
    for key in ("import_controls", "workflow_controls"):
        controls = config.get(key, [])
        if (not isinstance(controls, list) or len(controls) > 8
                or any(not isinstance(label, str) or not label.strip() or len(label) > 100 for label in controls)):
            raise ValueError("invalid x10 UI control path")
    oracle = json.loads(bound["oracle"].read_text(encoding="utf-8"), parse_float=Decimal)
    if not isinstance(oracle, dict) or not isinstance(oracle.get("expected"), dict) or not oracle["expected"]:
        raise ValueError("oracle.expected must be a nonempty object")
    total = rate_total(json.loads(bound["rates"].read_text(encoding="utf-8")))
    return config, bound, oracle["expected"], total


def classify(observation, expected=None):
    """Only an explicit rendered blocker in a confirmed new project can FAIL."""
    fresh = observation.get("fresh_project") is True
    label = observation.get("ground_option", "")
    unavailable = (isinstance(label, str)
                   and re.search(r"\bGround (?:Mount )?Physical\b", label, re.I)
                   and re.search(r"\bunavailable\b", label, re.I))
    pending = "Checking Solar steps" in observation.get("ui_text", "")
    proven = fresh and observation.get("evidence_saved") is True and not pending
    if proven and observation.get("imported_version_selected") is True and unavailable:
        verdict = "FAIL"
        reason = ("Fresh canonical project with imported drawing version selected renders "
                  "Ground Physical unavailable; acceptance is conjunctive.")
    elif proven and observation.get("import_rejection"):
        verdict = "FAIL"
        reason = "Primary drawing import explicitly rejected by product: " + safe_text(observation["import_rejection"])
    else:
        verdict = "UNREADABLE"
        remaining = ("" if observation.get("imported_version_selected") is True
                     else "complete primary drawing import and version selection, ")
        reason = ("No confirmed rendered product blocker. Remaining: " + remaining
                  + "bind terrain and verify every step-1 acceptance clause. "
                  + safe_text(observation.get("issue", "Available option alone is insufficient.")))
    rows = [Step(1, verdict, reason + " Evidence: x10-ui.json / x10-ground.png when saved.",
                 observed=observation, expected=expected)]
    rows.extend(Step(n, "UNREADABLE", "Prerequisite: completed step 1 and " + subject
                     + "; downstream product observation unimplemented in this slice.")
                for n, subject in enumerate(DOWNSTREAM, 2))
    return rows


def _click_controls(page, labels):
    for label in labels:
        page.get_by_role("button", name=label, exact=True).or_(
            page.get_by_role("tab", name=label, exact=True)).click(timeout=15_000)


def _create_project(page, ctx, config, observation):
    name = config["project_name"].strip() + "-" + uuid4().hex[:12]
    field = page.get_by_role("textbox", name="Project name", exact=True)
    if not field.is_visible():
        page.get_by_role("button", name=re.compile(r"^Projects: change project\.")).click(timeout=15_000)
        field = page.get_by_role("textbox", name="New project", exact=True)
    field.fill(name, timeout=15_000)

    def created(response):
        return (response.request.method == "POST"
                and response.url == ctx.base.rstrip("/") + "/api/projects")

    with page.expect_response(created, timeout=30_000) as pending:
        field.locator("xpath=ancestor::form").get_by_role(
            "button", name="Create project", exact=True).click(timeout=15_000)
    response = pending.value
    observation["creation_status"] = response.status
    if response.status not in (200, 201):
        raise ValueError("project creation not confirmed")
    # Extract only the response's project identity. Never retain the body.
    project = response.json().get("project", {})
    identity = str(UUID(project.get("project_id") or project.get("id")))
    if project.get("name") != name:
        raise ValueError("project creation identity mismatch")
    chip = page.get_by_role("button", name="Projects: change project. Project " + name, exact=True)
    chip.wait_for(state="visible", timeout=30_000)
    # Confirm the UI actually opened this response identity, without reading bodies.
    page.wait_for_load_state("networkidle", timeout=30_000)
    observation.update(fresh_project=True, project_id=identity, project_name=safe_text(name))


def _save(page, ctx, observation):
    ctx.out_dir.mkdir(parents=True, exist_ok=True)
    page.get_by_role("navigation", name="Solar design steps").screenshot(
        path=str(ctx.out_dir / "x10-ground.png"), timeout=15_000)
    saved = dict(observation, evidence_saved=True)
    (ctx.out_dir / "x10-ui.json").write_text(
        json.dumps(saved, indent=2) + "\n", encoding="utf-8")
    observation["evidence_saved"] = True


def _diagnostic(ctx, observation, stage, exc):
    # Exception messages can contain URLs, provider bodies or fixture paths.
    observation.update(stage=stage, exception_type=safe_text(type(exc).__name__),
                       issue="UI/infrastructure unreadable at " + stage + ": " + safe_text(type(exc).__name__),
                       evidence_saved=False)
    try:
        ctx.out_dir.mkdir(parents=True, exist_ok=True)
        (ctx.out_dir / "x10-ui.json").write_text(
            json.dumps(observation, indent=2) + "\n", encoding="utf-8")
    except OSError:
        observation["diagnostic_saved"] = False


def _continue_workspace(page):
    button = page.get_by_role("button", name="Continue workspace", exact=True)
    if button.is_visible():
        button.click(timeout=15_000)


def _prepare_workspace(page):
    page.get_by_role("combobox", name="Workspace profile", exact=True).select_option(
        label="Solar CAD", timeout=15_000)
    _continue_workspace(page)


def _import_primary(page, ctx, config, bound, observation):
    project = page.get_by_role("button", name="Project", exact=True)
    if project.get_attribute("aria-expanded") != "true":
        project.click(timeout=15_000)
    page.get_by_role("button", name="Material", exact=True).click(timeout=15_000)
    _click_controls(page, config.get("import_controls", []))
    upload = page.get_by_label("Drawing file", exact=True)
    upload.wait_for(state="attached", timeout=30_000)
    page.wait_for_function("input => !input.disabled", arg=upload.element_handle(), timeout=30_000)
    upload.set_input_files(str(bound["drawing"]), timeout=15_000)
    status = page.get_by_role("status").filter(has_text=re.compile(r"^Attached "))
    status.wait_for(state="visible", timeout=180_000)
    # Keep the rendered attachment sentence, replacing customer names and paths.
    sentence = status.inner_text(timeout=15_000)
    version = re.search(r"\bas version (\d{1,9})\b", sentence)
    observation["attachment_status"] = ("Attached [primary drawing]"
        + (" as version " + version.group(1) if version else "") + " to [fresh project]")
    observation["import_status"] = "Attached primary bound drawing through material UI"
    # The board preview selects canonical project mode; Close workspace would
    # instead discard the project binding and must never be used here.
    project = page.get_by_role("button", name="Project", exact=True)
    if project.get_attribute("aria-expanded") != "true":
        project.click(timeout=15_000)
    page.get_by_role("button", name="Preview v1 in the drawing", exact=True).click(timeout=30_000)
    marker = page.get_by_role("status").filter(
        has_text=re.compile(r"^Project drawing version selected\.$"))
    marker.wait_for(state="visible", timeout=30_000)
    observation["version_selection_status"] = safe_text(marker.inner_text())
    observation["imported_version_selected"] = True


def _open_workflow(page, config):
    back = page.get_by_role("button", name="Return to drawing", exact=True)
    if back.is_visible():
        back.click(timeout=15_000)
    _continue_workspace(page)
    solar = page.get_by_role("tab", name="Solar", exact=True)
    if solar.is_visible() and solar.get_attribute("aria-selected") != "true":
        solar.click(timeout=15_000)
    _click_controls(page, config.get("workflow_controls", []))
    toggle = page.get_by_role("button", name="Solar flow tools", exact=True)
    if toggle.is_visible() and toggle.get_attribute("aria-expanded") == "false":
        toggle.click(timeout=15_000)


def _ground(page, ctx, observation):
    rail = page.get_by_role("navigation", name="Solar design steps")
    rail.get_by_text("Checking Solar steps for the selected project drawing version.", exact=True).first.wait_for(
        state="hidden", timeout=60_000)
    picker = page.get_by_role("combobox", name="Solar flow", exact=True)
    picker.wait_for(state="visible", timeout=15_000)
    option = picker.locator('option[value="ground-physical"]')
    label = option.inner_text(timeout=15_000)
    picker.select_option("ground-physical", timeout=15_000)
    observation["ground_option"] = safe_text(label)
    observation["selected_labels"] = [safe_text(text) for text in picker.locator("option:checked").all_text_contents()]
    observation["ui_text"] = safe_text(page.get_by_role("navigation", name="Solar design steps").inner_text())
    _save(page, ctx, observation)


def observe(page, ctx, config, bound, session_info=None):
    observation = {"fresh_project": False, "imported_version_selected": False, "evidence_saved": False}
    for key in ("source", "web_build"):
        value = (session_info or {}).get(key)
        if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{40}", value):
            observation[key] = value
    stage = "workspace profile"
    try:
        _prepare_workspace(page)
        stage = "fresh project creation"
        _create_project(page, ctx, config, observation)
        stage = "primary drawing attachment and version selection"
        _import_primary(page, ctx, config, bound, observation)
        stage = "selected drawing workflow navigation"
        _open_workflow(page, config)
        stage = "rendered Ground Physical evidence"
        _ground(page, ctx, observation)
    except Exception as exc:
        _diagnostic(ctx, observation, stage, exc)
    return observation


def run(ctx):
    try:
        config, bound, _expected, total = bindings(ctx)
    except (ValueError, OSError, KeyError, TypeError, InvalidOperation):
        return classify({"issue": "Missing or invalid fixture bindings/config, oracle or rates; browser not opened."})
    try:
        repo = Path(__file__).resolve().parents[3]
        with signed_in(repo, ctx.source_sha, origin=ctx.base) as (page, info):
            observation = observe(page, ctx, config, bound, session_info=info)
    except Exception as exc:
        observation = {}
        _diagnostic(ctx, observation, "authentication/browser session", exc)
    # Fixture arithmetic is retained as provenance only, never a measured outcome.
    observation["fixture_rate_total"] = str(total)
    return classify(observation, expected=_expected)
