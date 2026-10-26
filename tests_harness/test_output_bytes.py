"""Keep exact output contracts visible across subprocess decoding."""

import json
from pathlib import Path
import tempfile
import unittest

from verification import parallel_case, small_case


class OutputBytesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="output-bytes-")
        self.addCleanup(self.temporary.cleanup)
        self.workspace = Path(self.temporary.name)
        for name in ("statusctl", "opsreport"):
            package = self.workspace / name
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")

    def program(self, package, source):
        (self.workspace / package / "__main__.py").write_text(source, encoding="utf-8")

    def test_small_json_contract_rejects_crlf_with_identical_json_value(self):
        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                self.program(
                    "statusctl",
                    "import sys\n"
                    "def number(name, default):\n"
                    "    return int(sys.argv[sys.argv.index(name) + 1]) if name in sys.argv else default\n"
                    "available = number('--available', 7)\n"
                    "reserved = number('--reserved', 3)\n"
                    "payload = f'{{\"available\":{available},\"reserved\":{reserved}}}'\n"
                    f"sys.stdout.buffer.write(payload.encode('utf-8') + {ending!r})\n",
                )
                response = small_case.command(self.workspace, "--format", "json")
                self.assertEqual(json.loads(response.stdout), {"available": 7, "reserved": 3})
                self.assertTrue(response.stdout.endswith(ending.decode("utf-8")))
                criteria = {item["id"]: item for item in small_case.verify(self.workspace)["criteria"]}
                self.assertEqual(criteria["json_unchanged"]["passed"], ending == b"\n")

    def test_parallel_csv_contract_rejects_crlf(self):
        for ending in (b"\n", b"\r\n"):
            with self.subTest(ending=ending):
                payload = parallel_case.HEADER.rstrip("\n").encode("utf-8") + ending
                self.program("opsreport", f"import sys\nsys.stdout.buffer.write({payload!r})\n")
                response = parallel_case.output(self.workspace, [], "audit")
                self.assertEqual(response, payload.decode("utf-8"))
                if ending == b"\n":
                    parallel_case.equal(response, parallel_case.HEADER)
                else:
                    with self.assertRaises(parallel_case.CheckFailure):
                        parallel_case.equal(response, parallel_case.HEADER)

    def test_strict_utf8_decoding_rejects_invalid_bytes(self):
        for package, invoke in (
            ("statusctl", lambda: small_case.command(self.workspace)),
            ("opsreport", lambda: parallel_case.invoke(self.workspace, {"events": []}, "reports")),
        ):
            for stream in ("stdout", "stderr"):
                with self.subTest(package=package, stream=stream):
                    self.program(package, f"import sys\nsys.{stream}.buffer.write(b'\\xff')\n")
                    with self.assertRaises(UnicodeDecodeError):
                        invoke()


if __name__ == "__main__":
    unittest.main()
