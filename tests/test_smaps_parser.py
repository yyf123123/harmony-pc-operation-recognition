import unittest

from collector.smaps_collector import parse_smaps_text


class SmapsParserTests(unittest.TestCase):
    def test_parses_file_and_anonymous_vmas(self):
        text = """00400000-00402000 r-xp 00000000 00:00 1 /system/lib/liba.so
Size:                  8 kB
Rss:                   4 kB
Pss:                   4 kB
Referenced:            8 kB
Anonymous:             0 kB

00600000-00602000 rw-p 00000000 00:00 0
Size:                  8 kB
Rss:                   4 kB
Pss:                   4 kB
Referenced:            4 kB
Anonymous:             4 kB
"""
        rows = parse_smaps_text(text)
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].namespace, "FILE")
        self.assertEqual(rows[0].referenced_pages, 2.0)
        self.assertEqual(rows[1].namespace, "ANON")


if __name__ == "__main__":
    unittest.main()
