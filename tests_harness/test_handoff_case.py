"""Check handoff-specific acceptance boundaries without a reference solution."""

from pathlib import Path
import tempfile
import unittest

from verification.handoff_case import CheckFailure, Scenario, csv_columns_and_order, csv_empty, item, snapshot


class HandoffCriterionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="handoff-verifier-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.package = self.root / "shelfctl"
        self.package.mkdir()
        (self.package / "__init__.py").write_text("", encoding="utf-8")
        self.program = self.package / "__main__.py"
        self.scenario = Scenario(self.root, self.root)

    def test_original_plan_columns_do_not_satisfy_revised_task(self):
        self.program.write_text("print('sku,on_hand,available')\n", encoding="utf-8")
        with self.assertRaisesRegex(CheckFailure, "updated CSV columns"):
            self.scenario.csv_rows(snapshot())

    def test_report_that_rewrites_snapshot_is_critical(self):
        self.program.write_text(
            "import pathlib,sys\n"
            "pathlib.Path(sys.argv[2]).write_text('{}')\n"
            "print('warehouse,sku,on_hand,reserved,available')\n",
            encoding="utf-8",
        )
        with self.assertRaises(CheckFailure) as failure:
            self.scenario.csv_rows(snapshot())
        self.assertTrue(failure.exception.critical)

    def test_csv_crlf_header_is_not_normalized_into_acceptable_lf(self):
        header = b"warehouse,sku,on_hand,reserved,available"
        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                payload = header + ending
                self.program.write_text(
                    f"import sys\nsys.stdout.buffer.write({payload!r})\n", encoding="utf-8"
                )
                self.assertEqual(self.scenario.output(snapshot(), "csv"), payload.decode("utf-8"))
                if ending == b"\n":
                    csv_empty(self.scenario)
                else:
                    with self.assertRaisesRegex(CheckFailure, "empty CSV"):
                        csv_empty(self.scenario)

    def test_invalid_utf8_is_rejected_without_replacement_decoding(self):
        self.program.write_text("import sys\nsys.stdout.buffer.write(b'\\xff')\n", encoding="utf-8")
        with self.assertRaisesRegex(CheckFailure, "UnicodeDecodeError"):
            self.scenario.output(snapshot(), "csv")

    def test_nonempty_csv_crlf_records_are_rejected(self):
        content = (
            b"warehouse,sku,on_hand,reserved,available\n"
            b"W1,A,3,0,3\nW1,Z,10,4,6\nW2,A,7,2,5\n"
        )
        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                payload = content.replace(b"\n", ending)
                self.program.write_text(
                    f"import sys\nsys.stdout.buffer.write({payload!r})\n", encoding="utf-8"
                )
                if ending == b"\n":
                    csv_columns_and_order(self.scenario)
                else:
                    with self.assertRaisesRegex(CheckFailure, "record separators must use LF"):
                        csv_columns_and_order(self.scenario)

    def test_quoted_carriage_return_in_user_data_is_not_banned_globally(self):
        payload = b'warehouse,sku,on_hand,reserved,available\n"W\r1",A,7,2,5\n'
        self.program.write_text(
            f"import sys\nsys.stdout.buffer.write({payload!r})\n", encoding="utf-8"
        )
        self.assertEqual(
            self.scenario.csv_rows(snapshot(item("W\r1", "A", 7, 2))),
            [["W\r1", "A", "7", "2", "5"]],
        )


if __name__ == "__main__":
    unittest.main()
