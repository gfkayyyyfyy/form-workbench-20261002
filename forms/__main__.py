"""命令行入口：python -m forms submit --form f.json --answers a.json --db data.db"""

import argparse
import json
import sys

from . import (
    InvalidInput,
    StorageError,
    load_json_file,
    save_submission,
    validate_answers,
    validate_form,
)


def _emit(payload):
    print(json.dumps(payload, ensure_ascii=False))


def main(argv=None):
    parser = argparse.ArgumentParser(prog="forms")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit", help="校验答卷并保存有效提交")
    submit.add_argument("--form", required=True, help="表单 JSON 文件路径")
    submit.add_argument("--answers", required=True, help="答卷 JSON 文件路径")
    submit.add_argument("--db", required=True, help="SQLite 数据库路径")
    args = parser.parse_args(argv)

    if args.command == "submit":
        return _submit(args)
    parser.error("unknown command")  # pragma: no cover


def _submit(args):
    try:
        form, form_text = load_json_file(args.form)
        answers, answers_text = load_json_file(args.answers)
        fields = validate_form(form)
        if not isinstance(answers, dict):
            raise InvalidInput("answers must be an object")
    except InvalidInput:
        _emit({"error": "invalid_input"})
        return 2

    errors = validate_answers(fields, answers)
    if errors:
        _emit({"valid": False, "errors": errors})
        return 1

    try:
        submission_id = save_submission(args.db, form_text, answers_text)
    except StorageError:
        _emit({"error": "storage_error"})
        return 2

    _emit({"valid": True, "errors": {}, "submissionId": submission_id})
    return 0


if __name__ == "__main__":
    sys.exit(main())
