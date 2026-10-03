"""命令行入口：python -m forms submit --form f.json --answers a.json --db data.db"""

import argparse
import json
import math
import sqlite3
import sys

VALID_TYPES = ("text", "number", "select")


def _is_nonneg_int(value):
    """长度配置只接受大于等于零的 JSON 整数（布尔不算整数）。"""
    return not isinstance(value, bool) and isinstance(value, int) and value >= 0


def _emit(payload, code):
    json.dump(payload, sys.stdout, ensure_ascii=False)
    sys.stdout.write("\n")
    sys.exit(code)


def _invalid_input():
    _emit({"error": "invalid_input"}, 2)


def _storage_error():
    _emit({"error": "storage_error"}, 2)


def _reject_constant(value):
    raise ValueError("invalid JSON constant: %s" % value)


def _load_json(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, UnicodeError):
        _invalid_input()
    try:
        return text, json.loads(text, parse_constant=_reject_constant)
    except ValueError:
        _invalid_input()


def _check_form(form):
    """校验表单结构，不合法时直接以 invalid_input 退出，否则返回 fields 列表。"""
    if not isinstance(form, dict):
        _invalid_input()
    fields = form.get("fields")
    if not isinstance(fields, list) or not fields:
        _invalid_input()
    seen = set()
    for field in fields:
        if not isinstance(field, dict):
            _invalid_input()
        fid = field.get("id")
        if not isinstance(fid, str) or not fid or fid in seen:
            _invalid_input()
        seen.add(fid)
        ftype = field.get("type")
        if ftype not in VALID_TYPES:
            _invalid_input()
        if not isinstance(field.get("required", False), bool):
            _invalid_input()
        if "maxLength" in field:
            if ftype != "text" or not _is_nonneg_int(field["maxLength"]):
                _invalid_input()
        if "minLength" in field:
            if ftype != "text" or not _is_nonneg_int(field["minLength"]):
                _invalid_input()
        if "minLength" in field and "maxLength" in field:
            if field["minLength"] > field["maxLength"]:
                _invalid_input()
        if ftype == "select":
            options = field.get("options")
            if (
                not isinstance(options, list)
                or not options
                or any(not isinstance(o, str) for o in options)
                or len(set(options)) != len(options)
            ):
                _invalid_input()
    return fields


def _validate(fields, answers):
    """汇总全部字段错误，每字段一个码，必填判断优先。"""
    errors = {}
    by_id = {f["id"]: f for f in fields}
    for key in answers:
        if key not in by_id:
            errors[key] = "unknown_field"
    for field in fields:
        fid = field["id"]
        missing = fid not in answers
        value = answers.get(fid)
        if field.get("required", False) and (missing or value is None or value == ""):
            errors[fid] = "required"
            continue
        if missing or value is None:
            continue
        ftype = field["type"]
        if ftype == "text":
            if not isinstance(value, str):
                errors[fid] = "type"
            elif "maxLength" in field and len(value) > field["maxLength"]:
                errors[fid] = "max_length"
            elif "minLength" in field and len(value) < field["minLength"]:
                errors[fid] = "min_length"
        elif ftype == "number":
            # int 为任意精度，必然有限；仅 float 需排除 inf/nan（如 1e400 解析为 inf）
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                errors[fid] = "type"
            elif isinstance(value, float) and not math.isfinite(value):
                errors[fid] = "type"
        elif ftype == "select":
            if not isinstance(value, str):
                errors[fid] = "type"
            elif value not in field["options"]:
                errors[fid] = "choice"
    return errors


def _save(db_path, form_text, answers_text):
    try:
        conn = sqlite3.connect(db_path)
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS submissions ("
                "id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "form TEXT NOT NULL, "
                "answers TEXT NOT NULL)"
            )
            cur = conn.execute(
                "INSERT INTO submissions (form, answers) VALUES (?, ?)",
                (form_text, answers_text),
            )
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except sqlite3.Error:
        _storage_error()


def _submit(args):
    form_text, form = _load_json(args.form)
    answers_text, answers = _load_json(args.answers)
    fields = _check_form(form)
    if not isinstance(answers, dict):
        _invalid_input()
    errors = _validate(fields, answers)
    if errors:
        _emit({"valid": False, "errors": errors}, 1)
    submission_id = _save(args.db, form_text, answers_text)
    _emit({"valid": True, "errors": {}, "submissionId": submission_id}, 0)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="forms")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit", help="校验并保存一次答卷提交")
    submit.add_argument("--form", required=True, help="表单 JSON 文件路径")
    submit.add_argument("--answers", required=True, help="答卷 JSON 文件路径")
    submit.add_argument("--db", required=True, help="SQLite 数据库路径")
    args = parser.parse_args(argv)
    if args.command == "submit":
        _submit(args)


if __name__ == "__main__":
    main()
