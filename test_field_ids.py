"""字段 id 合法性与唯一性的回归测试。

只通过公开入口 ``python -m forms submit`` 执行：合成 JSON 写入临时目录，
检查退出码、stdout 解析后的 JSON 与 stderr，并用标准库 sqlite3 核对落库
结果。不导入 forms 包内的任何内部函数，不读取或修改既有数据库。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))

INVALID_INPUT_LINE = '{"error": "invalid_input"}\n'


class FieldIdTests(unittest.TestCase):
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

    def _assert_invalid_input(self, result):
        """退出码 2、stdout 恰为一个 invalid_input 对象、stderr 为空、不建库。"""
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(proc.stderr, b"")
        self.assertEqual(proc.stdout.decode("utf-8"), INVALID_INPUT_LINE)
        self.assertEqual(self._payload(proc), {"error": "invalid_input"})
        self.assertFalse(os.path.exists(db_path))

    def _read_rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    # -- 非法 id：缺失、空串、非字符串 ------------------------------------

    def test_missing_id_rejected(self):
        # 字段没有 id 键，其余属性合法，答卷为合法 JSON 对象。
        form = {"fields": [{"type": "text"}]}
        self._assert_invalid_input(self._run(form, {"name": "甲"}))

    def test_non_string_or_empty_ids_rejected(self):
        # id 为空字符串、null、数字、布尔、数组或对象时同样拒绝；
        # 字段其余属性保持合法，答卷本身合法。
        bad_ids = ["", None, 0, 1.5, True, False, ["name"], {"id": "name"}]
        for bad_id in bad_ids:
            with self.subTest(bad_id=bad_id):
                form = {"fields": [{"id": bad_id, "type": "text"}]}
                self._assert_invalid_input(self._run(form, {"name": "甲"}))

    def test_invalid_id_among_valid_fields_rejected(self):
        # 仅一个字段 id 非法，其余字段完全合法，整份表单仍被拒绝。
        form = {
            "fields": [
                {"id": "name", "type": "text"},
                {"id": None, "type": "number"},
                {"id": "color", "type": "select", "options": ["红", "蓝"]},
            ]
        }
        answers = {"name": "甲", "color": "红"}
        self._assert_invalid_input(self._run(form, answers))

    # -- 重复 id ----------------------------------------------------------

    def test_duplicate_id_rejected_even_with_different_types(self):
        # 两个字段 id 相同但 type 不同，仍应被拒绝。
        form = {
            "fields": [
                {"id": "name", "type": "text"},
                {"id": "name", "type": "number"},
            ]
        }
        self._assert_invalid_input(self._run(form, {"name": "甲"}))

    def test_duplicate_id_not_overridden_by_answer_errors(self):
        # 重复 id 同时遇到必填遗漏与未知答卷字段，仍返回 invalid_input，
        # 不能被答卷校验结果（退出码 1）覆盖。
        form = {
            "fields": [
                {"id": "name", "type": "text", "required": True},
                {"id": "name", "type": "number"},
            ]
        }
        # name 缺失（required）、extra 未定义（unknown_field）。
        self._assert_invalid_input(self._run(form, {"extra": "x"}))

    def test_invalid_id_not_overridden_by_answer_errors(self):
        # 非法 id 与答卷字段错误并存时，同样只报 invalid_input。
        form = {
            "fields": [
                {"id": "", "type": "text"},
                {"id": "req", "type": "text", "required": True},
            ]
        }
        self._assert_invalid_input(self._run(form, {"extra": "x"}))

    # -- 非法 id 不影响既有库 ---------------------------------------------

    def test_invalid_ids_leave_existing_database_unchanged(self):
        # 先用合法表单成功保存一份答卷作为种子记录。
        seed_form = {"fields": [{"id": "name", "type": "text"}]}
        seeded = self._run(seed_form, {"name": "甲"})
        proc, db_path, _, _ = seeded
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        seeded_rows = self._read_rows(db_path)
        self.assertEqual(len(seeded_rows), 1)

        bad_forms = [
            {"fields": [{"type": "text"}]},
            {"fields": [{"id": "", "type": "text"}]},
            {"fields": [{"id": None, "type": "text"}]},
            {"fields": [{"id": 1, "type": "text"}]},
            {"fields": [{"id": True, "type": "text"}]},
            {"fields": [{"id": ["name"], "type": "text"}]},
            {"fields": [{"id": {"id": "name"}, "type": "text"}]},
            {"fields": [
                {"id": "name", "type": "text"},
                {"id": "name", "type": "number"},
            ]},
        ]
        for bad_form in bad_forms:
            with self.subTest(bad_form=bad_form):
                result = self._run(bad_form, {"name": "乙"}, db_path=db_path)
                proc, _, _, _ = result
                self.assertEqual(proc.returncode, 2)
                self.assertEqual(proc.stderr, b"")
                self.assertEqual(
                    proc.stdout.decode("utf-8"), INVALID_INPUT_LINE
                )
                # 记录数、编号及两份原文均保持不变。
                self.assertEqual(self._read_rows(db_path), seeded_rows)

    # -- 合法对照：id 按字符串原值区分 -------------------------------------

    def test_similar_ids_are_distinct_by_exact_string_value(self):
        # name、Name、" name " 为三个不同字段：不做大小写转换或去除空格。
        form = {
            "fields": [
                {"id": "name", "type": "text"},
                {"id": "Name", "type": "text"},
                {"id": " name ", "type": "text"},
            ]
        }
        # 刻意使用带多余空白与转义写法的原文，验证逐字符保留。
        form_text = json.dumps(form, ensure_ascii=False, indent=2)
        answers_text = '{ "name": "甲", "Name": "乙", " name ": " 丙 " }'
        result = self._run_raw(form_text, answers_text)
        proc, db_path, _, _ = result
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        self.assertEqual(proc.stderr, b"")
        payload = self._payload(proc)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], {})
        submission_id = payload["submissionId"]
        self.assertIsInstance(submission_id, int)

        # 数据库只追加一条记录，编号与返回值一致，原文逐字符保留。
        rows = self._read_rows(db_path)
        self.assertEqual(len(rows), 1)
        row_id, stored_form, stored_answers = rows[0]
        self.assertEqual(row_id, submission_id)
        self.assertEqual(stored_form, form_text)
        self.assertEqual(stored_answers, answers_text)


if __name__ == "__main__":
    unittest.main()
