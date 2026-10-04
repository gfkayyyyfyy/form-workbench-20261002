"""show 读取历史答卷的回归测试。

只通过公开入口 ``python -m forms submit`` / ``python -m forms show`` 执行：
合成 JSON 写入独立临时目录与独立临时 SQLite，验证表单文件修订后旧答卷仍按
提交时的内容读出、读取不改动记录，以及 not_found / invalid_input /
storage_error 的退出码与 JSON 协议。

不导入 forms 包内的任何内部函数，仅依赖 Python 标准库；
可用 ``python -m unittest discover`` 执行，可独立重复运行。
"""

import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.abspath(__file__))


def _note_form(field_type):
    return {"fields": [{"id": "note", "type": field_type}]}


class ShowHistoryTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = self._tmp.name
        self.form_path = os.path.join(self.tmp, "form.json")
        self._answer_seq = 0

    def tearDown(self):
        self._tmp.cleanup()

    # -- 辅助 ------------------------------------------------------------

    def _write_json(self, path, value):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(value, fh, ensure_ascii=False)

    def _env(self):
        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env["PYTHONUTF8"] = "1"
        return env

    def _submit(self, form, answers, db_path):
        self._write_json(self.form_path, form)
        self._answer_seq += 1
        answers_path = os.path.join(self.tmp, "answers_%d.json" % self._answer_seq)
        self._write_json(answers_path, answers)
        proc = subprocess.run(
            [
                sys.executable,
                "-m",
                "forms",
                "submit",
                "--form",
                self.form_path,
                "--answers",
                answers_path,
                "--db",
                db_path,
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
        )
        return proc

    def _show(self, db_path, raw_id):
        return subprocess.run(
            [
                sys.executable,
                "-m",
                "forms",
                "show",
                "--db",
                db_path,
                "--id",
                str(raw_id),
            ],
            cwd=REPO_ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env(),
        )

    def _single_payload(self, proc):
        """stdout 必须恰好包含一份 JSON，前后不混有任何说明文字。"""
        text = proc.stdout.decode("utf-8")
        self.assertEqual(proc.stderr, b"", proc.stderr.decode("utf-8"))
        value, end = json.JSONDecoder().raw_decode(text)
        self.assertEqual(text[end:].strip(), "", text)
        return value

    def _submit_ok(self, form, answers, db_path):
        proc = self._submit(form, answers, db_path)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        payload = self._single_payload(proc)
        self.assertTrue(payload["valid"])
        self.assertEqual(payload["errors"], {})
        submission_id = payload["submissionId"]
        self.assertIsInstance(submission_id, int)
        self.assertNotIsInstance(submission_id, bool)
        return submission_id

    def _show_ok(self, db_path, raw_id):
        proc = self._show(db_path, raw_id)
        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8"))
        payload = self._single_payload(proc)
        self.assertEqual(
            set(payload), {"submissionId", "form", "answers"}
        )
        return payload

    def _dump_rows(self, db_path):
        with sqlite3.connect(db_path) as conn:
            return conn.execute(
                "SELECT id, form, answers FROM submissions ORDER BY id"
            ).fetchall()

    # -- 主场景：表单修订后旧答卷仍可读，且读取不改记录 -------------------

    def test_revised_form_keeps_old_submission_readable(self):
        db_path = os.path.join(self.tmp, "data.db")

        # 第一次：仅含 note 文本字段的表单，答卷含换行原字符串。
        form_text_v1 = _note_form("text")
        answers_v1 = {"note": "甲\n乙"}
        id1 = self._submit_ok(form_text_v1, answers_v1, db_path)
        with open(self.form_path, encoding="utf-8") as fh:
            form_text_raw = fh.read()

        # 修订同一表单文件：note 改为数字字段，再提交数字答卷。
        form_text_v2 = _note_form("number")
        answers_v2 = {"note": 7}
        id2 = self._submit_ok(form_text_v2, answers_v2, db_path)
        self.assertNotEqual(id1, id2)

        # 读取前快照：全部编号、表单原文、答卷原文。
        rows_before = self._dump_rows(db_path)
        self.assertEqual(
            [row[0] for row in rows_before], sorted([id1, id2])
        )

        payload1 = self._show_ok(db_path, id1)
        payload2 = self._show_ok(db_path, id2)

        # 旧答卷返回提交时的文本定义与含换行的原字符串，
        # 新答卷返回数字定义与数字 7（以解码后的对象核对，不要求原排版）。
        self.assertEqual(payload1["submissionId"], id1)
        self.assertEqual(payload1["form"], form_text_v1)
        self.assertEqual(payload1["answers"], answers_v1)
        self.assertEqual(payload1["answers"]["note"], "甲\n乙")

        self.assertEqual(payload2["submissionId"], id2)
        self.assertEqual(payload2["form"], form_text_v2)
        self.assertEqual(payload2["answers"], answers_v2)
        # JSON 数字 7 必须以整数而非字符串/布尔读出。
        self.assertIsInstance(payload2["answers"]["note"], int)
        self.assertNotIsInstance(payload2["answers"]["note"], bool)
        self.assertEqual(payload2["answers"]["note"], 7)

        # 表单文件已修订，但第一份记录的表单定义不受影响。
        self.assertNotEqual(payload1["form"], payload2["form"])

        # 删除全部输入文件后，第一份仍能从库中读出同样内容。
        for name in os.listdir(self.tmp):
            if name.endswith(".json"):
                os.remove(os.path.join(self.tmp, name))
        self.assertFalse(os.path.exists(self.form_path))
        payload1_again = self._show_ok(db_path, id1)
        self.assertEqual(payload1_again, payload1)

        # 多次（含失败外的）读取前后，库中编号与原文完全一致。
        self.assertEqual(self._dump_rows(db_path), rows_before)
        # 提交时的表单原文未被读取动作改写。
        row1 = next(row for row in rows_before if row[0] == id1)
        self.assertEqual(json.loads(row1[1]), form_text_v1)
        self.assertEqual(row1[1], form_text_raw)

    # -- 未保存的合法编号 -------------------------------------------------

    def test_unsaved_id_returns_not_found_and_leaves_db_untouched(self):
        db_path = os.path.join(self.tmp, "data.db")
        saved = self._submit_ok(_note_form("text"), {"note": "甲"}, db_path)
        rows_before = self._dump_rows(db_path)
        # 不假定编号从 1 开始，也不假定密度：取一个必然未保存的合法正整数。
        missing_id = max(row[0] for row in rows_before) + 100
        self.assertNotIn(missing_id, [row[0] for row in rows_before])

        proc = self._show(db_path, missing_id)
        self.assertEqual(proc.returncode, 1)
        self.assertEqual(self._single_payload(proc), {"error": "not_found"})
        self.assertEqual(self._dump_rows(db_path), rows_before)
        # 已保存记录仍可正常读取。
        self.assertEqual(self._show_ok(db_path, saved)["submissionId"], saved)

    # -- 编号非法：0、负数、含字母 ---------------------------------------

    def test_invalid_ids_return_invalid_input_and_leave_db_untouched(self):
        db_path = os.path.join(self.tmp, "data.db")
        self._submit_ok(_note_form("text"), {"note": "甲"}, db_path)
        rows_before = self._dump_rows(db_path)

        for raw_id in ("0", "-1", "-007", "abc", "1a", "0x1", " 1", "1 "):
            with self.subTest(raw_id=raw_id):
                proc = self._show(db_path, raw_id)
                self.assertEqual(proc.returncode, 2, raw_id)
                self.assertEqual(
                    self._single_payload(proc), {"error": "invalid_input"}
                )
                # 失败读取前后记录数量与原文一致。
                self.assertEqual(self._dump_rows(db_path), rows_before)

    # -- 前导零的合法编号读到同一记录 -------------------------------------

    def test_leading_zeros_read_same_record(self):
        db_path = os.path.join(self.tmp, "data.db")
        submission_id = self._submit_ok(
            _note_form("text"), {"note": "甲\n乙"}, db_path
        )
        baseline = self._show_ok(db_path, submission_id)
        rows_before = self._dump_rows(db_path)

        for raw_id in ("0" + str(submission_id), "000" + str(submission_id)):
            with self.subTest(raw_id=raw_id):
                payload = self._show_ok(db_path, raw_id)
                self.assertEqual(payload, baseline)
                self.assertEqual(payload["submissionId"], submission_id)

        # 读取不改动库中记录。
        self.assertEqual(self._dump_rows(db_path), rows_before)

    # -- 数据库路径不存在：报错且不创建任何文件 ---------------------------

    def test_missing_database_path_creates_nothing(self):
        missing_dir = os.path.join(self.tmp, "no_such_dir")
        os.mkdir(missing_dir)
        db_path = os.path.join(missing_dir, "data.db")
        self.assertEqual(os.listdir(missing_dir), [])

        proc = self._show(db_path, 1)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            self._single_payload(proc), {"error": "storage_error"}
        )
        # 既不创建数据库文件，也不留下 -wal/-shm 等附属文件。
        self.assertEqual(os.listdir(missing_dir), [])
        self.assertFalse(os.path.exists(db_path))

    # -- 缺少 submissions 表：报错且不建表、不动既有内容 ------------------

    def test_database_without_submissions_table_creates_nothing(self):
        db_path = os.path.join(self.tmp, "other.db")
        with sqlite3.connect(db_path) as conn:
            conn.execute("CREATE TABLE other (x)")
            conn.execute("INSERT INTO other (x) VALUES (?)", (42,))
            conn.commit()

        proc = self._show(db_path, 1)
        self.assertEqual(proc.returncode, 2)
        self.assertEqual(
            self._single_payload(proc), {"error": "storage_error"}
        )

        with sqlite3.connect(db_path) as conn:
            names = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            # 未新建 submissions 表，既有表与数据保持不变。
            self.assertEqual(names, {"other"})
            self.assertEqual(
                conn.execute("SELECT x FROM other").fetchall(), [(42,)]
            )

    # -- 已有记录的库：成功与失败读取均不改变记录数量与原文 ---------------

    def test_reads_never_modify_existing_records(self):
        db_path = os.path.join(self.tmp, "data.db")
        id1 = self._submit_ok(
            _note_form("text"), {"note": "甲\n乙"}, db_path
        )
        id2 = self._submit_ok(_note_form("number"), {"note": 7}, db_path)
        rows_before = self._dump_rows(db_path)

        for raw_id in (id1, id2, "0" + str(id1), "0", "-1", "abc", 999999):
            with self.subTest(raw_id=raw_id):
                self._show(db_path, raw_id)
                self.assertEqual(self._dump_rows(db_path), rows_before)


if __name__ == "__main__":
    unittest.main()
