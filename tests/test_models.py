from __future__ import annotations

from pathlib import Path
import sys
import unittest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_DIR = PROJECT_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from matched_betting.models import decimal_from_probability
from matched_betting.providers.polymarket import _parse_stringified_json_list


class ModelTests(unittest.TestCase):
    def test_decimal_from_probability(self) -> None:
        self.assertEqual(decimal_from_probability(0.5), 2.0)
        self.assertEqual(decimal_from_probability(0.25), 4.0)

    def test_parse_stringified_json_list(self) -> None:
        self.assertEqual(_parse_stringified_json_list('["A", "B"]'), ["A", "B"])
        self.assertEqual(_parse_stringified_json_list("[0.4, 0.6]"), [0.4, 0.6])


if __name__ == "__main__":
    unittest.main()
