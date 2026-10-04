"""命令行入口：
python -m forms submit --form f.json --answers a.json --db data.db
python -m forms show --db data.db --id 1
"""

import argparse
import json
import math
import os
import sqlite3
import sys
from decimal import Decimal

VALID_TYPES = ("text", "number", "select")


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
            if ftype != "text":
                _invalid_input()
            max_length = field["maxLength"]
            if isinstance(max_length, bool) or not isinstance(max_length, int) or max_length < 0:
                _invalid_input()
        if "minLength" in field:
            if ftype != "text":
                _invalid_input()
            min_length = field["minLength"]
            if isinstance(min_length, bool) or not isinstance(min_length, int) or min_length < 0:
                _invalid_input()
        if "minLength" in field and "maxLength" in field and min_length > max_length:
            _invalid_input()
        if "minimum" in field:
            if ftype != "number":
                _invalid_input()
            minimum = field["minimum"]
            # bool 是 int 的子类，须先排除；仅接受有限的 int/float
            # （1e400 会解码为 inf，同样拒绝）。
            if isinstance(minimum, bool) or not isinstance(minimum, (int, float)):
                _invalid_input()
            if isinstance(minimum, float) and not math.isfinite(minimum):
                _invalid_input()
        if "maximum" in field:
            if ftype != "number":
                _invalid_input()
            maximum = field["maximum"]
            if isinstance(maximum, bool) or not isinstance(maximum, (int, float)):
                _invalid_input()
            if isinstance(maximum, float) and not math.isfinite(maximum):
                _invalid_input()
        if "minimum" in field and "maximum" in field:
            # 两侧均为 int 时按任意精度整数比较；否则用 Decimal 精确比较，
            # 避免大整数转 float 舍入后误判区间为空或非空。
            if isinstance(minimum, int) and isinstance(maximum, int):
                inverted = minimum > maximum
            else:
                inverted = Decimal(minimum) > Decimal(maximum)
            if inverted:
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


def _below_minimum(value, minimum):
    # 两侧均为 int 时直接按任意精度整数比较；否则用 Decimal 精确比较
    # float 的实际二进制值，避免大整数/小数舍入误判。
    if isinstance(value, int) and isinstance(minimum, int):
        return value < minimum
    return Decimal(value) < Decimal(minimum)


def _above_maximum(value, maximum):
    if isinstance(value, int) and isinstance(maximum, int):
        return value > maximum
    return Decimal(value) > Decimal(maximum)


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
            elif "minimum" in field and _below_minimum(value, field["minimum"]):
                errors[fid] = "min_value"
            elif "maximum" in field and _above_maximum(value, field["maximum"]):
                errors[fid] = "max_value"
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


def _parse_submission_id(raw):
    # 仅接受 ASCII 数字组成的十进制整数（允许前导零），
    # 数值须在 SQLite INTEGER 主键范围 1..2**63-1 内。
    # isdigit 会接受非 ASCII 数字（如全角数字），故先限制 ASCII。
    if not raw or not raw.isascii() or not raw.isdigit():
        _invalid_input()
    value = int(raw)
    if not 1 <= value <= 9223372036854775807:
        _invalid_input()
    return value


def _fetch(db_path, submission_id):
    # 只读查询：数据库不存在时直接报 storage_error，绝不连接创建。
    if not os.path.exists(db_path):
        _storage_error()
    try:
        conn = sqlite3.connect(db_path)
        try:
            cur = conn.execute(
                "SELECT form, answers FROM submissions WHERE id = ?",
                (submission_id,),
            )
            return cur.fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # 文件不可读、不是有效 SQLite、缺表或缺列都落到此处
        _storage_error()


def _show(args):
    submission_id = _parse_submission_id(args.id)
    row = _fetch(args.db, submission_id)
    if row is None:
        _emit({"error": "not_found"}, 1)
    form_text, answers_text = row
    try:
        form = json.loads(form_text, parse_constant=_reject_constant)
        answers = json.loads(answers_text, parse_constant=_reject_constant)
    except (ValueError, TypeError):
        _storage_error()
    if not isinstance(form, dict) or not isinstance(answers, dict):
        _storage_error()
    _emit({"submissionId": submission_id, "form": form, "answers": answers}, 0)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="forms")
    subparsers = parser.add_subparsers(dest="command", required=True)
    submit = subparsers.add_parser("submit", help="校验并保存一次答卷提交")
    submit.add_argument("--form", required=True, help="表单 JSON 文件路径")
    submit.add_argument("--answers", required=True, help="答卷 JSON 文件路径")
    submit.add_argument("--db", required=True, help="SQLite 数据库路径")
    show = subparsers.add_parser("show", help="按编号读取一份历史答卷")
    show.add_argument("--db", required=True, help="SQLite 数据库路径")
    show.add_argument("--id", required=True, help="提交编号")
    args = parser.parse_args(argv)
    if args.command == "submit":
        _submit(args)
    elif args.command == "show":
        _show(args)


if __name__ == "__main__":
    main()
