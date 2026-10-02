"""表单校验与提交存储核心逻辑。"""

import json
import math
import sqlite3

FIELD_TYPES = ("text", "number", "select")

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    form TEXT NOT NULL,
    answers TEXT NOT NULL
)
"""


class InvalidInput(Exception):
    """输入文件不可读、JSON 非法或结构不符。"""


class StorageError(Exception):
    """数据库打开或保存失败。"""


def _reject_constant(value):
    raise ValueError("non-finite JSON number: %s" % value)


def load_json_file(path):
    """读取 UTF-8 JSON 文件，返回 (解析结果, 原始文本)。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            text = f.read()
    except (OSError, UnicodeDecodeError) as exc:
        raise InvalidInput(str(exc))
    try:
        data = json.loads(text, parse_constant=_reject_constant)
    except ValueError as exc:
        raise InvalidInput(str(exc))
    return data, text


def validate_form(form):
    """校验表单结构，返回字段列表；不合法时抛出 InvalidInput。"""
    if not isinstance(form, dict):
        raise InvalidInput("form must be an object")
    fields = form.get("fields")
    if not isinstance(fields, list) or not fields:
        raise InvalidInput("fields must be a non-empty array")
    seen = set()
    for field in fields:
        if not isinstance(field, dict):
            raise InvalidInput("field must be an object")
        fid = field.get("id")
        if not isinstance(fid, str) or not fid:
            raise InvalidInput("field id must be a non-empty string")
        if fid in seen:
            raise InvalidInput("duplicate field id: %s" % fid)
        seen.add(fid)
        ftype = field.get("type")
        if ftype not in FIELD_TYPES:
            raise InvalidInput("unknown field type: %r" % (ftype,))
        if not isinstance(field.get("required", False), bool):
            raise InvalidInput("required must be a boolean")
        if ftype == "select":
            options = field.get("options")
            if not isinstance(options, list) or not options:
                raise InvalidInput("options must be a non-empty array")
            if any(not isinstance(o, str) for o in options):
                raise InvalidInput("options must be strings")
            if len(set(options)) != len(options):
                raise InvalidInput("options must be unique")
    return fields


def validate_answers(fields, answers):
    """按字段校验答卷，返回 {字段 id: 错误码}；全部通过时为空 dict。"""
    errors = {}
    known_ids = set()
    for field in fields:
        fid = field["id"]
        known_ids.add(fid)
        required = field.get("required", False)
        if fid not in answers or answers[fid] is None:
            if required:
                errors[fid] = "required"
            continue
        value = answers[fid]
        if required and isinstance(value, str) and value == "":
            errors[fid] = "required"
            continue
        ftype = field["type"]
        if ftype == "text":
            if not isinstance(value, str):
                errors[fid] = "type"
        elif ftype == "number":
            if (isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)):
                errors[fid] = "type"
        elif ftype == "select":
            if not isinstance(value, str):
                errors[fid] = "type"
            elif value not in field["options"]:
                errors[fid] = "choice"
    for key in answers:
        if key not in known_ids:
            errors[key] = "unknown_field"
    return errors


def save_submission(db_path, form_text, answers_text):
    """把原表单和原答卷追加保存到 SQLite，返回提交 id。"""
    try:
        conn = sqlite3.connect(db_path)
        try:
            with conn:
                conn.execute(SCHEMA)
                cur = conn.execute(
                    "INSERT INTO submissions (form, answers) VALUES (?, ?)",
                    (form_text, answers_text),
                )
            return cur.lastrowid
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise StorageError(str(exc))
