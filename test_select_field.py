"""select 字段单选值校验的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 JSON 写入临时目录，
分别检查退出码、stdout 解析后的 JSON 与标准库 sqlite3 中的落库结果。
不导入 forms 包内的任何内部函数，不读取或修改既有数据库。

覆盖的公开行为：
- 单选值按字符串原值匹配：填 "A"/"B" 成功，"a"/" A"/"C" 报 choice，
  数字、布尔、数组、对象报 type；
- 选填缺失/null 通过、空字符串报 choice；必填缺失/null/空字符串报 required；
- options 含空字符串时选填空字符串通过、必填空字符串仍报 required；
- required、choice 与 unknown_field 可同时出现在 errors 中，每字段一个码；
- options 缺失/空数组/非数组/含非字符串元素/含重复字符串时，
  退出码 2 且输出恰为 {"error": "invalid_input"}，即使答卷本身也有字段错误；
- 校验失败不创建数据库文件，也不改变已有成功记录；
- 成功提交把含中文、空格与换行的表单与答卷原文逐字符落库。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _select_field(field_id="kind", options=("A", "B"), required=False):
    field = {"id": field_id, "type": "select", "options": list(options)}
    if required:
        field["required"] = True
    return field


class SelectFieldTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _run_raw(self, form_text, answers_text, db_path=None):
        """以给定原文写文件并执行提交。

        每次使用全新的子目录；db_path 为 None 时数据库也位于该子目录
        （尚不存在），传入既有路径则可验证失败不影响已有库。返回内容
        附带输入原文，便于断言信息指出具体输入。
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

    def _payload(self, proc, label):
        """解析 stdout 为 JSON，失败时附带原始输出与输入标签。"""
        raw = proc.stdout.decode("utf-8")
        try:
            return json.loads(raw)
        except ValueError:
            self.fail(
                "用例 %s：stdout 不是合法 JSON：%r（stderr=%r）"
                % (label, raw, proc.stderr.decode("utf-8"))
            )

    def _rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    def _assert_ok(self, label, result, expected_answers=None):
        """退出码 0、valid 为 true、errors 为空、submissionId 为整数，
        且原文（含中文、空格与换行）作为新行落库。"""
        proc, db_path, form_text, answers_text = result
        self.assertEqual(
            proc.returncode,
            0,
            "用例 %s：期望退出码 0，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        payload = self._payload(proc, label)
        self.assertIs(
            payload.get("valid"),
            True,
            "用例 %s：期望 valid=true，实际 %r" % (label, payload),
        )
        self.assertEqual(
            payload.get("errors"),
            {},
            "用例 %s：期望 errors 为空，实际 %r"
            % (label, payload.get("errors")),
        )
        submission_id = payload.get("submissionId")
        self.assertIsInstance(
            submission_id,
            int,
            "用例 %s：submissionId 应为整数，实际 %r"
            % (label, submission_id),
        )
        self.assertTrue(
            os.path.exists(db_path),
            "用例 %s：成功提交后数据库文件应存在" % label,
        )
        rows = self._rows(db_path)
        self.assertTrue(rows, "用例 %s：submissions 中应有记录" % label)
        row_id, row_form, row_answers = rows[-1]
        self.assertEqual(
            row_id,
            submission_id,
            "用例 %s：submissionId %r 与库中主键 %r 不一致"
            % (label, submission_id, row_id),
        )
        # 存储证据：表单与答卷均保留提交原文中的中文、空格与换行。
        self.assertIn("中文", form_text)
        self.assertIn("中文", answers_text)
        self.assertIn(" ", form_text)
        self.assertIn(" ", answers_text)
        self.assertIn("\n", form_text)
        self.assertIn("\n", answers_text)
        self.assertEqual(
            row_form,
            form_text,
            "用例 %s：库中表单原文应与提交原文逐字符一致" % label,
        )
        self.assertEqual(
            row_answers,
            answers_text,
            "用例 %s：库中答卷原文应与提交原文逐字符一致" % label,
        )
        if expected_answers is not None:
            self.assertEqual(
                json.loads(row_answers),
                expected_answers,
                "用例 %s：库中答卷解码值应为 %r" % (label, expected_answers),
            )
        return payload, rows

    def _assert_validation_failed(self, label, result, expected_errors):
        """退出码 1、valid 为 false、错误映射精确匹配、无 submissionId，
        且数据库文件不被创建。"""
        proc, db_path, _, _ = result
        self.assertEqual(
            proc.returncode,
            1,
            "用例 %s：期望退出码 1，实际 %s，stderr=%r"
            % (label, proc.returncode, proc.stderr.decode("utf-8")),
        )
        payload = self._payload(proc, label)
        self.assertEqual(
            payload,
            {"valid": False, "errors": expected_errors},
            "用例 %s：期望 %r，实际 %r"
            % (
                label,
                {"valid": False, "errors": expected_errors},
                payload,
            ),
        )
        self.assertNotIn(
            "submissionId",
            payload,
            "用例 %s：校验失败不应返回 submissionId" % label,
        )
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：校验失败不应创建数据库文件 %s" % (label, db_path),
        )

    def _assert_invalid_input(self, label, result):
        """退出码 2、输出恰为 {"error":"invalid_input"}，不创建数据库。"""
        proc, db_path, _, _ = result
        self.assertEqual(
            proc.returncode,
            2,
            "用例 %s：期望退出码 2，实际 %s，stdout=%r，stderr=%r"
            % (
                label,
                proc.returncode,
                proc.stdout.decode("utf-8"),
                proc.stderr.decode("utf-8"),
            ),
        )
        payload = self._payload(proc, label)
        self.assertEqual(
            payload,
            {"error": "invalid_input"},
            "用例 %s：期望 invalid_input，实际 %r" % (label, payload),
        )
        self.assertFalse(
            os.path.exists(db_path),
            "用例 %s：输入不合法不应创建数据库文件 %s" % (label, db_path),
        )

    def _pretty_raw(self, kind_literal, note_literal=None):
        """构造刻意含中文、空格与换行的提交原文，用于核对原样存储。"""
        if note_literal is None:
            note_literal = '"中文 备注\\n第二行"'
        form_text = (
            "{\n"
            '  "title": "中文 调查表",\n'
            '  "fields": [\n'
            '    { "id": "note", "type": "text" },\n'
            '    { "id": "kind", "type": "select", "options": ["A", "B"] }\n'
            "  ]\n"
            "}\n"
        )
        answers_text = (
            "{\n"
            '  "note": %s,\n'
            '  "kind": %s  \n'
            "}\n" % (note_literal, kind_literal)
        )
        return form_text, answers_text

    # -- 原值匹配：填选项原文成功，且含中文的原文逐字符落库 ----------------

    def test_exact_option_strings_submit_and_persist(self):
        for label, value in (("option-A", '"A"'), ("option-B", '"B"')):
            with self.subTest(value=label):
                form_text, answers_text = self._pretty_raw(value)
                result = self._run_raw(form_text, answers_text)
                self._assert_ok(
                    label,
                    result,
                    expected_answers={
                        "note": "中文 备注\n第二行",
                        "kind": value.strip('"'),
                    },
                )

    # -- 大小写、空白与未列出的值报 choice --------------------------------

    def test_near_miss_strings_report_choice(self):
        form = {"fields": [_select_field()]}
        cases = [
            ("lowercase-a", "a"),
            ("leading-space-A", " A"),
            ("trailing-space-A", "A "),
            ("unlisted-C", "C"),
            ("empty-string", ""),
        ]
        for label, value in cases:
            with self.subTest(value=label):
                result = self._run(form, {"kind": value})
                self._assert_validation_failed(
                    label, result, {"kind": "choice"}
                )

    # -- 非字符串值报 type -------------------------------------------------

    def test_non_string_values_report_type(self):
        form = {"fields": [_select_field()]}
        cases = [
            ("integer", 1),
            ("decimal", 1.5),
            ("boolean-true", True),
            ("boolean-false", False),
            ("array", ["A"]),
            ("object", {"value": "A"}),
        ]
        for label, bad_value in cases:
            with self.subTest(value=label):
                result = self._run(form, {"kind": bad_value})
                self._assert_validation_failed(
                    label, result, {"kind": "type"}
                )

    # -- 选填：缺失与 null 通过 ---------------------------------------------

    def test_optional_missing_and_null_pass(self):
        for label, answers_text in (
            ("missing", '{\n  "note": "中文 备注\\n第二行"\n}\n'),
            (
                "null",
                '{\n  "note": "中文 备注\\n第二行",\n  "kind": null\n}\n',
            ),
        ):
            with self.subTest(case=label):
                form_text, _ = self._pretty_raw('"A"')
                result = self._run_raw(form_text, answers_text)
                expected_answers = {"note": "中文 备注\n第二行"}
                if label == "null":
                    expected_answers["kind"] = None
                self._assert_ok(
                    label, result, expected_answers=expected_answers
                )

    # -- 必填：缺失/null/空字符串报 required，越界值仍报 choice -----------

    def test_required_missing_null_empty_report_required(self):
        form = {"fields": [_select_field(required=True)]}
        cases = [
            ("required-missing", {}),
            ("required-null", {"kind": None}),
            ("required-empty-string", {"kind": ""}),
        ]
        for label, answers in cases:
            with self.subTest(case=label):
                self._assert_validation_failed(
                    label, self._run(form, answers), {"kind": "required"}
                )

    def test_required_listed_value_passes_and_unlisted_reports_choice(self):
        form = {"fields": [_select_field(required=True)]}
        ok = self._run(form, {"kind": "B"})
        proc, db_path, _, _ = ok
        self.assertEqual(
            proc.returncode,
            0,
            "必填合法值应成功，stderr=%r" % proc.stderr.decode("utf-8"),
        )
        payload = self._payload(proc, "required-listed")
        self.assertEqual(
            payload.get("valid"),
            True,
            "必填合法值应 valid=true，实际 %r" % payload,
        )
        self.assertEqual(payload.get("errors"), {})
        self.assertIsInstance(payload.get("submissionId"), int)
        rows = self._rows(db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][0], payload["submissionId"])

        bad = self._run(form, {"kind": "C"})
        self._assert_validation_failed(
            "required-unlisted", bad, {"kind": "choice"}
        )

    # -- options 含空字符串：选填空字符串通过，必填仍报 required ----------

    def test_empty_string_option(self):
        optional_form = {"fields": [_select_field(options=("", "A"))]}
        ok = self._run(optional_form, {"kind": ""})
        proc, db_path, _, _ = ok
        self.assertEqual(
            proc.returncode,
            0,
            "选填且空字符串在 options 中应成功，stderr=%r"
            % proc.stderr.decode("utf-8"),
        )
        payload = self._payload(proc, "optional-empty-option")
        self.assertEqual(payload.get("valid"), True)
        self.assertEqual(payload.get("errors"), {})
        rows = self._rows(db_path)
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0][2]), {"kind": ""})

        required_form = {
            "fields": [_select_field(options=("", "A"), required=True)]
        }
        bad = self._run(required_form, {"kind": ""})
        self._assert_validation_failed(
            "required-empty-option", bad, {"kind": "required"}
        )

    # -- 多字段：required、choice 与 unknown_field 同时给出 ----------------

    def test_required_choice_and_unknown_field_reported_together(self):
        # 必填 a 缺失 -> required；选填 b 填未列出值 -> choice；
        # 答卷含未知键 extra -> unknown_field。三者必须同时出现，
        # 且每个字段仅有一个错误码。
        form = {
            "fields": [
                _select_field("a", required=True),
                _select_field("b", options=("X", "Y")),
            ]
        }
        result = self._run(form, {"b": "Z", "extra": 1})
        self._assert_validation_failed(
            "a-missing-b-unlisted-extra",
            result,
            {"a": "required", "b": "choice", "extra": "unknown_field"},
        )

    # -- options 定义不合法：退出码 2，即使答卷也有字段错误 ----------------

    def test_invalid_options_definitions_exit_invalid_input(self):
        # (标签, 字段定义中 options 键的值；_MISSING 表示省略该键)
        missing = object()
        cases = [
            ("options-missing", missing),
            ("options-empty-array", []),
            ("options-not-array-string", "AB"),
            ("options-not-array-object", {"0": "A"}),
            ("options-not-array-number", 2),
            ("options-null", None),
            ("options-non-string-int", [1, "A"]),
            ("options-non-string-bool", [True, "A"]),
            ("options-non-string-null", [None, "A"]),
            ("options-non-string-array", [["A"]]),
            ("options-duplicate", ["A", "A"]),
        ]
        # 答卷同时带有字段错误（未列出值与未知键）：输入不合法必须优先，
        # 结果仍是退出码 2 的 invalid_input，而不是退出码 1 的字段错误。
        bad_answers = {"kind": "C", "extra": 1}
        for label, options in cases:
            with self.subTest(definition=label):
                field = {"id": "kind", "type": "select"}
                if options is not missing:
                    field["options"] = options
                form = {"fields": [field]}
                result = self._run(form, bad_answers)
                self._assert_invalid_input(label, result)

    # -- 失败不影响既有库 -------------------------------------------------

    def test_failures_leave_existing_database_unchanged(self):
        # 先写入一条成功记录作为基线（含中文、空格与换行的原文）。
        form_text, answers_text = self._pretty_raw('"A"')
        seeded = self._run_raw(form_text, answers_text)
        proc, db_path, _, _ = seeded
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        seeded_rows = self._rows(db_path)
        self.assertEqual(len(seeded_rows), 1)

        valid_form = (
            '{"fields": [{"id": "kind", "type": "select", '
            '"options": ["A", "B"], "required": true}]}'
        )
        invalid_form = (
            '{"fields": [{"id": "kind", "type": "select", '
            '"options": ["A", "A"]}]}'
        )
        # 全部本次失败用例在同一既有库上执行：退出码各就各位，
        # 记录数、主键与每条记录原文均不得增加或改变。
        failing = [
            ("required-missing", valid_form, "{}\n", 1),
            ("required-null", valid_form, '{"kind": null}', 1),
            ("required-empty", valid_form, '{"kind": ""}', 1),
            ("choice-unlisted", valid_form, '{"kind": "C"}', 1),
            ("choice-lowercase", valid_form, '{"kind": "a"}', 1),
            ("type-number", valid_form, '{"kind": 1}', 1),
            ("type-bool", valid_form, '{"kind": true}', 1),
            ("type-array", valid_form, '{"kind": ["A"]}', 1),
            ("type-object", valid_form, '{"kind": {"v": "A"}}', 1),
            ("unknown-field", valid_form, '{"kind": "A", "extra": 1}', 1),
            ("invalid-form", invalid_form, '{"kind": "A"}', 2),
        ]
        for label, raw_form, raw_answers, expected_code in failing:
            with self.subTest(case=label):
                proc, _, _, _ = self._run_raw(
                    raw_form, raw_answers, db_path=db_path
                )
                self.assertEqual(
                    proc.returncode,
                    expected_code,
                    "用例 %s：期望退出码 %s，实际 %s，stdout=%r"
                    % (
                        label,
                        expected_code,
                        proc.returncode,
                        proc.stdout.decode("utf-8"),
                    ),
                )
                rows = self._rows(db_path)
                self.assertEqual(
                    rows,
                    seeded_rows,
                    "用例 %s：失败提交后数据库记录发生变化" % label,
                )


if __name__ == "__main__":
    unittest.main()
