"""text 字段 maxLength 约束的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 JSON 写入临时目录，
检查退出码与 stdout 解析后的 JSON，并用标准库 sqlite3 核对落库结果。
不导入 forms 包内的任何内部函数，不读取或修改既有数据库。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _text_field(max_length=None, required=False):
    field = {"id": "note", "type": "text"}
    if required:
        field["required"] = True
    if max_length is not None:
        field["maxLength"] = max_length
    return field


class MaxLengthTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _run_raw(self, form_text, answers_text, db_path=None):
        """以给定原文写文件并执行提交。

        每次使用全新的子目录；db_path 为 None 时数据库也位于该子目录
        （尚不存在），传入既有路径则可验证失败不影响已有库。
        """
        run_dir = tempfile.mkdtemp(dir=self.tmp)
        form_path = os.path.join(run_dir, "form.json")
        answers_path = os.path.join(run_dir, "answers.json")
        if db_path is None:
            db_path = os.path.join(run_dir, "data.db")
        with open(form_path, "w", encoding="utf-8") as fh:
            fh.write(form_text)
        with open(answers_path, "w", encoding="utf-8") as fh:
            fh.write(answers_text)
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "forms",
                "submit",
                "--form",
                form_path,
                "--answers",
                answers_path,
                "--db",
                db_path,
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
        )
        return proc, db_path, form_text, answers_text

    def _run(self, form, answers, db_path=None):
        return self._run_raw(
            json.dumps(form, ensure_ascii=False),
            json.dumps(answers, ensure_ascii=False),
            db_path=db_path,
        )

    def _payload(self, proc):
        return json.loads(proc.stdout.decode("utf-8"))

    def _assert_ok(self, result):
        """退出码 0、valid 为 true、errors 为空，且原文按新行落库。"""
        proc, db_path, form_text, answers_text = result
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        payload = self._payload(proc)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], {})
        submission_id = payload["submissionId"]
        self.assertIsInstance(submission_id, int)
        self.assertTrue(os.path.exists(db_path))
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()
        self.assertTrue(rows)
        self.assertEqual(rows[-1][0], submission_id)
        self.assertEqual(rows[-1][1], form_text)
        self.assertEqual(rows[-1][2], answers_text)
        return payload, rows

    def _assert_validation_failed(self, result, expected_errors):
        """退出码 1、错误码精确匹配，且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 1, proc.stderr.decode("utf-8"))
        self.assertEqual(
            self._payload(proc),
            {"valid": False, "errors": expected_errors},
        )
        self.assertFalse(os.path.exists(db_path))

    def _assert_invalid_input(self, result):
        """退出码 2、invalid_input，且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8"))
        self.assertEqual(self._payload(proc), {"error": "invalid_input"})
        self.assertFalse(os.path.exists(db_path))

    # -- 边界值：零上限、恰好达到、超出一个码点 --------------------------

    def test_zero_limit_empty_string_passes(self):
        result = self._run(
            {"fields": [_text_field(max_length=0)]}, {"note": ""}
        )
        self._assert_ok(result)

    def test_zero_limit_one_code_point_fails(self):
        result = self._run(
            {"fields": [_text_field(max_length=0)]}, {"note": "A"}
        )
        self._assert_validation_failed(result, {"note": "max_length"})

    def test_exactly_at_limit_passes(self):
        # “中🙂”为两个 Unicode 码点（UTF-8 下共 7 字节）。
        result = self._run(
            {"fields": [_text_field(max_length=2)]}, {"note": "中🙂"}
        )
        self._assert_ok(result)

    def test_one_code_point_over_limit_fails(self):
        result = self._run(
            {"fields": [_text_field(max_length=2)]}, {"note": "中🙂A"}
        )
        self._assert_validation_failed(result, {"note": "max_length"})

    # -- 计量单位：解码后的码点，而非转义文本、UTF-8 字节或 UTF-16 单元 --

    def test_length_counts_decoded_code_points(self):
        # 每项：答卷 JSON 原文片段（刻意保留转义写法）、上限、是否应通过。
        cases = [
            # "\n" 在 JSON 源文本中占两字符，解码后换行只算 1 个码点。
            (r'{"note": "a\nb"}', 3, True),
            (r'{"note": "a\nb"}', 2, False),
            # 代理对转义源文本很长，解码为 🙂 一个码点（4 个 UTF-8 字节、
            # 2 个 UTF-16 单元）。
            (r'{"note": "🙂"}', 1, True),
            # 空格按 1 个码点计量：中 + 空格 + 文 = 3。
            ('{"note": "中 文"}', 3, True),
            # 中文 3 字节、emoji 4 字节，合计 7 字节但只有 2 个码点。
            ('{"note": "中🙂"}', 2, True),
            # 两个 emoji 为 2 个码点，上限 1 应失败。
            ('{"note": "🙂🙂"}', 1, False),
        ]
        for raw_answers, limit, should_pass in cases:
            with self.subTest(raw=raw_answers, limit=limit):
                form_text = json.dumps(
                    {"fields": [_text_field(max_length=limit)]},
                    ensure_ascii=False,
                )
                result = self._run_raw(form_text, raw_answers)
                if should_pass:
                    self._assert_ok(result)
                else:
                    self._assert_validation_failed(
                        result, {"note": "max_length"}
                    )

    def test_long_text_without_max_length_submits(self):
        long_text = "字" * 10000
        result = self._run(
            {"fields": [_text_field()]}, {"note": long_text}
        )
        self._assert_ok(result)

    # -- 与必填、类型判断的交互 ------------------------------------------

    def test_optional_missing_null_and_empty_string_at_zero_limit(self):
        form = {"fields": [_text_field(max_length=0)]}
        for answers in ({}, {"note": None}, {"note": ""}):
            with self.subTest(answers=answers):
                self._assert_ok(self._run(form, answers))

    def test_required_takes_precedence_over_length(self):
        # 即便上限为 0，必填问题仍报 required，而非 max_length。
        form = {"fields": [_text_field(max_length=0, required=True)]}
        for answers in ({}, {"note": None}, {"note": ""}):
            with self.subTest(answers=answers):
                self._assert_validation_failed(
                    self._run(form, answers), {"note": "required"}
                )

    def test_required_non_string_reports_type_not_max_length(self):
        form = {"fields": [_text_field(max_length=0, required=True)]}
        for bad_value in (123, ["x"], True):
            with self.subTest(bad_value=bad_value):
                self._assert_validation_failed(
                    self._run(form, {"note": bad_value}),
                    {"note": "type"},
                )

    def test_optional_non_string_reports_type_not_max_length(self):
        form = {"fields": [_text_field(max_length=0)]}
        for bad_value in (123, ["x"], False):
            with self.subTest(bad_value=bad_value):
                self._assert_validation_failed(
                    self._run(form, {"note": bad_value}),
                    {"note": "type"},
                )

    def test_required_overlong_string_still_reports_max_length(self):
        form = {"fields": [_text_field(max_length=2, required=True)]}
        result = self._run(form, {"note": "abc"})
        self._assert_validation_failed(result, {"note": "max_length"})

    # -- 非法 maxLength 定义 ---------------------------------------------

    def test_invalid_max_length_values_reject_form(self):
        # 其余配置与答卷均合法，失败原因只能来自 maxLength 取值。
        for bad in (-1, True, "2", None, 2.5):
            with self.subTest(bad=bad):
                form = {"fields": [{"id": "note", "type": "text",
                                    "maxLength": bad}]}
                self._assert_invalid_input(self._run(form, {}))

    def test_max_length_on_number_or_select_rejects_form(self):
        number_form = {
            "fields": [{"id": "n", "type": "number", "maxLength": 5}]
        }
        select_form = {
            "fields": [
                {"id": "s", "type": "select",
                 "options": ["A"], "maxLength": 5}
            ]
        }
        self._assert_invalid_input(self._run(number_form, {}))
        self._assert_invalid_input(self._run(select_form, {}))

    # -- 失败不影响既有库 -------------------------------------------------

    def test_failure_leaves_existing_database_unchanged(self):
        form = {"fields": [_text_field(max_length=2)]}
        seeded = self._run(form, {"note": "中🙂"})
        _, db_path, _, _ = seeded
        _, seeded_rows = self._assert_ok(seeded)

        # 长度校验失败：退出码 1，记录数与内容不变。
        over = self._run(form, {"note": "中🙂A"}, db_path=db_path)
        proc, _, _, _ = over
        self.assertEqual(proc.returncode, 1)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, seeded_rows)

        # 表单定义非法：退出码 2，记录数与内容同样不变。
        bad_form = {"fields": [{"id": "note", "type": "text",
                                "maxLength": -1}]}
        invalid = self._run(bad_form, {}, db_path=db_path)
        proc2, _, _, _ = invalid
        self.assertEqual(proc2.returncode, 2)
        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()
        self.assertEqual(rows, seeded_rows)


if __name__ == "__main__":
    unittest.main()
