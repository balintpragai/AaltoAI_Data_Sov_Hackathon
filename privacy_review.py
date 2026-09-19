#!/usr/bin/env python3
"""
Privacy review CLI (advisory only). Does not transform CSVs or write pipeline column lists.

    export PRIVACY_REVIEW_BASE_URL=https://your-eu-host.example/v1
    export PRIVACY_REVIEW_MODEL=...
    export PRIVACY_REVIEW_API_KEY=...   # optional Bearer token; never commit

    python privacy_review.py request outputs/profile_raw.json -o outputs/review_raw.json
    python privacy_review.py render outputs/review_raw.json
    python privacy_review.py sign outputs/review_raw.json --suggestion-id col:enb \\
        --disposition accepted --by ada --role data_owner
    python privacy_review.py must-answer outputs/review_raw.json --question-id q1 --on --by ada --role data_owner
    python privacy_review.py answer outputs/review_raw.json --question-id q1 --text "..." --by ada --role data_owner
    python privacy_review.py share-check outputs/review_release.json
    python privacy_review.py promote outputs/review_release.json --markdown docs/privacy-reviews/review.md
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from redacted_profile import SUBJECTS

REPO_ROOT = Path(__file__).resolve().parent
ALLOWED_TEST_IDS = {f"T{i}" for i in range(1, 10)}
QI_CLASSES = frozenset({"direct identifier", "quasi-identifier"})
ROLES = ("engineer", "data_owner")
DISPOSITIONS = ("pending", "accepted", "rejected", "deferred")
FORBIDDEN_HOSTS = ("api.openai.com", "openai.com")
LOG_PATH = Path("outputs/privacy_review_calls.jsonl")

POLICY_STATIC = """
You produce a privacy review: non-binding suggestions only. You are not a DPIA, not
anonymisation, and not a transform. Do not claim the data are anonymous (GDPR Recital 26).
Do not emit pipeline config or column lists for guide_anonymize.py or tests.py.

Ground every sensitivity label in: GDPR Art. 4(1) and Recital 30 (online identifiers);
Art. 4(5) (pseudonymisation is not anonymisation); WP216 tests (singling out, linkability,
inference); EDPB isolation / linkage / inference; ePrivacy traffic and location rules.

You may flag additional risks and open questions. You may not remove enb / time_start /
direct identifiers from the high-risk set without recording that as an open question for
a human. If a column description is null, do not invent a meaning as fact — ask an open question.

Return one JSON object matching the schema in the user message. Checks must use tests.py ids
T1–T9 or status not_implemented with a note.
""".strip()


class CheckModel(BaseModel):
    tests_py_id: str | None = None
    status: str | None = None
    note: str | None = None

    @model_validator(mode="after")
    def shape(self) -> CheckModel:
        if self.tests_py_id:
            if self.tests_py_id not in ALLOWED_TEST_IDS:
                raise ValueError(f"unknown tests_py_id {self.tests_py_id}")
            return self
        if self.status != "not_implemented" or not (self.note or "").strip():
            raise ValueError("check must be a T1–T9 id or not_implemented with a note")
        return self


class ColumnSuggestion(BaseModel):
    name: str
    proposed_class: Literal[
        "direct identifier", "quasi-identifier", "behavioural", "kpi", "unknown"
    ]
    rationale: str = Field(min_length=1)
    checks: list[CheckModel] = Field(min_length=1)


class OpenQuestionDraft(BaseModel):
    text: str = Field(min_length=1)
    must_answer_proposed: bool = False


class ModelReview(BaseModel):
    columns: list[ColumnSuggestion] = Field(min_length=1)
    open_questions: list[OpenQuestionDraft] = Field(default_factory=list)

    @field_validator("columns")
    @classmethod
    def unique_names(cls, cols: list[ColumnSuggestion]) -> list[ColumnSuggestion]:
        names = [c.name for c in cols]
        if len(names) != len(set(names)):
            raise ValueError("duplicate column names in model output")
        return cols


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def load_policy_pack() -> str:
    chunks = [POLICY_STATIC]
    context = REPO_ROOT / "CONTEXT.md"
    agents = REPO_ROOT / "AGENTS.md"
    if context.exists():
        chunks.append("# Glossary\n" + context.read_text(encoding="utf-8"))
    if agents.exists():
        text = agents.read_text(encoding="utf-8")
        start = text.find("## 2. Dataset context")
        end = text.find("\n## 3.", start) if start >= 0 else -1
        if start >= 0 and end > start:
            chunks.append(text[start:end].strip())
        elif start >= 0:
            chunks.append(text[start : start + 8000])
    return "\n\n".join(chunks)


def output_schema_prompt() -> str:
    return """Respond with JSON only:
{
  "columns": [
    {
      "name": "<exact profile column name>",
      "proposed_class": "direct identifier|quasi-identifier|behavioural|kpi|unknown",
      "rationale": "<short, with source>",
      "checks": [
        {"tests_py_id": "T3"}
        or {"tests_py_id": null, "status": "not_implemented", "note": "..."}
      ]
    }
  ],
  "open_questions": [
    {"text": "...", "must_answer_proposed": false}
  ]
}
Every profile column must appear exactly once. Do not include dispositions.
""".strip()


def assert_allowed_base_url(base_url: str) -> None:
    parsed = urlparse(base_url)
    host = (parsed.hostname or "").lower()
    if not parsed.scheme.startswith("http") or not host:
        raise SystemExit("PRIVACY_REVIEW_BASE_URL must be an http(s) URL with a host")
    if host in FORBIDDEN_HOSTS or host.endswith(".openai.com"):
        raise SystemExit(
            "PRIVACY_REVIEW_BASE_URL must not be an OpenAI company host (ADR 0002). "
            "Point it at your EU/on-prem OpenAI-compatible endpoint."
        )


def completions_url(base_url: str) -> str:
    base = base_url.rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def chat_json(base_url: str, api_key: str | None, model: str, messages: list[dict], timeout: float) -> str:
    url = completions_url(base_url)
    body: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "temperature": 0,
        "response_format": {"type": "json_object"},
    }

    def post(payload: dict) -> dict:
        data = json.dumps(payload).encode()
        req = urllib.request.Request(url, data=data, method="POST")
        req.add_header("Content-Type", "application/json")
        if api_key:
            req.add_header("Authorization", f"Bearer {api_key}")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:500]
            raise RuntimeError(f"HTTP {exc.code} from model host: {detail}") from exc

    try:
        raw = post(body)
    except RuntimeError as exc:
        if "response_format" in str(exc).lower() or "400" in str(exc):
            body.pop("response_format", None)
            raw = post(body)
        else:
            raise
    try:
        return raw["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError) as exc:
        raise RuntimeError("model host returned no message content") from exc


def parse_model_review(content: str) -> ModelReview:
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
        text = text.strip()
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"fail closed: model output is not JSON ({exc})") from exc
    try:
        return ModelReview.model_validate(data)
    except ValidationError as exc:
        raise SystemExit(f"fail closed: model JSON failed schema validation:\n{exc}") from exc


def assemble_review(profile: dict, draft: ModelReview, model_name: str, host: str) -> dict:
    by_name = {c.name: c for c in draft.columns}
    missing = [c["name"] for c in profile["columns"] if c["name"] not in by_name]
    extra = [n for n in by_name if n not in {c["name"] for c in profile["columns"]}]
    if missing or extra:
        raise SystemExit(
            f"fail closed: model columns must match the profile exactly. missing={missing} extra={extra}"
        )
    columns = []
    for col in profile["columns"]:
        sug = by_name[col["name"]]
        columns.append(
            {
                "suggestion_id": f"col:{col['name']}",
                "name": col["name"],
                "type": col["type"],
                "description": col.get("description"),
                "proposed_class": sug.proposed_class,
                "rationale": sug.rationale,
                "checks": [c.model_dump() for c in sug.checks],
                "disposition": "pending",
                "by": None,
                "role": None,
                "at": None,
            }
        )
    questions = []
    for i, q in enumerate(draft.open_questions, start=1):
        questions.append(
            {
                "id": f"q{i}",
                "text": q.text,
                "must_answer_proposed": q.must_answer_proposed,
                "must_answer": False,
                "answered": False,
                "answer": None,
                "by": None,
                "role": None,
                "at": None,
            }
        )
    review = {
        "subject": profile["subject"],
        "redaction": profile.get("redaction", {}),
        "model": {"host": host, "name": model_name, "time": utc_now()},
        "profile_sha256": profile.get("profile_sha256"),
        "columns": columns,
        "open_questions": questions,
    }
    review["shareable_release_candidate"] = shareable(review)
    return review


def shareable(review: dict) -> bool:
    if review.get("subject") != "release_candidate":
        return False
    for q in review.get("open_questions", []):
        if q.get("must_answer") and not q.get("answered"):
            return False
    return True


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def save_review(path: Path, review: dict) -> None:
    review["shareable_release_candidate"] = shareable(review)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(review, indent=2))


def append_log(entry: dict) -> None:
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LOG_PATH.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry) + "\n")


def render_markdown(review: dict) -> str:
    lines = [
        f"# Privacy review ({review.get('subject')})",
        "",
        "Advisory suggestions only. Not a DPIA. Not anonymisation.",
        "",
        f"- model: `{review.get('model', {}).get('name')}` host `{review.get('model', {}).get('host')}`",
        f"- time: {review.get('model', {}).get('time')}",
        f"- profile_sha256: `{review.get('profile_sha256')}`",
        f"- shareable_release_candidate: **{review.get('shareable_release_candidate')}**",
        "",
        "## Columns",
        "",
    ]
    for col in review["columns"]:
        lines.append(f"### `{col['name']}` (`{col['suggestion_id']}`)")
        lines.append("")
        lines.append(f"- type: `{col.get('type')}`")
        lines.append(f"- description: {col.get('description') or '_null_'}")
        lines.append(f"- proposed class: **{col['proposed_class']}**")
        lines.append(f"- rationale: {col['rationale']}")
        lines.append(f"- disposition: {col['disposition']} by {col.get('by') or '—'} ({col.get('role') or '—'})")
        for chk in col["checks"]:
            if chk.get("tests_py_id"):
                lines.append(f"- check: `{chk['tests_py_id']}`")
            else:
                lines.append(f"- check: not implemented — {chk.get('note')}")
        lines.append("")
    lines.append("## Open questions")
    lines.append("")
    if not review.get("open_questions"):
        lines.append("_None._")
    for q in review.get("open_questions", []):
        flag = "must-answer" if q.get("must_answer") else (
            "proposed must-answer" if q.get("must_answer_proposed") else "optional"
        )
        state = "answered" if q.get("answered") else "open"
        lines.append(f"- `{q['id']}` ({flag}, {state}): {q['text']}")
        if q.get("answer"):
            lines.append(f"  - answer: {q['answer']}")
    lines.append("")
    return "\n".join(lines)


def require_role_for_column(col: dict, role: str) -> None:
    if col["proposed_class"] in QI_CLASSES and role != "data_owner":
        raise SystemExit(
            f"{col['suggestion_id']} changes identifier/QI handling; role must be data_owner"
        )


def cmd_request(args: argparse.Namespace) -> None:
    profile = load_json(Path(args.profile))
    if profile.get("subject") not in SUBJECTS:
        raise SystemExit("profile subject missing or invalid")
    if "top_value" in json.dumps(profile):
        raise SystemExit("refuse: profile looks like column_stats (contains top_value)")

    if args.offline_json:
        content = Path(args.offline_json).read_text(encoding="utf-8")
        host = "offline"
        model_name = "offline"
    else:
        base = os.environ.get("PRIVACY_REVIEW_BASE_URL") or ""
        model_name = os.environ.get("PRIVACY_REVIEW_MODEL") or ""
        key = os.environ.get("PRIVACY_REVIEW_API_KEY")
        if not base or not model_name:
            raise SystemExit("set PRIVACY_REVIEW_BASE_URL and PRIVACY_REVIEW_MODEL")
        assert_allowed_base_url(base)
        host = urlparse(base).hostname or "unknown"
        messages = [
            {"role": "system", "content": load_policy_pack()},
            {
                "role": "user",
                "content": output_schema_prompt() + "\n\nRedacted column profile:\n"
                + json.dumps(profile, indent=2),
            },
        ]
        timeout = float(os.environ.get("PRIVACY_REVIEW_TIMEOUT", "60"))
        content = chat_json(base, key, model_name, messages, timeout)

    draft = parse_model_review(content)
    host = host if not args.offline_json else "offline"
    review = assemble_review(profile, draft, model_name, host)
    dest = Path(args.output) if args.output else Path("outputs") / f"review_{profile['subject']}.json"
    save_review(dest, review)
    append_log(
        {
            "time": utc_now(),
            "model": model_name,
            "host": host,
            "profile_sha256": profile.get("profile_sha256"),
            "review_path": str(dest),
        }
    )
    md_path = dest.with_suffix(".md")
    md_path.write_text(render_markdown(review))
    print(f"Wrote {dest}")
    print(f"Wrote {md_path}")
    print("Keep these under outputs/ until a human checks redaction.")


def find_suggestion(review: dict, suggestion_id: str) -> dict:
    for col in review["columns"]:
        if col["suggestion_id"] == suggestion_id:
            return col
    raise SystemExit(f"unknown suggestion-id {suggestion_id}")


def find_question(review: dict, question_id: str) -> dict:
    for q in review.get("open_questions", []):
        if q["id"] == question_id:
            return q
    raise SystemExit(f"unknown question-id {question_id}")


def cmd_sign(args: argparse.Namespace) -> None:
    path = Path(args.review)
    review = load_json(path)
    if args.disposition not in DISPOSITIONS or args.disposition == "pending":
        raise SystemExit("disposition must be accepted, rejected, or deferred")
    if args.role not in ROLES:
        raise SystemExit("role must be engineer or data_owner")
    col = find_suggestion(review, args.suggestion_id)
    if args.disposition == "accepted":
        require_role_for_column(col, args.role)
    col["disposition"] = args.disposition
    col["by"] = args.by
    col["role"] = args.role
    col["at"] = utc_now()
    save_review(path, review)
    print(f"Signed {args.suggestion_id} -> {args.disposition}")


def cmd_must_answer(args: argparse.Namespace) -> None:
    path = Path(args.review)
    review = load_json(path)
    if args.role not in ROLES:
        raise SystemExit("role must be engineer or data_owner")
    q = find_question(review, args.question_id)
    q["must_answer"] = bool(args.on)
    q["by"] = args.by
    q["role"] = args.role
    q["at"] = utc_now()
    save_review(path, review)
    print(f"{q['id']} must_answer={q['must_answer']}")


def cmd_answer(args: argparse.Namespace) -> None:
    path = Path(args.review)
    review = load_json(path)
    if args.role not in ROLES:
        raise SystemExit("role must be engineer or data_owner")
    q = find_question(review, args.question_id)
    q["answered"] = True
    q["answer"] = args.text
    q["by"] = args.by
    q["role"] = args.role
    q["at"] = utc_now()
    save_review(path, review)
    print(f"Answered {q['id']}")


def cmd_share_check(args: argparse.Namespace) -> None:
    review = load_json(Path(args.review))
    if review.get("subject") != "release_candidate":
        print("raw (or non-release) reviews are never a shareable release candidate")
        sys.exit(2)
    blockers = [
        q["id"] for q in review.get("open_questions", [])
        if q.get("must_answer") and not q.get("answered")
    ]
    if blockers:
        print("not shareable; must-answer still open: " + ", ".join(blockers))
        sys.exit(1)
    print("shareable_release_candidate: no must-answer open questions (still not legally anonymous)")
    sys.exit(0)


def cmd_render(args: argparse.Namespace) -> None:
    review = load_json(Path(args.review))
    text = render_markdown(review)
    if args.output:
        Path(args.output).write_text(text)
        print(f"Wrote {args.output}")
    else:
        print(text)


def cmd_promote(args: argparse.Namespace) -> None:
    src = Path(args.review)
    review = load_json(src)
    dest = Path(args.markdown)
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(render_markdown(review))
    print(f"Wrote {dest}")
    print(
        "Human must check this markdown for leaked labels/IDs before commit. "
        "Do not copy the JSON profile into git."
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Advisory privacy review (ADR 0002).")
    sub = p.add_subparsers(dest="cmd", required=True)

    req = sub.add_parser("request", help="Call the model with a redacted profile")
    req.add_argument("profile")
    req.add_argument("-o", "--output", default="")
    req.add_argument(
        "--offline-json",
        default="",
        help="Skip HTTP; parse this file as the model JSON (tests / air-gap)",
    )
    req.set_defaults(func=cmd_request)

    sg = sub.add_parser("sign")
    sg.add_argument("review")
    sg.add_argument("--suggestion-id", required=True)
    sg.add_argument("--disposition", required=True, choices=("accepted", "rejected", "deferred"))
    sg.add_argument("--by", required=True)
    sg.add_argument("--role", required=True, choices=ROLES)
    sg.set_defaults(func=cmd_sign)

    ma = sub.add_parser("must-answer")
    ma.add_argument("review")
    ma.add_argument("--question-id", required=True)
    ma.add_argument("--on", action=argparse.BooleanOptionalAction, default=True)
    ma.add_argument("--by", required=True)
    ma.add_argument("--role", required=True, choices=ROLES)
    ma.set_defaults(func=cmd_must_answer)

    ans = sub.add_parser("answer")
    ans.add_argument("review")
    ans.add_argument("--question-id", required=True)
    ans.add_argument("--text", required=True)
    ans.add_argument("--by", required=True)
    ans.add_argument("--role", required=True, choices=ROLES)
    ans.set_defaults(func=cmd_answer)

    sc = sub.add_parser("share-check")
    sc.add_argument("review")
    sc.set_defaults(func=cmd_share_check)

    rd = sub.add_parser("render")
    rd.add_argument("review")
    rd.add_argument("-o", "--output", default="")
    rd.set_defaults(func=cmd_render)

    pr = sub.add_parser("promote")
    pr.add_argument("review")
    pr.add_argument("--markdown", required=True)
    pr.set_defaults(func=cmd_promote)
    return p


def main() -> None:
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
